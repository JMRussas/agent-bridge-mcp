#
#  agent-bridge-mcp - Copyright(c) 2026
#

# A message store shared by every peer, plus a live fan-out so a message does
# not have to be polled for.
#
# The polling half and the push half are NOT alternatives. An agent that is
# mid-turn cannot service a socket, so every message is durably queued first and
# only then offered to whatever happens to be listening. A subscriber that is
# absent, slow or dead therefore loses nothing: it reads the same message out of
# the inbox on its next turn. Push is an optimisation on latency, never the
# system of record.

import asyncio
import time
from dataclasses import dataclass, field, asdict


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


class Mailbox:
    def __init__(self, capacity: int = 200):
        self._capacity = capacity
        self._messages: list[Message] = []
        self._next_id = 1
        self._seen: dict[str, float] = {}
        # agent name -> queues. One agent may have several sessions listening.
        self._subs: dict[str, list[asyncio.Queue]] = {}

    # --- writing ---

    def post(self, sender: str, to: str, text: str, thread: str = "", meta: dict | None = None) -> Message:
        sender = (sender or "unknown").strip().lower()
        to = (to or "").strip().lower()
        if not to:
            raise ValueError("'to' is required - name the peer this is for")
        if not text.strip():
            raise ValueError("'text' is empty")

        msg = Message(id=self._next_id, ts=time.time(), sender=sender, to=to,
                      text=text, thread=thread, meta=meta or {})
        self._next_id += 1
        self._messages.append(msg)
        self._seen[sender] = msg.ts

        # Oldest-first eviction, and only messages already read - an unread
        # message aging out would be a silently dropped question.
        while len(self._messages) > self._capacity:
            victim = next((m for m in self._messages if m.read), None)
            if victim is None:
                victim = self._messages[0]
            self._messages.remove(victim)

        self._fanout(msg)
        return msg

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

    # --- live subscription ---

    def subscribe(self, agent: str, maxsize: int = 64) -> asyncio.Queue:
        agent = (agent or "*").strip().lower()
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.setdefault(agent, []).append(q)
        return q

    def unsubscribe(self, agent: str, q: asyncio.Queue) -> None:
        agent = (agent or "*").strip().lower()
        lst = self._subs.get(agent, [])
        if q in lst:
            lst.remove(q)
