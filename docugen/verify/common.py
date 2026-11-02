"""Shared types for the DocuGen validators.

The validators are the same code in two places: inside the box (so the agent can repair its own
output) and in the trusted deliver step (which never trusts the box's report). Standard library
only, so they run unchanged in the box image, in a Lambda and in CI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Finding:
    """One rule violation. ``detail`` never carries a secret value."""

    rule: str
    message: str
    path: str = ""
    line: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def __str__(self) -> str:
        where = self.path or "-"
        if self.line:
            where += f":{self.line}"
        return f"{where}: [{self.rule}] {self.message}"


def read_text_limited(path: str, max_bytes: int) -> str:
    """Read a file as UTF-8 text, refusing anything over ``max_bytes`` or not valid UTF-8."""
    with open(path, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"{path}: larger than {max_bytes} bytes")
    return data.decode("utf-8")
