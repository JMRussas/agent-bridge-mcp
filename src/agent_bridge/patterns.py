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


MAX_PATTERN_CHARS = 512
MAX_NESTING = 64
# The regex module unrolls counted repeats at compile time, and nested ones
# multiply: (a{1000}){1000} costs 235 MB and three seconds before any search
# happens, (a{60000}){60000} does not finish. Siblings do not multiply -
# (a|b){500}c{500} is 0.5 MB - so the guard estimates the unrolled size by
# following nesting rather than capping every bound. 20,000 admits anything a
# source grep would plausibly want ((a{100}){100} is 2.6 MB, 30 ms).
MAX_UNROLL = 20_000


def compile_pattern(pattern: str, ignore_case: bool = False):
    """Compile, or raise ValueError with a message fit to show the caller."""
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ValueError(f"pattern is {len(pattern)} characters; the limit is {MAX_PATTERN_CHARS}")
    cost = unroll_estimate(pattern)
    if cost > MAX_UNROLL:
        raise ValueError(
            f"pattern is too expensive to compile (estimated {cost} unrolled atoms, "
            f"limit {MAX_UNROLL}): nested counted repeats like (a{{1000}}){{1000}} "
            "multiply. Use + or * instead of large {n}."
        )
    try:
        return regex.compile(pattern, regex.IGNORECASE if ignore_case else 0)
    except regex.error as e:
        raise ValueError(f"bad regular expression: {e}") from None
    except (RecursionError, OverflowError, MemoryError):
        raise ValueError("pattern is too deeply nested or too large to compile") from None


_BOUND = regex.compile(r"\{(\d*)(?:,(\d*))?\}")


def unroll_estimate(pattern: str) -> int:
    """Roughly how many atoms the regex module will materialise for `pattern`.

    A literal, class or escape counts one; {n} or {n,m} multiplies the atom or
    group before it by the larger bound; * + ? and alternation do not unroll.
    Nesting deeper than MAX_NESTING is refused outright, because the compiler
    recurses per group and a thousand open parens is a RecursionError.
    """
    n = len(pattern)
    i = 0

    def group(depth: int) -> int:
        nonlocal i
        if depth > MAX_NESTING:
            raise ValueError(f"pattern nests more than {MAX_NESTING} groups deep")
        total = 0
        last = 0                          # cost of the most recent atom
        while i < n:
            c = pattern[i]
            if c == ")":
                i += 1
                return total
            if c == "(":
                i += 1
                last = group(depth + 1)
                total += last
            elif c == "\\":
                i += 2
                last = 1
                total += 1
            elif c == "[":
                j = i + 1
                if j < n and pattern[j] == "^":
                    j += 1
                if j < n and pattern[j] == "]":
                    j += 1
                while j < n and pattern[j] != "]":
                    j += 2 if pattern[j] == "\\" else 1
                i = j + 1
                last = 1
                total += 1
            elif c == "{":
                m = _BOUND.match(pattern, i)
                if m:
                    lo = int(m.group(1) or 0)
                    hi = int(m.group(2)) if m.group(2) else lo
                    k = max(lo, hi, 1)
                    i = m.end()
                    total += last * (k - 1)   # `last` was already counted once
                    last *= k
                else:
                    i += 1
                    last = 1
                    total += 1
            elif c in "*+?|":
                i += 1
            else:
                i += 1
                last = 1
                total += 1
            if total > MAX_UNROLL:
                return total               # no point being exact past the limit
        return total

    return group(0)


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
