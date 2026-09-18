#
#  agent-bridge-mcp - Copyright(c) 2026
#

# A message store shared by every peer, plus a live fan-out so a message does
# not have to be polled for.
#
# The polling half and the push half are NOT alternatives. An agent that is
# mid-turn cannot service a socket, so every message is durably queued first and
# only then offered to whatever happens to be listening. A subscriber that is
# absent, slow or dead loses nothing: it reads the same message out of the inbox
# on its next turn.
#
# One deliberate exception: a message actually written to a connected /notify
# socket under the addressee's own name is consumed, exactly as bridge_inbox
# would consume it. "Written" means the frame reached the kernel, not that the
# listener processed it; a half-open connection (sleep, Wi-Fi drop) can lose
# that one frame from the inbox, though bridge_history keeps it. The
# alternative - never consuming on the socket - replayed every message on every
# reconnect, forever, and this server reconnects often. Push is therefore the
# system of record only for a listener that is connected; for everyone else it
# is still just latency.

import asyncio
import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Message:
    id: int
    ts: float
    sender: str
    to: str
    text: str
    thread: str = ""
    read: bool = False
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["age_s"] = round(time.time() - self.ts, 1)
        return d


# surrogatepass, because a lone surrogate can arrive through /api/send and
# json.dumps will happily store it. A strict encode here would then raise from
# _evict on every later post, permanently, until the store was hand-edited.
def _size(text: str) -> int:
    return len(text.encode("utf-8", errors="surrogatepass"))


