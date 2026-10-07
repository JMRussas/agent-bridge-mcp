"""Keep every collected test in exactly one documented purpose group."""

import pytest


GROUPS = frozenset({
    "core", "cli", "http", "mcp", "evidence", "leases", "reports",
    "worker", "hygiene",
})


def pytest_collection_modifyitems(items):
    for item in items:
        groups = {marker.name for marker in item.iter_markers()} & GROUPS
        if len(groups) != 1:
            raise pytest.UsageError(
                f"{item.nodeid}: assign exactly one purpose marker from "
                f"{', '.join(sorted(GROUPS))} (found {sorted(groups)})"
            )
