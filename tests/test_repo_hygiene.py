#
#  agent-bridge-mcp - Copyright(c) 2026
#

# Line endings. .gitattributes normalises to LF on commit, so the repository
# was always right - but every tool that wrote CRLF into the working copy
# (Python's write_text on Windows does, by default) produced a wall of
# "CRLF will be replaced by LF" warnings on the next commit and a diff that
# was nothing but line endings when a file was later touched by something
# else. This test makes the working copy match the policy, so the problem is
# caught where it is introduced instead of at commit time.

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.hygiene

REPO = Path(__file__).resolve().parents[1]
CRLF_SUFFIXES = {".ps1", ".psm1", ".bat", ".cmd"}      # .gitattributes: eol=crlf
BINARY_SUFFIXES = {".png", ".rgba", ".exe", ".dll", ".pyc"}


def _tracked() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
                             check=True, timeout=30).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("not a git checkout, or git is unavailable")
    return [REPO / rel for rel in out.decode("utf-8").split("\0") if rel]


def test_tracked_text_files_use_the_line_ending_the_policy_says():
    wrong = []
    for path in _tracked():
        if not path.is_file() or path.suffix in BINARY_SUFFIXES:
            continue
        data = path.read_bytes()
        if not data:
            continue
        want_crlf = path.suffix in CRLF_SUFFIXES
        has_crlf = b"\r\n" in data
        has_bare_lf = b"\n" in data.replace(b"\r\n", b"")
        if want_crlf and has_bare_lf or not want_crlf and has_crlf:
            wrong.append(path.relative_to(REPO).as_posix())
    assert not wrong, (
        "line endings do not match .gitattributes / .editorconfig for:\n  "
        + "\n  ".join(wrong)
        + "\n(a Python write_text on Windows writes CRLF; pass newline='\\n')"
    )
