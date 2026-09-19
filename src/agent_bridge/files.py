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

import asyncio
import os
import shutil
from fnmatch import fnmatch
from pathlib import Path

import anyio

from agent_bridge.patterns import Deadline, PatternTimeout, compile_pattern

# Anything here is either huge, binary, or someone's credentials. Skipped by
# list and grep, and refused by read.
DENY_PARTS = {".git", "node_modules", "obj", "bin", ".venv", "__pycache__", ".vs"}
# mailbox.json is every message this bridge has carried; a root that happens
# to contain the bridge's own checkout must not turn bridge_read into a way
# around the per-agent mailbox boundary.
DENY_NAMES = {".env", ".credentials.json", "config.json", "mailbox.json", "id_rsa", ".npmrc"}

# Only used by the Python grep fallback: art, binaries and compiled assets are
# most of the bytes in these trees and none of the answers.
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".pdf",
                   ".dll", ".exe", ".pdb", ".so", ".zip", ".7z", ".mp4", ".wav",
                   ".ogg", ".ttf", ".otf", ".rgba", ".pen", ".safetensors"}
MAX_SCAN_BYTES = 2 * 1024 * 1024
GREP_TIMEOUT_S = 20.0


class PathDenied(Exception):
    pass


def _skip_rest_of_line(f) -> None:
    while True:
        piece = f.readline(1 << 16)
        if not piece or piece.endswith(b"\n"):
            return


# Lines left in the file from the current position, at bytes.count speed. A
# trailing partial line (no final newline) is a line too, as splitlines has it.
def _count_remaining_lines(f) -> int:
    n = 0
    last = b""
    while True:
        buf = f.read(1 << 20)
        if not buf:
            break
        n += buf.count(b"\n")
        last = buf[-1:]
    return n + (1 if last and last != b"\n" else 0)


# The first `limit` bytes of a line, backed off to a character boundary so the
# cut does not end in a replacement glyph.
def _utf8_head(line: bytes, limit: int) -> str:
    head = line[:limit]
    while head and (head[-1] & 0xC0) == 0x80:
        head = head[:-1]
    if head and head[-1] >= 0xC0:           # a lead byte with no continuation
        head = head[:-1]
    return head.decode("utf-8", errors="replace")


