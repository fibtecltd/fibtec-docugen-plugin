"""Test helpers. Secret-shaped strings are BUILT here, never written out in a file: a committed
token-looking literal trips push protection and every secret scanner that reads this repository."""

from __future__ import annotations

import string

_ALPHA = string.ascii_letters + string.digits


def fake(n: int, seed: int = 1, alphabet: str = _ALPHA) -> str:
    """A deterministic, varied string of length n (not a placeholder: more than two distinct characters)."""
    return "".join(alphabet[(i * 7 + seed * 13 + i * i) % len(alphabet)] for i in range(n))
