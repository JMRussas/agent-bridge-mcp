"""Session context and evidence links. References confer no access or authority."""

import json
import time
import uuid

from agent_bridge.auth import Forbidden, Principal
from agent_bridge.mailbox import Mailbox

RELATIONS = {"originated_in", "replies_to", "supports", "contradicts", "followed_up_in"}
TARGETS = {"conversation", "session", "checkin", "assignment", "commit", "artifact", "message"}
OUTCOMES = {"blocked", "completed", "verified", "accepted", "reopened"}


def bounded(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("data must be an object")
    if len(json.dumps(value, allow_nan=False).encode()) > 16384:
        raise ValueError("data exceeds 16 KiB")
    return value


def reference(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError(f"{label} must be a nonempty string of at most 2000 characters")
    return value.strip()


class Evidence:
    def __init__(self, box: Mailbox):
        self.box = box
        self.db = box.db
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, owner TEXT NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS links (id TEXT PRIMARY KEY, message_id INTEGER NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS links_message ON links(message_id,ts)")
            self.db.execute("CREATE TABLE IF NOT EXISTS outcomes (id TEXT PRIMARY KEY, message_id INTEGER NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS outcomes_message ON outcomes(message_id,ts)")

    def message(self, who: Principal, ref: str):
        m = self.box.get(ref)
        if not (who.may_read(m.to) or who.may_read(m.sender)):
            raise Forbidden("message evidence is visible only to its participants or operator")
        return m

    def register(self, who: Principal, context: dict) -> dict:
        context = dict(bounded(context))
        allowed = {"harness", "conversation_ref", "repository", "worktree", "branch", "commit", "configured_model", "evidence_source", "listener_connected", "wake_capability"}
        if set(context) - allowed:
            raise ValueError("unknown session context fields")
        for key in ("harness", "conversation_ref"):
            reference(context.get(key), key)
        for key, value in context.items():
            if key == "listener_connected":
                if not isinstance(value, bool):
                    raise ValueError("listener_connected must be boolean")
            elif not isinstance(value, str) or len(value) > 2000:
                raise ValueError(f"{key} must be a string of at most 2000 characters")
        if context.get("wake_capability", "none") not in {"none", "context_only", "supervised_worker"}:
            raise ValueError("unknown wake capability")
        session = {"id": str(uuid.uuid4()), "owner": who.name, "registered_at": time.time(),
                   "context": context, "provenance": "operator_registered" if who.admin else "participant_reported",
                   "model_attested": False}
        with self.box._lock, self.db:
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (session["id"], who.name, session["registered_at"], json.dumps(session)))
        return session

    def session(self, who: Principal, ref: str) -> dict:
        row = self.db.execute("SELECT owner,payload FROM sessions WHERE id=?", (ref,)).fetchone()
        if not row:
            raise ValueError("unknown session")
        if not who.may_read(row[0]):
            raise Forbidden("session context is visible only to its role or operator")
        return json.loads(row[1])

    def sessions(self, who: Principal) -> list[dict]:
        with self.box._lock:
            return [json.loads(r[1]) for r in self.db.execute("SELECT owner,payload FROM sessions ORDER BY ts") if who.may_read(r[0])]

    def directory(self, owner: str) -> list[dict]:
        # Explicitly limited public discovery data, never conversation references.
        rows = self.db.execute("SELECT payload FROM sessions WHERE owner=? ORDER BY ts DESC LIMIT 10", (owner,))
        return [{"id": s["id"], "registered_at": s["registered_at"], "harness": s["context"]["harness"],
                 "configured_model": s["context"].get("configured_model", ""),
                 "provenance": s["provenance"], "model_attested": False,
                 "wake_capability": s["context"].get("wake_capability", "none")}
                for s in (json.loads(r[0]) for r in rows)]

    def link(self, who: Principal, message_id: str, relation: str, target_type: str,
             target_ref: str, inferred: bool = False) -> dict:
        if relation not in RELATIONS or target_type not in TARGETS:
            raise ValueError("unknown relation or target type")
        target_ref = reference(target_ref, "target_ref")
        with self.box._lock:
            m = self.message(who, message_id)
            if target_type == "message":
                target_ref = self.message(who, target_ref).uid
            elif target_type == "session":
                self.session(who, target_ref)
            item = {"id": str(uuid.uuid4()), "message_id": m.uid, "relation": relation,
                    "target_type": target_type, "target_ref": target_ref, "inferred": bool(inferred),
                    "actor": who.name, "ts": time.time()}
            with self.db:
                self.db.execute("INSERT INTO links VALUES (?,?,?,?)", (item["id"], m.id, item["ts"], json.dumps(item)))
            return item

    def find_links(self, who: Principal, target_type: str, target_ref: str) -> list[dict]:
        if target_type not in TARGETS:
            raise ValueError("unknown target type")
        reference(target_ref, "target_ref")
        with self.box._lock:
            out = []
            for row in self.db.execute("SELECT payload FROM links ORDER BY ts"):
                item = json.loads(row[0])
                if item["target_type"] != target_type or item["target_ref"] != target_ref:
                    continue
                try:
                    self.message(who, item["message_id"])
                except Forbidden:
                    continue
                out.append(item)
            return out

    def outcome(self, who: Principal, message_id: str, kind: str, artifact_ref: str = "",
                evidence_refs: list[str] | None = None, details: dict | None = None) -> dict:
        if kind not in OUTCOMES:
            raise ValueError("unknown outcome")
        evidence_refs = [] if evidence_refs is None else evidence_refs
        if not isinstance(evidence_refs, list) or len(evidence_refs) > 100:
            raise ValueError("evidence_refs must be a list with at most 100 references")
        for ref in evidence_refs:
            reference(ref, "evidence reference")
        if artifact_ref:
            reference(artifact_ref, "artifact_ref")
        if kind in {"verified", "accepted"}:
            reference(artifact_ref, "artifact_ref")
            if not evidence_refs:
                raise ValueError("verified/accepted outcomes require evidence_refs")
        with self.box._lock:
            m = self.message(who, message_id)
            if kind == "accepted" and not (who.admin or (m.authenticated_principal and who.name == m.authenticated_principal)):
                raise Forbidden("only the authenticated assigner or operator may accept work")
            if kind == "accepted":
                row = self.db.execute("SELECT payload FROM outcomes WHERE message_id=? ORDER BY rowid DESC LIMIT 1", (m.id,)).fetchone()
                last = json.loads(row[0]) if row else {}
                if last.get("kind") not in {"verified", "accepted"} or last.get("artifact_ref") != artifact_ref:
                    raise ValueError("acceptance requires the current verified artifact; reverify after changes or reopening")
            item = {"id": str(uuid.uuid4()), "message_id": m.uid, "kind": kind,
                    "artifact_ref": artifact_ref, "evidence_refs": evidence_refs,
                    "details": bounded({} if details is None else details), "actor": who.name, "ts": time.time()}
            with self.db:
                self.db.execute("INSERT INTO outcomes VALUES (?,?,?,?)", (item["id"], m.id, item["ts"], json.dumps(item)))
                self.box._event(m.id, kind, who.name, {"outcome_id": item["id"], "artifact_ref": artifact_ref})
            return item

    def inspect(self, who: Principal, message_id: str) -> dict:
        with self.box._lock:
            m = self.message(who, message_id)
            def records(table):
                return [json.loads(r[0]) for r in self.db.execute(f"SELECT payload FROM {table} WHERE message_id=? ORDER BY ts", (m.id,))]
            events = [{"id": r[0], "kind": r[1], "ts": r[2], "actor": r[3], "data": json.loads(r[4])}
                      for r in self.db.execute("SELECT id,kind,ts,actor,data FROM events WHERE message_id=? ORDER BY id", (m.id,))]
            return {"message": m.as_dict(), "links": records("links"), "outcomes": records("outcomes"), "events": events}
