"""Defined telemetry and reviewed learning candidates from retained evidence."""

from __future__ import annotations

import json
import math
import time
import uuid
from collections import Counter

from agent_bridge.auth import Forbidden
from agent_bridge.evidence import reference


class Reports:
    def __init__(self, evidence):
        self.evidence = evidence
        self.box = evidence.box
        self.db = self.box.db
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS learnings (id TEXT PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS learning_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, learning_id TEXT NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL)")

    def telemetry(self, who, overdue_after_s: float = 3600) -> dict:
        if not isinstance(overdue_after_s, (int, float)) or not math.isfinite(overdue_after_s) or overdue_after_s <= 0:
            raise ValueError("overdue_after_s must be finite and positive")
        with self.box._lock:
            messages = [m for m in self.box.history(limit=2147483647) if who.may_read(m.sender) or who.may_read(m.to)]
            pending, overdue, reopened = [], [], []
            blockers = Counter()
            intervals = {"acknowledgment": [], "completion": [], "acceptance": []}
            events = Counter()
            for m in messages:
                record = self.evidence.inspect(who, m.uid)
                if not m.read:
                    pending.append(m.uid)
                    if m.ack_required and time.time() - m.ts >= overdue_after_s:
                        overdue.append(m.uid)
                if m.acknowledged_at is not None:
                    intervals["acknowledgment"].append(max(0, m.acknowledged_at - m.ts))
                for kind, label in (("completed", "completion"), ("accepted", "acceptance")):
                    outcome = next((o for o in record["outcomes"] if o["kind"] == kind), None)
                    if outcome:
                        intervals[label].append(max(0, outcome["ts"] - m.ts))
                for o in record["outcomes"]:
                    if o["kind"] == "blocked":
                        reason = o["details"].get("reason", "unspecified")
                        if isinstance(reason, str):
                            blockers[" ".join(reason.split()).casefold()] += 1
                    if o["kind"] == "reopened":
                        reopened.append(m.uid)
                events.update(e["kind"] for e in record["events"])
            return {"scope": "messages visible to the caller", "messages": len(messages),
                    "pending": pending, "overdue_unacknowledged": overdue, "overdue_after_s": overdue_after_s,
                    "reopened_messages": sorted(set(reopened)), "blockers": dict(blockers), "events": dict(events),
                    "durations_s": {k: {"samples": len(v), "mean": sum(v) / len(v) if v else None, "max": max(v) if v else None} for k, v in intervals.items()},
                    "definitions": {"pending": "not consumed or explicitly acknowledged", "overdue_unacknowledged": "pending ack-required mail older than the chosen threshold; not proof of failure", "durations": "send to explicit acknowledgment or first recorded completion/acceptance", "blockers": "counts of identical normalized reported reasons; no inferred cause", "delivered": "socket write, not model observation", "offered": "inbox/wait response prepared; not proof of receipt"}}

    def propose(self, who, text: str, message_ids: list[str], evidence_refs: list[str],
                contradicting_message_ids: list[str] | None = None) -> dict:
        text = reference(text, "text")
        for label, values in (("message_ids", message_ids), ("evidence_refs", evidence_refs)):
            if not isinstance(values, list) or not values or len(values) > 100:
                raise ValueError(f"{label} requires 1 to 100 references")
        contradictions = [] if contradicting_message_ids is None else contradicting_message_ids
        if not isinstance(contradictions, list) or len(contradictions) > 100:
            raise ValueError("contradicting_message_ids must contain at most 100 references")
        for ref in evidence_refs:
            reference(ref, "evidence reference")
        with self.box._lock:
            support = [self.evidence.message(who, ref).uid for ref in message_ids]
            contrary = [self.evidence.message(who, ref).uid for ref in contradictions]
            item = {"id": str(uuid.uuid4()), "text": text, "message_ids": support,
                    "contradicting_message_ids": contrary, "evidence_refs": evidence_refs,
                    "actor": who.name, "ts": time.time(), "status": "candidate"}
            with self.db:
                self.db.execute("INSERT INTO learnings VALUES (?,?,?)", (item["id"], item["ts"], json.dumps(item)))
            return item

    def get(self, who, learning_id: str) -> dict:
        row = self.db.execute("SELECT payload FROM learnings WHERE id=?", (learning_id,)).fetchone()
        if not row:
            raise ValueError("unknown learning")
        item = json.loads(row[0])
        for ref in item["message_ids"] + item["contradicting_message_ids"]:
            self.evidence.message(who, ref)
        item["reviews"] = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM learning_reviews WHERE learning_id=? ORDER BY id", (learning_id,))]
        if item["reviews"]:
            item["status"] = item["reviews"][-1]["status"]
        item["source_outcomes"] = {ref: self.evidence.inspect(who, ref)["outcomes"] for ref in item["message_ids"] + item["contradicting_message_ids"]}
        if item["status"] == "accepted":
            reviewed_at = item["reviews"][-1]["ts"]
            if any(o["kind"] == "reopened" and o["ts"] > reviewed_at for outcomes in item["source_outcomes"].values() for o in outcomes):
                item["status"] = "needs_revision"
                item["revalidation_reason"] = "source work reopened after the latest review"
        return item

    def list(self, who) -> list[dict]:
        with self.box._lock:
            out = []
            for row in self.db.execute("SELECT id FROM learnings ORDER BY ts"):
                try:
                    out.append(self.get(who, row[0]))
                except Forbidden:
                    continue
            return out

    def review(self, who, learning_id: str, status: str, evidence_refs: list[str], note: str = "") -> dict:
        if status not in {"accepted", "rejected", "needs_revision"}:
            raise ValueError("unknown review status")
        if not isinstance(evidence_refs, list) or not evidence_refs or len(evidence_refs) > 100:
            raise ValueError("review requires 1 to 100 evidence references")
        for ref in evidence_refs:
            reference(ref, "review evidence")
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("note must be a string of at most 2000 characters")
        with self.box._lock:
            self.get(who, learning_id)
            item = {"status": status, "evidence_refs": evidence_refs, "note": note, "actor": who.name, "ts": time.time()}
            with self.db:
                self.db.execute("INSERT INTO learning_reviews(learning_id,ts,payload) VALUES (?,?,?)", (learning_id, item["ts"], json.dumps(item)))
            return self.get(who, learning_id)
