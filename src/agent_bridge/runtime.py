"""Durable exclusive harness claims. No automatic expiry of possibly live jobs."""

import json
import time

from agent_bridge.auth import Forbidden
from agent_bridge.evidence import bounded, reference


class Runtime:
    def __init__(self, evidence):
        self.evidence = evidence
        self.box = evidence.box
        self.db = self.box.db
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS work_claims (message_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")

    def claim(self, who, message_id: str, worker_id: str, action: str = "start", data: dict | None = None) -> dict:
        reference(worker_id, "worker_id")
        data = bounded({} if data is None else data)
        if action not in {"start", "launched", "completed", "failed", "reset"}:
            raise ValueError("unknown claim action")
        with self.box._lock:
            m = self.evidence.message(who, message_id)
            if not who.may_read(m.to):
                raise Forbidden("only the recipient or operator may run this assignment")
            if not m.ack_required:
                raise ValueError("supervised jobs require ack_required=true")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT payload FROM work_claims WHERE message_id=?", (m.id,)).fetchone()
                prior = json.loads(row[0]) if row else None
                acquired = False
                if action == "start":
                    if m.read:
                        raise ValueError("message is already acknowledged")
                    if prior and prior["status"] in {"running", "completed"}:
                        self.db.commit()
                        return {"acquired": False, "claim": prior}
                    acquired = True
                    item = {"message_id": m.uid, "worker_id": worker_id, "status": "running", "actor": who.name, "ts": time.time(), "data": data}
                elif action == "launched":
                    if not prior or prior["worker_id"] != worker_id or prior["status"] != "running":
                        raise ValueError("claim is not held by this worker")
                    if not isinstance(data.get("pid"), int) or data["pid"] < 1:
                        raise ValueError("launched requires a positive process pid")
                    item = {**prior, "data": {**prior["data"], **data, "launched_at": time.time()}}
                elif action == "reset":
                    if not who.admin:
                        raise Forbidden("only the operator may reset an interrupted claim")
                    reference(data.get("reason"), "reset reason")
                    if not prior:
                        raise ValueError("unknown work claim")
                    item = {**prior, "status": "failed", "ts": time.time(), "data": data}
                else:
                    if not prior or prior["worker_id"] != worker_id or prior["status"] not in {"running", action}:
                        raise ValueError("claim is not held by this worker in the expected state")
                    if action == "completed":
                        reference(data.get("artifact_ref"), "artifact_ref")
                    if prior["status"] == action:
                        self.db.commit()
                        return {"acquired": False, "claim": prior}
                    item = {**prior, "status": action, "ts": time.time(), "data": data}
                self.db.execute("INSERT INTO work_claims VALUES (?,?) ON CONFLICT(message_id) DO UPDATE SET payload=excluded.payload", (m.id, json.dumps(item)))
                self.box._event(m.id, "worker_" + action, who.name, {"worker_id": worker_id, **data})
                self.db.commit()
                return {"acquired": acquired, "claim": item}
            except BaseException:
                self.db.rollback()
                raise
