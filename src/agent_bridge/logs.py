#
#  agent-bridge-mcp - Copyright(c) 2026
#

# Access to named log files, as a separate surface from bridge_read.
#
# It needs to be separate for two reasons:
#
#   1. Logs usually live where the general file reader refuses to go. A program
#      writes its log next to its executable, which means inside bin/ or dist/ -
#      and bin/ is on the deny list precisely so a remote agent cannot trawl
#      build output. Reading a log is not trawling build output, so this exposes
#      the configured log FILENAMES specifically rather than opening the folder.
#
#   2. A missing or stale log is the trap, not the exception. A binary built
#      before the code that writes the log produces none at all, and "no log"
#      then reads as "the subsystem never ran" when it means "old binary". When
#      the operator names the executables, every listing compares each log to
#      the executable beside it and says which case it is, so a reader cannot
#      draw the wrong conclusion from an absence.
#
# Nothing here is specific to any program: the names, the executables and the
# directories to skip all come from the `logs` block of config.json, and the
# tools are only registered when at least one log name is configured.

from __future__ import annotations

import os
import time
from pathlib import Path

from agent_bridge.patterns import Deadline, PatternTimeout, compile_pattern

# Directories with nothing to find and a lot to walk. The config's
# logs.skip_dirs extends this.
DEFAULT_SKIP_DIRS = (".git", "node_modules", ".venv", "__pycache__", ".vs", "obj")

MAX_TAIL = 2000
FILTER_TIMEOUT_S = 10.0


def _count_lines(path: Path) -> int:
    with path.open(encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


class Logs:
    def __init__(self, roots: dict[str, Path], names: list[str] | tuple[str, ...] = (),
                 exe_names: list[str] | tuple[str, ...] = (),
                 skip_dirs: list[str] | tuple[str, ...] = ()):
        self.roots = roots
        # Only these names are readable, anywhere under a configured root. An
        # allowlist of filenames keeps bin/ closed while letting the logs
        # inside it through.
        self.names = [n for n in names if n]
        # Executables whose timestamp says whether a log belongs to the
        # current build. Optional: with none, logs are listed without notes.
        self.exe_names = [e for e in exe_names if e]
        self.skip_dirs = set(DEFAULT_SKIP_DIRS) | {d for d in skip_dirs if d}

    @property
    def enabled(self) -> bool:
        return bool(self.names)

    def _walk(self):
        for root_name, base in self.roots.items():
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in self.skip_dirs]
                yield root_name, base, Path(dirpath), filenames

    # A log is only meaningful next to the binary that wrote it, so report both.
    def _build_note(self, log: Path | None, folder: Path) -> dict:
        exe = next((folder / e for e in self.exe_names if (folder / e).exists()), None)
        if exe is None:
            return {"build_exe": None, "note": ""}

        exe_m = exe.stat().st_mtime
        info = {"build_exe": exe.name,
                "build_modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exe_m))}

        if log is None or not log.exists():
            info["note"] = (
                f"No {self.names[0]} beside {exe.name}. A binary built before the code "
                "that writes it never produces one, so this absence means OLD BINARY "
                "or NEVER RAN, not 'the subsystem is silent'. Rebuild and re-run before "
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
        missing = []

        for root_name, base, folder, filenames in self._walk():
            for fn in filenames:
                if fn not in self.names:
                    continue
                path = folder / fn
                st = path.stat()
                found.append({
                    "name": path.name,
                    "path": f"{root_name}:{path.relative_to(base).as_posix()}",
                    "bytes": st.st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                    "age_s": round(now - st.st_mtime),
                    "lines": _count_lines(path),
                    **self._build_note(path, folder),
                })

            # Folders that hold a build but lack a configured log are the
            # interesting gap, and they would be invisible in a listing of
            # files that exist. Judged per NAME, not "any log found here":
            # a folder with a secondary log and no primary one is exactly
            # the built-before-the-change case a reader must not mistake for
            # a silent subsystem.
            if self.exe_names and any(e in filenames for e in self.exe_names):
                absent = [n for n in self.names if n not in filenames]
                if absent:
                    missing.append({
                        "folder": f"{root_name}:{folder.relative_to(base).as_posix()}",
                        "missing": absent,
                        **self._build_note(None, folder),
                    })

        found.sort(key=lambda r: r["age_s"])
        return {"names": self.names, "logs": found, "builds_with_missing_logs": missing}

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

        if candidate.name not in self.names:
            raise ValueError(
                f"'{candidate.name}' is not a log this tool serves. Readable: "
                f"{', '.join(self.names)}. Use bridge_read for source files."
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
                return {"error": f"no {' or '.join(self.names)} exists under any root yet",
                        "hint": "the program may not have run, or its build predates the log"}
            target = listing[0]["path"]

        path = self._resolve(target)
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(text)

        if level:
            want = level.strip().upper()
            text = [ln for ln in text if f"[{want}]" in ln]
        if contains:
            try:
                rx = compile_pattern(contains, ignore_case=True)
            except ValueError as e:
                return {"error": str(e)}
            # Caller-supplied regex over a log that can be hundreds of thousands
            # of lines: bounded, for the same reason bridge_grep is.
            deadline = Deadline(FILTER_TIMEOUT_S)
            try:
                text = [ln for ln in text if deadline.search(rx, ln)]
            except PatternTimeout:
                return {"error": f"'contains' filter timed out after {FILTER_TIMEOUT_S:g}s; "
                                 "simplify the pattern or read fewer lines"}

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
