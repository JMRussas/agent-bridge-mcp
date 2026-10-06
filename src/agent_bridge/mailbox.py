#
#  agent-bridge-mcp - Copyright(c) 2026
#

# A message store shared by every agent, plus a live fan-out so a message does
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
import sqlite3
import uuid
import threading
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

    ack_required: bool = False
    acknowledged_at: float | None = None
    authenticated_principal: str = ""
    admin: bool = False
    impersonated: bool = False
    uid: str = ""
    bridge_id: str = ""

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
    # History is retained independently of pending-delivery limits. JSON paths
    # migrate once into a sibling SQLite file; the original remains untouched.
    def __init__(self, capacity: int = 200, store: str | Path | None = None,
                 max_message_bytes: int = 64 * 1024, max_bytes: int = 4 * 1024 * 1024,
                 debounce_s: float = 0.25):
        if capacity <= 0 or max_message_bytes <= 0 or max_bytes < max_message_bytes:
            raise ValueError("capacity must be positive and max_bytes must be at least max_message_bytes")
        self._capacity = capacity
        self._max_message_bytes = max_message_bytes
        self._max_bytes = max_bytes
        self._subs: dict[str, list[asyncio.Queue]] = {}
        self._seen: dict[str, float] = {}
        self._dirty: dict[int, Message] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._debounce_s = debounce_s
        self._pending: asyncio.TimerHandle | None = None
        legacy = Path(store) if store and Path(store).suffix.lower() == ".json" else None
        self._store = legacy.with_suffix(".sqlite3") if legacy else (Path(store) if store else None)
        self.db = sqlite3.connect(str(self._store) if self._store else ":memory:",
                                  check_same_thread=False)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL, thread TEXT NOT NULL, ts REAL NOT NULL, read INTEGER NOT NULL, payload TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL, actor TEXT NOT NULL, data TEXT NOT NULL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS events_message ON events(message_id,id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS messages_inbox ON messages(recipient,read,id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread,id)")
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('bridge_id', ?)", (str(uuid.uuid4()),))
        self.bridge_id = self.db.execute("SELECT value FROM settings WHERE key='bridge_id'").fetchone()[0]
        migrated = self.db.execute("SELECT value FROM settings WHERE key='json_migrated'").fetchone()
        if legacy and legacy.exists() and not migrated:
            # Fail closed: malformed history must never become an empty mailbox.
            data = json.loads(legacy.read_text(encoding="utf-8"))
            with self.db:
                for raw in data.get("messages", []):
                    row = dict(raw)
                    row.pop("age_s", None)
                    row.setdefault("uid", str(uuid.uuid4()))
                    row.setdefault("bridge_id", self.bridge_id)
                    m = Message(**row)
                    self._insert(m)
                self.db.execute("INSERT INTO settings VALUES ('seen', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(data.get("seen", {})),))
                self.db.execute("INSERT INTO settings VALUES ('json_migrated', '1')")
        seen = self.db.execute("SELECT value FROM settings WHERE key='seen'").fetchone()
        self._seen = json.loads(seen[0]) if seen else {}

    def _insert(self, m: Message) -> None:
        self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
                        (m.id or None, m.uid, m.sender, m.to, m.thread, m.ts, int(m.read), json.dumps(asdict(m))))

    def _rows(self, sql: str, args: tuple = ()) -> list[Message]:
        with self._lock:
            out = [Message(**json.loads(r[0])) for r in self.db.execute(sql, args)]
            return [self._dirty.get(m.id, m) if not m.read else m for m in out]

    def _persist(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._pending is not None:
                self._pending.cancel()
                self._pending = None
            with self.db:
                for m in self._dirty.values():
                    changed = self.db.execute("UPDATE messages SET read=?,payload=? WHERE id=? AND read=0", (int(m.read), json.dumps(asdict(m)), m.id)).rowcount
                    if changed:
                        self._event(m.id, "consumed", m.to, {"mode": "legacy"})
                self.db.execute("INSERT INTO settings VALUES ('seen', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(self._seen),))
            self._dirty.clear()

    def _persist_soon(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._persist()
            return
        if self._pending is None:
            self._pending = loop.call_later(self._debounce_s, self._persist)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._persist()
                self.db.close()
                self._closed = True

    @property
    def bytes_used(self) -> int:
        return sum(_size(m.text) for m in self.history(limit=2147483647))

    def post(self, sender: str, to: str, text: str, thread: str = "", meta: dict | None = None, *, ack_required: bool = False,
             authenticated_principal: str = "", admin: bool = False) -> Message:
        for label, value in (("sender", sender), ("to", to), ("text", text), ("thread", thread)):
            if not isinstance(value, str):
                raise ValueError(f"{label} must be a string")
        if not isinstance(ack_required, bool):
            raise ValueError("ack_required must be boolean")
        if meta is not None and not isinstance(meta, dict):
            raise ValueError("metadata must be an object")
        if len(json.dumps(meta or {}, allow_nan=False).encode()) > 16384:
            raise ValueError("metadata exceeds 16 KiB")
        sender = (sender or "unknown").strip().lower()
        to = (to or "").strip().lower()
        if not to:
            raise ValueError("'to' is required - name the agent this is for")
        if not text.strip():
            raise ValueError("'text' is empty")
        size = _size(text)
        if size > self._max_message_bytes:
            raise ValueError(f"message is {size} bytes; the limit is {self._max_message_bytes}. Put the bulk in a file and send its path instead.")
        for label, value in (("sender", sender), ("to", to), ("thread", thread)):
            if len(value) > 200:
                raise ValueError(f"'{label}' is longer than 200 characters")
        with self._lock:
            self._persist()
            pending = self._rows("SELECT payload FROM messages WHERE read=0")
            if len(pending) >= self._capacity or sum(_size(m.text) for m in pending) + size > self._max_bytes:
                raise ValueError("pending mailbox capacity exceeded; acknowledge pending mail before sending more")
            m = Message(id=0, ts=time.time(), sender=sender, to=to, text=text,
                        thread=thread, meta=meta or {}, ack_required=ack_required,
                        authenticated_principal=authenticated_principal, admin=admin,
                        impersonated=bool(admin and authenticated_principal != sender), uid=str(uuid.uuid4()), bridge_id=self.bridge_id)
            with self.db:
                self._insert(m)
                m.id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
                self.db.execute("UPDATE messages SET payload=? WHERE id=?", (json.dumps(asdict(m)), m.id))
                self._event(m.id, "sent", authenticated_principal or sender, {})
            self._seen[sender] = m.ts
        self._fanout(m)
        return m

    def get(self, reference: str | int) -> Message:
        ref = str(reference)
        if ref.isdecimal() and len(ref) <= 19 and int(ref) < 2**63:
            rows = self._rows("SELECT payload FROM messages WHERE id=?", (int(ref),))
        else:
            rows = self._rows("SELECT payload FROM messages WHERE uid=?", (ref,))
        if not rows:
            raise ValueError("unknown message")
        return rows[0]

    def _event(self, message_id: int, kind: str, actor: str, data: dict) -> None:
        self.db.execute("INSERT INTO events(message_id,kind,ts,actor,data) VALUES (?,?,?,?,?)", (message_id, kind, time.time(), actor, json.dumps(data)))

    def offered(self, messages: list[Message], actor: str, transport: str) -> None:
        with self._lock, self.db:
            for m in messages:
                self._event(m.id, "offered", actor, {"transport": transport})

    def delivered(self, reference: str | int, actor: str, transport: str, address: str = "") -> None:
        with self._lock, self.db:
            m = self.get(reference)
            self._event(m.id, "delivered", actor, {"transport": transport, "listener_address": address or actor, "recipient_listener": (address or actor) == m.to})

    def acknowledge(self, reference: str | int, principal) -> Message:
        with self._lock:
            m = self.get(reference)
            if not principal.may_read(m.to):
                from agent_bridge.auth import Forbidden
                raise Forbidden("only the recipient or operator may acknowledge this message")
            self._persist()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # Another server connection may have acknowledged meanwhile.
                m = self.get(reference)
                if m.acknowledged_at is None:
                    m.acknowledged_at = time.time()
                    m.read = True
                    self.db.execute("UPDATE messages SET read=1,payload=? WHERE id=?", (json.dumps(asdict(m)), m.id))
                    self._event(m.id, "acknowledged", principal.name, {})
                self.db.commit()
                return m
            except BaseException:
                self.db.rollback()
                raise

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
        out = self._rows("SELECT payload FROM messages WHERE recipient=? AND read=0 AND (?='' OR thread=?) ORDER BY id", (agent, thread, thread))
        out = [m for m in out if not m.read][:max(0, min(limit, 1000))]
        if not peek:
            self.mark_read(*out)
        return out

    def history(self, agent: str = "", limit: int = 50, thread: str = "") -> list[Message]:
        out = self._rows("SELECT payload FROM messages WHERE (?='' OR recipient=? OR sender=?) AND (?='' OR thread=?) ORDER BY id DESC LIMIT ?", (agent, agent, agent, thread, thread, max(0, limit)))
        return out[::-1]

    def unread_count(self, agent: str) -> int:
        agent = (agent or "").strip().lower()
        with self._lock:
            count = self.db.execute("SELECT count(*) FROM messages WHERE recipient=? AND read=0", (agent,)).fetchone()[0]
            return count - sum(1 for m in self._dirty.values() if m.to == agent and m.read)

    # Every name that has appeared in traffic or listened, with what waits
    # for it. Activity, not configuration: server.py merges this with the
    # configured agents into the directory bridge_agents returns.
    def mailboxes(self) -> list[dict]:
        now = time.time()
        names = set(self._seen) | {r[0] for r in self.db.execute("SELECT sender FROM messages UNION SELECT recipient FROM messages")}
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
        with self._lock:
            for m in msgs:
                current = self.get(m.id)
                if current.ack_required or current.read:
                    continue
                current.read = True
                m.read = True
                self._dirty[m.id] = current
            if self._dirty:
                self._persist_soon()

    def unsubscribe(self, agent: str, q: asyncio.Queue) -> None:
        agent = (agent or "*").strip().lower()
        lst = self._subs.get(agent, [])
        if q in lst:
            lst.remove(q)