class Mailbox:
    # Persisted to disk, because "durable" has to mean durable against the
    # process ending, not merely against a recipient being busy. Holding the
    # queue in memory alone was wrong in the most ordinary way possible: this
    # server is restarted every time a tool is added to it, and each restart
    # silently discarded an unread question and every answer nobody had polled
    # for yet. A mailbox that loses mail when its process exits is a buffer.
    def __init__(self, capacity: int = 200, store: str | Path | None = None,
                 max_message_bytes: int = 64 * 1024, max_bytes: int = 4 * 1024 * 1024,
                 debounce_s: float = 0.25):
        if max_message_bytes <= 0 or max_bytes < max_message_bytes:
            raise ValueError(
                f"mailbox_max_bytes ({max_bytes}) must be at least max_message_bytes "
                f"({max_message_bytes}), or one post evicts every other message"
            )
        self._capacity = capacity
        self._max_message_bytes = max_message_bytes
        self._max_bytes = max_bytes
        self._messages: list[Message] = []
        self._next_id = 1
        self._seen: dict[str, float] = {}
        # agent name -> queues. One agent may have several sessions listening.
        self._subs: dict[str, list[asyncio.Queue]] = {}
        self._store = Path(store) if store else None
        # Read-state writes are coalesced (see _persist_soon); this is the
        # pending timer, and the delay.
        self._debounce_s = debounce_s
        self._pending: asyncio.TimerHandle | None = None
        self._load()

    # --- persistence --------------------------------------------------------

    def _load(self) -> None:
        if not self._store or not self._store.exists():
            return
        try:
            data = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return                                  # a corrupt store is not fatal
        for row in data.get("messages", []):
            row.pop("age_s", None)
            try:
                self._messages.append(Message(**row))
            except TypeError:
                continue
        self._seen = data.get("seen", {})
        self._next_id = max((m.id for m in self._messages), default=0) + 1

    def _persist(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        if not self._store:
            return
        payload = {"messages": [asdict(m) for m in self._messages], "seen": self._seen}
        tmp = self._store.with_suffix(self._store.suffix + ".tmp")
        try:
            # Write-then-replace: a crash mid-write must not leave a truncated
            # store that _load then silently discards.
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._store)
        except OSError:
            pass

    # Two kinds of write. A post ADDS data and is written at once: losing one
    # to a kill inside a debounce window is a dropped question, the exact
    # failure this store exists to prevent. A read-state change only flips a
    # flag, happens on every inbox() and every socket frame, and rewrote the
    # whole file each time; losing one costs a single re-delivery. Those are
    # coalesced onto a short timer. Outside an event loop (tests, tools) they
    # write immediately, so the behaviour is only ever "at least as durable".
    def _persist_soon(self) -> None:
        if not self._store:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._persist()
            return
        if self._pending is None:
            self._pending = loop.call_later(self._debounce_s, self._persist)

    # Flush anything pending. Wired to the app's shutdown, and safe to call
    # any time.
    def close(self) -> None:
        if self._pending is not None:
            self._persist()

    @property
    def bytes_used(self) -> int:
        return sum(_size(m.text) for m in self._messages)

    # --- writing ---

    def post(self, sender: str, to: str, text: str, thread: str = "", meta: dict | None = None) -> Message:
        sender = (sender or "unknown").strip().lower()
        to = (to or "").strip().lower()
        if not to:
            raise ValueError("'to' is required - name the peer this is for")
        if not text.strip():
            raise ValueError("'text' is empty")
        size = _size(text)
        if size > self._max_message_bytes:
            raise ValueError(
                f"message is {size} bytes; the limit is {self._max_message_bytes}. "
                "Put the bulk in a file under a root and send its path instead."
            )
        for label, value in (("sender", sender), ("to", to), ("thread", thread)):
            if len(value) > 200:
                raise ValueError(f"'{label}' is longer than 200 characters")

        msg = Message(id=self._next_id, ts=time.time(), sender=sender, to=to,
                      text=text, thread=thread, meta=meta or {})
        self._next_id += 1
        self._messages.append(msg)
        self._seen[sender] = msg.ts
        self._evict()

        self._persist()
        self._fanout(msg)
        return msg

    # Oldest-first eviction by count AND by bytes, and only messages already
    # read - an unread message aging out would be a silently dropped question.
    # Only when every message is unread does the oldest go regardless, because
    # the alternative is a store that grows without bound.
    def _evict(self) -> None:
        while len(self._messages) > self._capacity or self.bytes_used > self._max_bytes:
            if len(self._messages) <= 1:
                return
            victim = next((m for m in self._messages if m.read), None)
            if victim is None:
                victim = self._messages[0]
            self._messages.remove(victim)

    def _fanout(self, msg: Message) -> None:
        for q in self._subs.get(msg.to, []) + self._subs.get("*", []):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # The durable copy is already in _messages; a full queue means a
                # listener is wedged, which is not the sender's problem.
                pass

    # --- reading ---

    def inbox(self, agent: str, limit: int = 20, peek: bool = False, thread: str = "") -> list[Message]:
        agent = (agent or "").strip().lower()
        self._seen[agent] = time.time()
        out = [m for m in self._messages
               if m.to == agent and not m.read and (not thread or m.thread == thread)]
        out = out[:limit]
        if not peek:
            for m in out:
                m.read = True
            # Read state has to survive too, or a restart re-delivers everything
            # the peer already answered.
            self._persist_soon()
        return out

    def history(self, agent: str = "", limit: int = 50, thread: str = "") -> list[Message]:
        out = [m for m in self._messages
               if (not agent or m.to == agent or m.sender == agent)
               and (not thread or m.thread == thread)]
        return out[-limit:]

    def unread_count(self, agent: str) -> int:
        agent = (agent or "").strip().lower()
        return sum(1 for m in self._messages if m.to == agent and not m.read)

    def peers(self) -> list[dict]:
        now = time.time()
        names = set(self._seen) | {m.sender for m in self._messages} | {m.to for m in self._messages}
        return sorted(
            ({"name": n,
              "unread": self.unread_count(n),
              "last_seen_s_ago": round(now - self._seen[n], 1) if n in self._seen else None}
             for n in names if n),
            key=lambda r: r["name"],
        )

    # Block until mail arrives for `agent`, or `timeout` passes. Returns what
    # bridge_inbox would have returned at that moment.
    #
    # The message that woke us is included even if a /notify socket for the
    # same agent marked it read between the wake-up and the re-read - both
    # subscribe to the same fan-out, and the socket's send can complete first.
    # Without this the waiter reported "mail arrived" with an empty list.
    async def wait(self, agent: str, timeout: float, limit: int = 20,
                   peek: bool = False) -> tuple[list[Message], bool]:
        agent = (agent or "").strip().lower()
        existing = self.inbox(agent, limit=limit, peek=peek)
        if existing:
            return existing, False

        q = self.subscribe(agent)
        try:
            woke = await asyncio.wait_for(q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return [], True
        finally:
            self.unsubscribe(agent, q)

        msgs = self.inbox(agent, limit=limit, peek=peek)
        if woke.to == agent and woke not in msgs:
            if not peek:
                self.mark_read(woke)
            msgs.insert(0, woke)
        return msgs, False

    # --- live subscription ---

    def subscribe(self, agent: str, maxsize: int = 64) -> asyncio.Queue:
        agent = (agent or "*").strip().lower()
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.setdefault(agent, []).append(q)
        return q

    # For the WebSocket: mark messages read once their frame is actually on the
    # wire, in one write. Marking before the send would drop a message whose
    # send failed; never marking meant every reconnect replayed it.
    def mark_read(self, *msgs: Message) -> None:
        for m in msgs:
            m.read = True
        if msgs:
            self._persist_soon()

    def unsubscribe(self, agent: str, q: asyncio.Queue) -> None:
        agent = (agent or "*").strip().lower()
        lst = self._subs.get(agent, [])
        if q in lst:
            lst.remove(q)
