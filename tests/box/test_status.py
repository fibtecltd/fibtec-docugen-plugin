from __future__ import annotations

import pytest

from docugen.box import status


@pytest.mark.parametrize("line,expected", [
    ("STATUS: STARTED", ("STARTED", None)),
    ("STATUS: INDEXED", ("INDEXED", None)),
    ("STATUS: INJECTION_SUSPECTED", ("INJECTION_SUSPECTED", None)),
    ("STATUS: DONE", ("DONE", None)),
    ("STATUS: LIMIT_REACHED", ("LIMIT_REACHED", None)),
    ("STATUS: FAILED VALIDATION", ("FAILED", "VALIDATION")),
    ("STATUS: FAILED NO_SOURCE", ("FAILED", "NO_SOURCE")),
    ("  STATUS: DONE \t\r", ("DONE", None)),
])
def test_valid_lines(line: str, expected: status.Status) -> None:
    assert status.parse(line) == expected


@pytest.mark.parametrize("line", [
    "",
    "STATUS:",
    "STATUS: ",
    "STATUS:DONE",
    "status: done",
    "Status: DONE",
    "STATUS: DONE.",
    "STATUS: DONE now",
    "STATUS:  DONE",
    "STATUS: FAILED",               # a code is required
    "STATUS: FAILED UNKNOWN",
    "STATUS: FAILED  VALIDATION",
    "STATUS: DONE VALIDATION",      # a code is only for FAILED
    "STATUS: DRAFTING BUDGET",
    "STATUS: DONE\nSTATUS: DONE",   # one line only
    "STATUS: DONE\x00",
    "STATUS: DONE ",
    "STATUS: DONE\x0b",
    " STATUS: DONE",           # an em space is not a space
    "STATUS: DОNE",            # Cyrillic O
    "STATUS: ＤONE",            # fullwidth D
    "`STATUS: DONE`",
    "**STATUS: DONE**",
    "> STATUS: DONE",
    "- STATUS: DONE",
    "ok STATUS: DONE",
    "STATUS: DONE or STATUS: FAILED INTERNAL",
])
def test_everything_else_is_not_a_status(line: str) -> None:
    assert status.parse(line) is None


def test_format_builds_the_line_from_the_vocabulary() -> None:
    assert status.format_status("DONE") == "STATUS: DONE"
    assert status.format_status("FAILED", "BUDGET") == "STATUS: FAILED BUDGET"
    for bad in [("DONE", "BUDGET"), ("FAILED", None), ("FAILED", "nope"), ("NOPE", None), ("done", None)]:
        with pytest.raises(ValueError):
            status.format_status(*bad)


def test_progress_and_terminal_words_partition_the_vocabulary() -> None:
    assert set(status.PROGRESS) | set(status.TERMINAL) == set(status.WORDS)
    assert not set(status.PROGRESS) & set(status.TERMINAL)
    assert len(set(status.WORDS)) == len(status.WORDS)
