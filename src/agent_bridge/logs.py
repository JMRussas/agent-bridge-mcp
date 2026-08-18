#
#  agent-bridge-mcp - Copyright(c) 2026
#

# Access to the game's log files, as a separate surface from bridge_read.
#
# It needs to be separate for two reasons:
#
#   1. The logs live where the general file reader refuses to go. engine.log is
#      written next to the executable, which means inside bin/ and dist/ - and
#      bin/ is on the deny list precisely so a remote agent cannot trawl build
#      output. Reading a log is not trawling build output, so this exposes the
#      log FILENAMES specifically rather than opening the directory.
#
#   2. A missing or stale log is the trap, not the exception. The desktop build
#      only started writing engine.log recently, so an old published binary
#      produces none at all - and "no engine.log" then reads as "the feed never
#      started" when it actually means "old binary". Every listing here compares
#      the log against the executable beside it and says which case it is, so a
#      reader cannot draw the wrong conclusion from an absence.

from __future__ import annotations

import os
import re
import time
from pathlib import Path

# Only these names are readable, anywhere under a configured root. An allowlist
# of filenames keeps bin/ closed while letting the logs inside it through.
LOG_NAMES = {"engine.log", "diag.log"}

# Directories with nothing to find and a lot to walk.
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".vs", "obj",
             "library", "assets", "noz"}

# Executables whose timestamp tells us whether a log belongs to the current build.
EXE_NAMES = ("RogueLite.exe", "roguelite.exe", "RogueLite.dll")

MAX_TAIL = 2000


class Logs:
    def __init__(self, roots: dict[str, Path]):
        self.roots = roots

    def _scan(self):
        for name, base in self.roots.items():
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for fn in filenames:
                    if fn in LOG_NAMES:
                        yield name, base, Path(dirpath) / fn

    # A log is only meaningful next to the binary that wrote it, so report both.
    def _build_note(self, log: Path | None, folder: Path) -> dict:
        exe = next((folder / e for e in EXE_NAMES if (folder / e).exists()), None)
        if exe is None:
            return {"build_exe": None, "note": ""}

        exe_m = exe.stat().st_mtime
        info = {"build_exe": exe.name,
                "build_modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exe_m))}

        if log is None or not log.exists():
            info["note"] = (
                f"No engine.log beside {exe.name}. If that binary was published before "
                "Log.Path was set in Game.Init, it never writes one - so this absence "
                "means OLD BINARY, not 'the feed never started'. Republish before "
                "concluding anything from it."
            )
        elif log.stat().st_mtime < exe_m:
            info["note"] = (
                f"{log.name} is OLDER than {exe.name}: it is from a previous run of a "
                "previous build. Re-run before reading it as current."
            )
        else:
            info["note"] = ""
        return info

    def list(self) -> dict:
        now = time.time()
        found = []

        for root_name, base, path in self._scan():
            st = path.stat()

            found.append({
                "name": path.name,
                "path": f"{root_name}:{path.relative_to(base).as_posix()}",
                "bytes": st.st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "age_s": round(now - st.st_mtime),
                "lines": sum(1 for _ in path.open(encoding="utf-8", errors="replace")),
                **self._build_note(path, path.parent),
            })

        # Folders that hold a build but no engine.log at all are the interesting
        # gap, and they would be invisible in a listing of files that exist.
        #
        # Keyed on engine.log ALONE. Keying it on "this folder has any log we
        # found" hid the most important case there is: dist/live has a diag.log
        # and no engine.log, which is exactly the published-before-the-change
        # build a reader must not mistake for a silent subsystem.
        missing = []
        for root_name, base in self.roots.items():
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                folder = Path(dirpath)
                if not any(e in filenames for e in EXE_NAMES):
                    continue
                if "engine.log" not in filenames:
                    missing.append({
                        "folder": f"{root_name}:{folder.relative_to(base).as_posix()}",
                        **self._build_note(None, folder),
                    })

        found.sort(key=lambda r: r["age_s"])
        return {"logs": found, "builds_without_engine_log": missing}

    def _resolve(self, target: str) -> Path:
        raw = (target or "").strip().replace("\\", "/")
        if ":" in raw and raw[1:2] != ":":
            root_name, _, rest = raw.partition(":")
            base = self.roots.get(root_name.strip().lower())
            if base is None:
                raise ValueError(f"unknown root '{root_name}'")
            candidate = (base / rest.lstrip("/")).resolve()
        else:
            candidate = Path(raw).resolve()

        if candidate.name not in LOG_NAMES:
            raise ValueError(
                f"'{candidate.name}' is not a log this tool serves. Readable: "
                f"{', '.join(sorted(LOG_NAMES))}. Use bridge_read for source files."
            )
        for base in self.roots.values():
            if base in candidate.parents:
                break
        else:
            raise ValueError(f"outside every configured root: {candidate}")
        if not candidate.exists():
            raise ValueError(f"no such log: {candidate}")
        return candidate

    def read(self, target: str = "", lines: int = 200, contains: str = "",
             level: str = "") -> dict:
        # With no target, serve the most recently written log - which is almost
        # always the one the asker means.
        if not target:
            listing = self.list()["logs"]
            if not listing:
                return {"error": "no engine.log or diag.log exists under any root yet",
                        "hint": "the build may predate Log.Path, or has not been run"}
            target = listing[0]["path"]

        path = self._resolve(target)
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(text)

        if level:
            want = level.strip().upper()
            text = [ln for ln in text if f"[{want}]" in ln]
        if contains:
            try:
                rx = re.compile(contains, re.IGNORECASE)
            except re.error as e:
                return {"error": f"bad regular expression: {e}"}
            text = [ln for ln in text if rx.search(ln)]

        lines = max(1, min(int(lines), MAX_TAIL))
        tail = text[-lines:]
        st = path.stat()
        return {
            "path": str(path),
            "total_lines": total,
            "matched_lines": len(text),
            "returned": len(tail),
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "age_s": round(time.time() - st.st_mtime),
            **self._build_note(path, path.parent),
            "text": "\n".join(tail),
        }
