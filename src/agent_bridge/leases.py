"""Advisory path ownership. Expiry does not fence filesystem writes."""

import json
import math
import time
import uuid
from pathlib import PurePosixPath

from agent_bridge.auth import Forbidden


class LeaseConflict(ValueError):
    def __init__(self, holders):
        self.holders = holders
        super().__init__("path reservation conflicts with active holders")


class Leases:
    def __init__(self, evidence, files):
        self.evidence = evidence
        self.box = evidence.box
        self.db = self.box.db
        self.files = files
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS leases (id TEXT PRIMARY KEY, scope TEXT NOT NULL, owner TEXT NOT NULL, session_id TEXT NOT NULL, expires_at REAL NOT NULL, released_at REAL, payload TEXT NOT NULL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS leases_scope ON leases(scope,expires_at)")

    @staticmethod
    def ttl(seconds):
        if not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or not 1 <= seconds <= 86400:
            raise ValueError("ttl_s must be between 1 and 86400 seconds")
        return seconds

    @staticmethod
    def path(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError("lease paths must be nonempty strings of at most 2000 characters")
        value = value.replace("\\", "/")
        p = PurePosixPath(value)
        if p.is_absolute() or any(c in value for c in ":*?[]") or ".." in p.parts:
            raise ValueError("lease paths must be relative literal paths without traversal or globs")
        return p.as_posix()

    @staticmethod
    def overlaps(a, b):
        return a == "." or b == "." or a == b or a.startswith(b + "/") or b.startswith(a + "/")

    def acquire(self, who, session_id: str, root: str, worktree: str, paths: list[str], ttl_s: float = 900) -> dict:
        self.ttl(ttl_s)
        self.evidence.session(who, session_id)
        if not isinstance(paths, list) or not 1 <= len(paths) <= 100:
            raise ValueError("paths requires 1 to 100 literal paths")
        worktree = self.path(worktree or ".")
        scope = self.files.resolve(root + ":" + worktree).as_posix().casefold()
        # Resolve each claim too: symlink aliases must collide with their targets.
        normalized = sorted({self.files.resolve(root + ":" + worktree + "/" + self.path(p)).as_posix().casefold() for p in paths})
        with self.box._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                holders = []
                for row in self.db.execute("SELECT payload FROM leases WHERE released_at IS NULL AND expires_at>?", (time.time(),)):
                    item = json.loads(row[0])
                    if any(self.overlaps(a, b) for a in normalized for b in item["canonical_paths"]):
                        holders.append(item)
                if holders:
                    raise LeaseConflict(holders)
                item = {"id": str(uuid.uuid4()), "owner": who.name, "session_id": session_id,
                        "root": root, "worktree": worktree, "paths": paths, "canonical_paths": normalized,
                        "created_at": time.time(), "expires_at": time.time() + ttl_s, "released_at": None,
                        "advisory": True}
                self.db.execute("INSERT INTO leases VALUES (?,?,?,?,?,?,?)", (item["id"], scope, who.name, session_id, item["expires_at"], None, json.dumps(item)))
                self.db.commit()
                return item
            except BaseException:
                self.db.rollback()
                raise

    def list(self, who) -> list[dict]:
        # Ownership claims are shared coordination metadata for authenticated roles.
        with self.box._lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM leases WHERE released_at IS NULL AND expires_at>? ORDER BY expires_at", (time.time(),))]

    def change(self, who, lease_id: str, ttl_s: float | None = None) -> dict:
        if ttl_s is not None:
            self.ttl(ttl_s)
        with self.box._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT payload FROM leases WHERE id=?", (lease_id,)).fetchone()
                if not row:
                    raise ValueError("unknown lease")
                item = json.loads(row[0])
                if not who.may_read(item["owner"]):
                    raise Forbidden("only the owning role or operator may change a lease")
                if ttl_s is not None:
                    if item["released_at"] is not None or item["expires_at"] <= time.time():
                        raise ValueError("expired/released leases must be acquired again")
                    item["expires_at"] = time.time() + ttl_s
                else:
                    item["released_at"] = item["released_at"] or time.time()
                self.db.execute("UPDATE leases SET expires_at=?,released_at=?,payload=? WHERE id=?", (item["expires_at"], item["released_at"], json.dumps(item), lease_id))
                self.db.commit()
                return item
            except BaseException:
                self.db.rollback()
                raise
