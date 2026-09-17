#
#  agent-bridge-mcp - Copyright(c) 2026
#

# Configuration is a file, not environment variables, because this server is
# started by a service wrapper rather than by Claude Code - there is no parent
# process to inherit an environment from.

import json
import os
import re
from pathlib import Path

# RFC 6455 subprotocol names are HTTP tokens. The WebSocket carries the bearer
# token as one, so a token outside this set would fail at connect time with
# nothing pointing at the cause. Refuse it at load instead.
TOKEN_CHARS = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]*$")

DEFAULTS = {
    "self_name": "fenrir",
    "host": "0.0.0.0",
    "port": 8791,
    "token": "",
    "roots": {},
    "allowed_hosts": [],
    "max_read_bytes": 256 * 1024,
    "inbox_max": 200,
    "mailbox_store": "mailbox.json",
    "output_dir": "out",
    "exec": {"enabled": False, "timeout": 300, "commands": {}},
    "gifterboard": {"url": "", "creator": "", "token": "", "avatar_size": 64},
}


class Config:
    def __init__(self, data: dict):
        merged = {**DEFAULTS, **data}
        merged["exec"] = {**DEFAULTS["exec"], **data.get("exec", {})}
        merged["gifterboard"] = {**DEFAULTS["gifterboard"], **data.get("gifterboard", {})}
        self._d = merged

        if not TOKEN_CHARS.match(merged["token"] or ""):
            raise SystemExit(
                "token contains characters the WebSocket subprotocol grammar cannot "
                "carry. Use A-Z a-z 0-9 and any of - . _ ~ (a hex or base64url "
                "string is fine)."
            )

        # Roots are resolved once, at load. Every later path check compares
        # against these resolved absolutes, so a symlink or a "..\" in a request
        # cannot widen the exposed surface after the fact.
        self.roots: dict[str, Path] = {
            name: Path(p).resolve() for name, p in merged["roots"].items()
        }

    def __getattr__(self, name):
        try:
            return self._d[name]
        except KeyError as e:
            raise AttributeError(name) from e

    @property
    def exec_enabled(self) -> bool:
        return bool(self._d["exec"].get("enabled"))

    @property
    def commands(self) -> dict:
        return self._d["exec"].get("commands", {})

    @property
    def exec_timeout(self) -> float:
        return float(self._d["exec"].get("timeout", 300))

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        p = Path(path or os.environ.get("AGENT_BRIDGE_CONFIG")
                 or Path(__file__).resolve().parents[2] / "config.json")
        if not p.exists():
            raise SystemExit(
                f"config not found: {p}\n"
                "Copy config.example.json to config.json and set a token."
            )
        return cls(json.loads(p.read_text(encoding="utf-8")))