class Files:
    def __init__(self, roots: dict[str, Path], max_read_bytes: int = 256 * 1024,
                 ripgrep: str = "", grep_timeout_s: float = GREP_TIMEOUT_S):
        self.roots = roots
        self.max_read_bytes = max_read_bytes
        # A path to rg, for the machine where it is not on PATH but is known to
        # sit inside some editor bundle. Empty means look on PATH.
        self.ripgrep = ripgrep
        self.grep_timeout_s = grep_timeout_s

    def engine(self) -> str:
        """Which grep will run, in words, for the startup log and whoami."""
        rg = shutil.which(self.ripgrep or "rg")
        if rg:
            return f"ripgrep ({rg})"
        if self.ripgrep:
            return (f"python scan - ripgrep_path {self.ripgrep!r} is not an executable "
                    "(fix it in config.json)")
        return "python scan (rg not on PATH; set ripgrep_path in config.json)"

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

    # Streams the file rather than loading it. The size cap used to apply only
    # when the whole file was asked for; any count>0 read the entire file into
    # memory and then sliced it, so a 2 GB file with count=1 was a 2 GB read.
    #
    # Binary, for three reasons found in review: the cap is then bytes rather
    # than code points (a CJK line is three bytes per character); a line longer
    # than the cap is read in bounded pieces instead of being buffered whole by
    # the text layer; and once the slice is complete the rest of the file is
    # counted with bytes.count, not decoded line by line.
    def read(self, path: str, start: int = 1, count: int = 0) -> dict:
        target = self.resolve(path)
        if not target.is_file():
            raise PathDenied(f"not a file: {target}")
        size = target.stat().st_size
        cap = self.max_read_bytes
        if size > cap and count == 0:
            raise PathDenied(
                f"{target.name} is {size} bytes, over the {cap} limit. "
                "Pass start/count to read a slice."
            )

        start = max(1, int(start))
        stop = start + count if count else None          # exclusive, 1-based
        chunk: list[str] = []
        used = 0
        truncated = False
        note = ""
        n = 0
        with target.open("rb") as f:
            while True:
                raw = f.readline(cap + 1)
                if not raw:
                    break
                n += 1
                # More than cap bytes and no newline: the line goes on. Skip the
                # rest of it without ever holding more than a buffer of it.
                cut = len(raw) > cap and not raw.endswith(b"\n")
                if cut:
                    _skip_rest_of_line(f)
                if n < start:
                    continue
                if stop is not None and n >= stop:
                    n += _count_remaining_lines(f)
                    break

                line = raw.rstrip(b"\r\n")
                cost = len(line) + 1
                if cut or used + cost > cap:
                    truncated = True
                    if chunk:
                        note = (f"stopped at {cap} bytes; the next line is {n}. "
                                "Ask for a smaller count.")
                    else:
                        # One line alone exceeds the cap. Return its head rather
                        # than nothing, or the caller can never read it at all.
                        chunk.append(_utf8_head(line, cap))
                        note = (f"line {n} is longer than the {cap}-byte limit; "
                                f"only its first {cap} bytes are shown.")
                    n += _count_remaining_lines(f)
                    break
                chunk.append(line.decode("utf-8", errors="replace"))
                used += cost

        out = {
            "path": str(target),
            "total_lines": n,
            "start": start,
            "returned": len(chunk),
            "text": "\n".join(f"{start + i}\t{ln}" for i, ln in enumerate(chunk)),
        }
        if truncated:
            out["truncated"] = True
            out["note"] = note
        return out

    # Async, because the MCP SDK calls a plain function inline on the event
    # loop. A grep that takes ten seconds used to stall the WebSocket, the
    # long-poll and every other session for those ten seconds. ripgrep now runs
    # as an awaited subprocess and the Python fallback in a worker thread.
    async def grep(self, pattern: str, root: str = "", glob: str = "", limit: int = 100,
                   context: int = 0, ignore_case: bool = False) -> dict:
        bases = ([self.roots[root.lower()]] if root and root.lower() in self.roots
                 else list(self.roots.values()))
        rg = shutil.which(self.ripgrep or "rg")
        if not rg:
            # ripgrep is not on this machine's PATH - it ships inside editor
            # bundles, behind a versioned directory name that would rot. Scanning
            # in Python is slower and always there, which is the better trade for
            # two source trees.
            return await anyio.to_thread.run_sync(
                self._grep_python, pattern, bases, glob, limit, context, ignore_case)

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

        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

        # Lines are collected as they arrive rather than through communicate(),
        # so a timeout returns what rg had already found instead of nothing.
        out: list[str] = []

        async def collect():
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.strip() and len(out) <= limit:
                    out.append(line)

        reader = asyncio.ensure_future(collect())
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.grep_timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
        await reader

        truncated = len(out) > limit or timed_out
        res = {"matches": out[:limit], "count": min(len(out), limit),
               "truncated": truncated, "pattern": pattern, "engine": "ripgrep"}
        if timed_out:
            res["reason"] = "timeout"
            res["note"] = (f"ripgrep stopped after {self.grep_timeout_s:g}s; these are the "
                           "matches it had found. Narrow the root or glob.")
        return res

    def _grep_python(self, pattern: str, bases: list[Path], glob: str, limit: int,
                     context: int, ignore_case: bool) -> dict:
        try:
            rx = compile_pattern(pattern, ignore_case)
        except ValueError as e:
            return {"error": str(e), "matches": []}

        # One budget for the whole request. It is checked between files, so a
        # big tree stops promptly, and passed into each search, so a pattern
        # that backtracks forever on one line stops too.
        deadline = Deadline(self.grep_timeout_s)
        matches, truncated, timed_out = [], False, False
        scanned = 0
        try:
            for base in bases:
                for path in self._walk(base):
                    if len(matches) >= limit:
                        truncated = True
                        break
                    if deadline.expired():
                        raise PatternTimeout()
                    rel = path.relative_to(base)
                    if glob and not fnmatch(rel.as_posix(), glob):
                        continue
                    try:
                        if path.stat().st_size > MAX_SCAN_BYTES:
                            continue
                        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    except OSError:
                        continue
                    scanned += 1

                    label = f"{self._root_name(base)}:{rel.as_posix()}"
                    for i, line in enumerate(lines):
                        if not deadline.search(rx, line):
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
        except PatternTimeout:
            truncated = timed_out = True

        out = {"matches": matches[:limit], "count": min(len(matches), limit),
               "truncated": truncated, "pattern": pattern, "engine": "python",
               "files_scanned": scanned}
        if timed_out:
            out["reason"] = "timeout"
            out["note"] = (f"stopped after {self.grep_timeout_s:g}s with {scanned} files "
                           "scanned. Narrow the root or glob, or simplify the pattern - "
                           "nested quantifiers like (a+)+ backtrack without bound.")
        return out

    def _walk(self, base: Path):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in DENY_PARTS]
            for fn in filenames:
                if fn in DENY_NAMES or Path(fn).suffix.lower() in BINARY_SUFFIXES:
                    continue
                yield Path(dirpath) / fn
