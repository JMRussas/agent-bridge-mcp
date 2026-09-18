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
    "peers": {},
    "roots": {},
    "allowed_hosts": [],
    "max_read_bytes": 256 * 1024,
    "ripgrep_path": "",
    "inbox_max": 200,
    "mailbox_max_bytes": 4 * 1024 * 1024,
    "max_message_bytes": 64 * 1024,
    "mailbox_store": "mailbox.json",
    "mailbox_debounce_s": 0.25,
    "output_dir": "out",
    "exec": {"enabled": False, "timeout": 300, "commands": {}},
    "gifterboard": {"url": "", "creator": "", "token": "", "avatar_size": 64},
}


class Config:
    def __init__(self, data: dict):
        merged = {**DEFAULTS, **data}
        merged["exec"] = {**DEFAULTS["exec"], **data.get("exec", {})}
        merged["gifterboard"] = {**DEFAULTS["gifterboard"], **data.get("gifterboard", {})}
        # A null token is no token. Left as None it would slip past every
        # "is this a placeholder" check while authorised() treated it as open.
        merged["token"] = merged.get("token") or ""
        self._d = merged

        if not TOKEN_CHARS.match(merged["token"]):
            raise SystemExit(
                "token contains characters the WebSocket subprotocol grammar cannot "
                "carry. Use A-Z a-z 0-9 and any of - . _ ~ (a hex or base64url "
                "string is fine)."
            )

        self.peers = _peers(merged.get("peers") or {}, merged["self_name"], merged["token"])

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
        p = Path(path) if path else default_config_path()
        if not p.exists():
            raise SystemExit(
                f"config not found: {p}\n"
                "Run 'agent-bridge init' to write one with a generated token."
            )
        return cls(json.loads(p.read_text(encoding="utf-8")))


# Each peer is {"token": "..."} and may carry more later (S3 adds scopes).
# Names are normalised the way the mailbox normalises them, because the name
# IS the mailbox once identity comes from the credential. Refused outright:
# a blank or placeholder token, a token that is not a subprotocol string, a
# peer named after this machine or "*", and any two credentials that match -
# a shared token would make the first name in file order the winner, silently.
def _peers(raw: dict, self_name: str, admin_token: str) -> dict[str, dict]:
    from agent_bridge.auth import PLACEHOLDER_TOKENS, WILDCARD

    if not isinstance(raw, dict):
        raise SystemExit("peers must be a map of name -> {\"token\": ...}")
    peers: dict[str, dict] = {}
    seen = {admin_token} if admin_token not in PLACEHOLDER_TOKENS else set()
    for name, spec in raw.items():
        key = str(name).strip().lower()
        if not key or key in (WILDCARD, str(self_name).strip().lower()):
            raise SystemExit(f"peer name {name!r} is reserved (this machine is '{self_name}')")
        token = (spec or {}).get("token") if isinstance(spec, dict) else None
        if not isinstance(token, str) or token in PLACEHOLDER_TOKENS:
            raise SystemExit(f"peer '{key}' needs a real token: run 'agent-bridge peer add {key}'")
        if not TOKEN_CHARS.match(token):
            raise SystemExit(f"peer '{key}' has a token the WebSocket subprotocol grammar cannot carry")
        if token in seen:
            raise SystemExit(f"peer '{key}' shares its token with another credential; each must be unique")
        if key in peers:
            raise SystemExit(f"peer '{key}' is listed twice")
        seen.add(token)
        peers[key] = {**spec, "token": token}
    return peers


def default_config_path() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_CONFIG")
                or Path(__file__).resolve().parents[2] / "config.json")
