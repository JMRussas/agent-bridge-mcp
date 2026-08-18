#
#  agent-bridge-mcp - Copyright(c) 2026
#

# Read-only access to the configured trees.
#
# Containment rule: a caller-supplied path is resolved and then required to sit
# under one of the resolved roots. Resolving BEFORE the check is what makes it
# hold - "..\..\Windows" and a junction pointing out of the tree both collapse to
# an absolute path that fails the comparison. Checking the string first and
# resolving after would pass both.

# Annotations are deferred because this class defines a method named `list`,
# which shadows the builtin inside the class body - so an eagerly evaluated
# `list[Path]` annotation resolves to the method and raises at import.
from __future__ import annotations

import os
import re
import shutil
import subprocess
from fnmatch import fnmatch
from pathlib import Path

# Anything here is either huge, binary, or someone's credentials. Skipped by
# list and grep, and refused by read.
DENY_PARTS = {".git", "node_modules", "obj", "bin", ".venv", "__pycache__", ".vs"}
DENY_NAMES = {".env", ".credentials.json", "config.json", "id_rsa", ".npmrc"}

# Only used by the Python grep fallback: art, binaries and compiled assets are
# most of the bytes in these trees and none of the answers.
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".pdf",
                   ".dll", ".exe", ".pdb", ".so", ".zip", ".7z", ".mp4", ".wav",
                   ".ogg", ".ttf", ".otf", ".rgba", ".pen", ".safetensors"}
MAX_SCAN_BYTES = 2 * 1024 * 1024


class PathDenied(Exception):
    pass


class Files:
    def __init__(self, roots: dict[str, Path], max_read_bytes: int = 256 * 1024):
        self.roots = roots
        self.max_read_bytes = max_read_bytes

    def resolve(self, path: str) -> Path:
        if not path or not path.strip():
            raise PathDenied("empty path")
        raw = path.strip().replace("\\", "/")

        # "root:sub/dir" is the preferred spelling - it says which tree you mean
        # without the caller needing to know where it lives on this disk.
        if ":" in raw and not raw[1:2] == ":":
            name, _, rest = raw.partition(":")
            base = self.roots.get(name.strip().lower())
            if base is None:
                raise PathDenied(f"unknown root '{name}'. Known: {', '.join(sorted(self.roots))}")
            target = (base / rest.lstrip("/")).resolve()
        else:
            target = Path(raw).resolve()

        for base in self.roots.values():
            if target == base or base in target.parents:
                break
        else:
            raise PathDenied(
                f"path is outside every configured root: {target}\n"
                f"Roots: {', '.join(f'{k} -> {v}' for k, v in self.roots.items())}"
            )

        if DENY_NAMES & {target.name} or DENY_PARTS & set(p.name for p in target.parents):
            raise PathDenied(f"path is on the deny list: {target}")
        return target

    def _skip(self, p: Path) -> bool:
        parts = set(p.parts)
        return bool(parts & DENY_PARTS) or p.name in DENY_NAMES

    def list(self, root: str = "", glob: str = "**/*", limit: int = 200) -> dict:
        bases = ([self.roots[root.lower()]] if root and root.lower() in self.roots
                 else list(self.roots.values()))
        hits, truncated = [], False
        for base in bases:
            for p in base.glob(glob):
                if not p.is_file() or self._skip(p.relative_to(base)):
                    continue
                if len(hits) >= limit:
                    truncated = True
                    break
                hits.append({
                    "path": f"{self._root_name(base)}:{p.relative_to(base).as_posix()}",
                    "bytes": p.stat().st_size,
                })
        return {"files": hits, "count": len(hits), "truncated": truncated}

    def _root_name(self, base: Path) -> str:
        for name, p in self.roots.items():
            if p == base:
                return name
        return base.name

    def read(self, path: str, start: int = 1, count: int = 0) -> dict:
        target = self.resolve(path)
        if not target.is_file():
            raise PathDenied(f"not a file: {target}")
        size = target.stat().st_size
        if size > self.max_read_bytes and count == 0:
            raise PathDenied(
                f"{target.name} is {size} bytes, over the {self.max_read_bytes} limit. "
                "Pass start/count to read a slice."
            )
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, start)
        chunk = lines[start - 1:(start - 1 + count) if count else None]
        return {
            "path": str(target),
            "total_lines": len(lines),
            "start": start,
            "returned": len(chunk),
            "text": "\n".join(f"{start + i}\t{ln}" for i, ln in enumerate(chunk)),
        }

    def grep(self, pattern: str, root: str = "", glob: str = "", limit: int = 100,
             context: int = 0, ignore_case: bool = False) -> dict:
        bases = ([self.roots[root.lower()]] if root and root.lower() in self.roots
                 else list(self.roots.values()))
        rg = shutil.which("rg")
        if not rg:
            # ripgrep is not on this machine's PATH - it ships inside editor
            # bundles, behind a versioned directory name that would rot. Scanning
            # in Python is slower and always there, which is the better trade for
            # two source trees.
            return self._grep_python(pattern, bases, glob, limit, context, ignore_case)

        args = [rg, "--line-number", "--no-heading", "--color", "never",
                "--max-count", "50", "--threads", "4"]
        if ignore_case:
            args.append("--ignore-case")
        if context:
            args += ["--context", str(int(context))]
        if glob:
            args += ["--glob", glob]
        for part in DENY_PARTS:
            args += ["--glob", f"!**/{part}/**"]
        args += ["--regexp", pattern, *[str(b) for b in bases]]

        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return {"error": "grep timed out after 60s", "matches": []}

        out = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        truncated = len(out) > limit
        return {"matches": out[:limit], "count": min(len(out), limit),
                "truncated": truncated, "pattern": pattern, "engine": "ripgrep"}

    def _grep_python(self, pattern: str, bases: list[Path], glob: str, limit: int,
                     context: int, ignore_case: bool) -> dict:
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            return {"error": f"bad regular expression: {e}", "matches": []}

        matches, truncated = [], False
        for base in bases:
            for path in self._walk(base):
                if len(matches) >= limit:
                    truncated = True
                    break
                rel = path.relative_to(base)
                if glob and not fnmatch(rel.as_posix(), glob):
                    continue
                try:
                    if path.stat().st_size > MAX_SCAN_BYTES:
                        continue
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue

                label = f"{self._root_name(base)}:{rel.as_posix()}"
                for i, line in enumerate(lines):
                    if not rx.search(line):
                        continue
                    if context:
                        lo, hi = max(0, i - context), min(len(lines), i + context + 1)
                        for j in range(lo, hi):
                            matches.append(f"{label}:{j + 1}:{lines[j]}")
                    else:
                        matches.append(f"{label}:{i + 1}:{line}")
                    if len(matches) >= limit:
                        truncated = True
                        break

        return {"matches": matches[:limit], "count": min(len(matches), limit),
                "truncated": truncated, "pattern": pattern, "engine": "python"}

    def _walk(self, base: Path):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in DENY_PARTS]
            for fn in filenames:
                if fn in DENY_NAMES or Path(fn).suffix.lower() in BINARY_SUFFIXES:
                    continue
                yield Path(dirpath) / fn
