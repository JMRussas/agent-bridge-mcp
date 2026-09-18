#
#  agent-bridge-mcp - Copyright(c) 2026
#

# Caller-supplied regular expressions, bounded in time.
#
# The standard library's `re` backtracks without limit: "(a|a)*$" against
# twenty-eight a's takes half a minute, and against a real source line it does
# not finish. A remote peer supplies the pattern, so that is a one-request
# denial of service against the fallback grep and the log filter. The `regex`
# module accepts a timeout per search, and its engine also short-circuits the
# common catastrophic shapes outright. ripgrep, when present, is a finite
# automaton and needs none of this.

from __future__ import annotations

import time

import regex


class PatternTimeout(Exception):
    pass


def compile_pattern(pattern: str, ignore_case: bool = False):
    """Compile, or raise ValueError with a message fit to show the caller."""
    try:
        return regex.compile(pattern, regex.IGNORECASE if ignore_case else 0)
    except regex.error as e:
        raise ValueError(f"bad regular expression: {e}") from None


class Deadline:
    """A wall-clock budget shared across every search in one request."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self._until = time.monotonic() + seconds

    def remaining(self) -> float:
        return self._until - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def search(self, rx, line: str):
        left = self.remaining()
        if left <= 0:
            raise PatternTimeout()
        try:
            return rx.search(line, timeout=left)
        except TimeoutError:
            raise PatternTimeout() from None
