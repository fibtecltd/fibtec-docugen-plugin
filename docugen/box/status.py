"""The status vocabulary of a DocuGen session: the only text that leaves the box.

The agent reports progress with lines such as ``STATUS: DRAFTING``. This module is the single definition of
what such a line may say. ``skills/docugen-session/SKILL.md`` lists the same words for the model, and a test
requires the two to agree.

A line is accepted only if, after trimming spaces, tabs and a carriage return, it is exactly ``STATUS: `` and
one word, and for ``FAILED`` one reason code. Nothing is ever echoed from the agent's text: the supervisor
prints the line it builds from these constants, so a hostile byte sequence cannot reach the logs through a
line that almost matches.
"""

from __future__ import annotations

import re

PREFIX = "STATUS: "

# Words the agent may report while it works. The supervisor passes these on.
PROGRESS = ("STARTED", "INDEXED", "DRAFTING", "VALIDATING", "REPAIRING", "PACKAGING", "INJECTION_SUSPECTED")
# Words that end a session. Only the supervisor prints them, after reading how the session really ended:
# the agent can claim DONE, and cannot make it true.
TERMINAL = ("DONE", "FAILED", "LIMIT_REACHED")
# The order of the skill's list.
WORDS = ("STARTED", "INDEXED", "DRAFTING", "VALIDATING", "REPAIRING", "PACKAGING", "DONE", "FAILED",
         "INJECTION_SUSPECTED", "LIMIT_REACHED")
FAILURE_CODES = ("NO_SOURCE", "TOO_LARGE", "VALIDATION", "BUDGET", "DENIED", "INTERNAL")

assert set(WORDS) == set(PROGRESS) | set(TERMINAL)  # noqa: S101 - a definition error, caught at import

_LINE = re.compile(
    PREFIX + r"(" + "|".join(WORDS) + r")(?: (" + "|".join(FAILURE_CODES) + r"))?",
    re.ASCII,
)

Status = tuple[str, "str | None"]


def parse(line: str) -> Status | None:
    """``(word, code)`` for a valid status line, else ``None``. A code is allowed after FAILED, and required."""
    m = _LINE.fullmatch(line.strip(" \t\r"))
    if not m:
        return None
    word, code = m.group(1), m.group(2)
    if (word == "FAILED") != (code is not None):
        return None
    return word, code


def format_status(word: str, code: str | None = None) -> str:
    """The line to print. Raises ``ValueError`` for anything outside the vocabulary."""
    if word not in WORDS or (word == "FAILED") != (code is not None) or (code is not None and code not in FAILURE_CODES):
        raise ValueError(f"not a status: {word!r} {code!r}")
    return PREFIX + word + (f" {code}" if code else "")
