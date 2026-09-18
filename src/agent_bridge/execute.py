#
#  agent-bridge-mcp - Copyright(c) 2026
#

# Allowlisted command execution.
#
# The allowlist is keyed by NAME, not by prefix matching on a caller-supplied
# string. A remote agent asks for "viewers"; it never gets to compose a command
# line. Prefix matching is the version of this that looks equivalent and is not:
# "git log" as an allowed prefix also permits "git log; rm -rf".
#
# Nothing here runs through a shell (shell=False, argv list), so metacharacters
# in an extra argument are inert even before the character filter rejects them.

import asyncio
import os
import re
import shutil
from pathlib import Path

# Extra arguments exist so "autoplay" can take a duration. They are values, not
# flags or paths - anything richer is a sign the allowlist needs a new entry.
SAFE_ARG = re.compile(r"^[A-Za-z0-9._=-]{1,64}$")

# A child gets these from the server's environment and nothing else. The
# server's own environment is whatever shell started it, and on a developer
# box that is where the API keys live; a remote peer must not be able to read
# them back with an allowlisted "printenv". What is listed is what a build
# tool or git needs to locate itself, its config and its caches - remove one
# and dotnet or git fails in a way that does not name the missing variable.
PASSTHROUGH = (
    "PATH", "PATHEXT", "SystemRoot", "SystemDrive", "windir", "ComSpec",
    "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles", "ProgramFiles(x86)",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "LANG", "LC_ALL", "USER", "USERNAME",
)

# Set unconditionally: a quieter, non-interactive child.
FIXED_ENV = {
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_NOLOGO": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "NO_COLOR": "1",
}


def child_env(spec_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a child's environment: the passthrough set, the fixed opt-outs,
    then the command spec's own ``env`` map, which wins because the operator
    wrote it.

    Windows environment names are case-insensitive, so a spec ``Path`` must
    REPLACE the passthrough ``PATH`` rather than sit beside it - otherwise the
    child sees one value and ``which()`` below resolves against the other.
    """
    env = {k: os.environ[k] for k in PASSTHROUGH if k in os.environ}
    env.update(FIXED_ENV)
    for k, v in (spec_env or {}).items():
        if os.name == "nt":
            env = {ek: ev for ek, ev in env.items() if ek.upper() != k.upper()}
        env[k] = v
    return env


def spec_env(name: str, spec: dict) -> dict[str, str]:
    """The spec's ``env`` map, checked to be str -> str. config.json is not
    validated at load, so a wrong shape would otherwise surface as an
    AttributeError on the first run instead of a refusal naming the command."""
    raw = spec.get("env") or {}
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise ExecDenied(f"'{name}' has a malformed env in config.json: expected a map of string to string")
    return raw


class ExecDenied(Exception):
    pass


class Runner:
    def __init__(self, commands: dict, roots: dict[str, Path], enabled: bool, timeout: float = 300):
        self.commands = commands
        self.roots = roots
        self.enabled = enabled
        self.timeout = timeout

    def describe(self) -> list[dict]:
        return [
            {"name": name,
             "argv": " ".join(spec.get("argv", [])),
             "root": spec.get("root", "*"),
             "max_args": spec.get("max_args", 0)}
            for name, spec in sorted(self.commands.items())
        ]

    async def run(self, name: str, args: list[str] | None = None, root: str = "") -> dict:
        if not self.enabled:
            raise ExecDenied("command execution is disabled in config.json (exec.enabled)")

        spec = self.commands.get((name or "").strip())
        if spec is None:
            raise ExecDenied(
                f"'{name}' is not on the allowlist. Available: "
                f"{', '.join(sorted(self.commands)) or '(none)'}"
            )

        args = [a for a in (args or []) if a != ""]
        max_args = int(spec.get("max_args", 0))
        if len(args) > max_args:
            raise ExecDenied(f"'{name}' accepts at most {max_args} extra argument(s), got {len(args)}")
        for a in args:
            if not SAFE_ARG.match(a):
                raise ExecDenied(f"argument {a!r} rejected: allowed characters are A-Z a-z 0-9 . _ = -")

        want_root = spec.get("root", "*")
        chosen = root.strip().lower() or (want_root if want_root != "*" else "")
        if want_root != "*" and chosen != want_root:
            raise ExecDenied(f"'{name}' only runs in root '{want_root}'")
        cwd = self.roots.get(chosen)
        if cwd is None:
            raise ExecDenied(
                f"no root selected for '{name}'. Pass root= one of: {', '.join(sorted(self.roots))}"
            )

        argv = list(spec["argv"]) + args
        env = child_env(spec_env(name, spec))
        # Resolve against the PATH the child will see, not the server's, so a
        # spec that overrides PATH runs the binary it named.
        exe = shutil.which(argv[0], path=env.get("PATH"))
        if exe is None:
            raise ExecDenied(f"'{argv[0]}' is not on this machine's PATH")
        argv[0] = exe

        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            timed_out = True

        def tail(b: bytes, n: int = 8000) -> str:
            s = b.decode("utf-8", errors="replace")
            return s if len(s) <= n else "…[truncated]…\n" + s[-n:]

        return {
            "command": name,
            "argv": " ".join(argv),
            "cwd": str(cwd),
            "exit_code": None if timed_out else proc.returncode,
            "timed_out": timed_out,
            "stdout": tail(out),
            "stderr": tail(err),
        }
