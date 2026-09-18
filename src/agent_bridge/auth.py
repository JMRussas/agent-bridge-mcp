#
#  agent-bridge-mcp - Copyright(c) 2026
#

# Who is calling. A credential resolves to a Principal: a name and whether it
# is the admin. Nothing in a request body is trusted for identity - the sender
# of a message is whoever authenticated, and a peer may read only its own
# mailbox. The admin credential (the single "token") keeps the old semantics
# so a one-machine setup and an existing peer keep working unchanged.
#
# This module knows nothing about HTTP. server.py pulls the token out of the
# header (or the WebSocket subprotocol) and asks here what it means.

from __future__ import annotations

import hmac
from dataclasses import dataclass

# What a copied example config contains. Never accepted as a credential - a
# placeholder that authenticates is worse than none, because it looks set.
PLACEHOLDER_TOKENS = {"", "CHANGE_ME", "changeme", "change-me"}

WILDCARD = "*"


@dataclass(frozen=True)
class Principal:
    name: str
    admin: bool

    def may_read(self, mailbox: str) -> bool:
        """Whether this principal may consume or watch `mailbox`. A peer sees
        its own; the admin sees any, including the wildcard listener."""
        return self.admin or mailbox == self.name


class Forbidden(Exception):
    """Authenticated, but not for that mailbox."""


class Credentials:
    def __init__(self, self_name: str, admin_token: str, peers: dict[str, dict] | None = None):
        self.self_name = self_name
        self.admin_token = "" if admin_token in PLACEHOLDER_TOKENS else admin_token
        self.peer_tokens: dict[str, str] = {
            name: spec["token"] for name, spec in (peers or {}).items()
        }

    @property
    def open(self) -> bool:
        """No credential of any kind: every caller is the admin. Only
        permissible on loopback; refuse_open_bind() enforces that."""
        return not self.admin_token and not self.peer_tokens

    def identify(self, supplied: str) -> Principal | None:
        if self.open:
            return Principal(self.self_name, admin=True)
        if not supplied:
            return None
        # compare_digest on every candidate, not an early-exit dict lookup, so
        # the response time does not say which name a guessed token was close to.
        if self.admin_token and hmac.compare_digest(supplied, self.admin_token):
            return Principal(self.self_name, admin=True)
        for name, token in self.peer_tokens.items():
            if hmac.compare_digest(supplied, token):
                return Principal(name, admin=False)
        return None

    def mailbox_for(self, who: Principal, requested: str) -> str:
        """The mailbox a request is about: the caller's own by default. Raises
        Forbidden when a peer names someone else's, or the wildcard."""
        agent = (requested or "").strip().lower() or who.name
        if not who.may_read(agent):
            what = "every mailbox" if agent == WILDCARD else f"the mailbox of '{agent}'"
            raise Forbidden(f"'{who.name}' may not read {what}; only its own")
        return agent
