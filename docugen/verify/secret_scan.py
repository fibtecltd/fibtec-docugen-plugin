"""Find and redact secrets in text.

Used twice: at ingest (redact before anything is sent to a model) and at deliver (fail when anything
secret-shaped is in the output, whatever the box reported). A finding never carries the secret: the
message shows a four-character prefix and the length at most.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

from .common import Finding

# (rule, pattern). Patterns are specific on purpose: a noisy scanner gets ignored.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pat)) for name, pat in (
        ("aws-access-key-id", r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
        ("aws-secret-access-key", r"(?i)aws.{0,24}(?:secret|sk).{0,24}['\"=:\s]([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])"),
        ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
        ("anthropic-api-key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
        ("openai-api-key", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
        ("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
        ("stripe-secret-key", r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
        ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}"),
        ("private-key-block", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
        ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        ("url-credentials", r"\b[a-z][a-z0-9+.-]*://[^\s/:@'\"]{1,64}:[^\s/@'\"]{3,128}@[^\s/'\"]+"),
        ("npm-token", r"\bnpm_[A-Za-z0-9]{36}\b"),
        ("pypi-token", r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,}"),
        ("sendgrid-key", r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),
        ("azure-storage-key", r"(?i)AccountKey=[A-Za-z0-9+/=]{40,}"),
        ("generic-assignment",
         r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)"
         r"\b\s*[:=]\s*['\"]([^'\"\s]{12,})['\"]"),
    )
)

# Values that are placeholders, not secrets: never reported.
_PLACEHOLDER = re.compile(r"(?i)example|changeme|placeholder|your[-_ ]|xxxx|<.*>|\$\{|\{\{|%\(|\.\.\.|dummy|redacted")
_MAX_LINE = 200_000


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.search(value)) or len(set(value)) <= 2 or value[:1] in "$%{<"


def _matches(line: str) -> Iterator[tuple[str, int, int]]:
    for rule, rx in RULES:
        for m in rx.finditer(line):
            value = m.group(1) if m.groups() and m.group(1) else m.group(0)
            if rule != "private-key-block" and (_is_placeholder(value) or _is_placeholder(m.group(0))):
                continue
            yield rule, m.start(), m.end()


def _preview(text: str) -> str:
    return f"{text[:4]}... ({len(text)} characters)"


def scan_text(text: str, path: str = "") -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rule, start, end in _matches(line[:_MAX_LINE]):
            findings.append(Finding(rule, f"secret-shaped value {_preview(line[start:end])}", path, lineno))
    return findings


def redact_text(text: str) -> tuple[str, list[Finding]]:
    """Replace every match with ``[REDACTED:<rule>]``; return the new text and what was found."""
    findings: list[Finding] = []
    out: list[str] = []
    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        spans = sorted({(s, e, r) for r, s, e in _matches(line[:_MAX_LINE])})
        if not spans:
            out.append(line)
            continue
        pos = 0
        pieces: list[str] = []
        for start, end, rule in spans:
            if start < pos:  # overlaps an earlier match on this line
                continue
            pieces.append(line[pos:start])
            pieces.append(f"[REDACTED:{rule}]")
            findings.append(Finding(rule, f"redacted {_preview(line[start:end])}", "", lineno))
            pos = end
        pieces.append(line[pos:])
        out.append("".join(pieces))
    return "".join(out), findings


def scan_tree(root: str, max_file_bytes: int = 5 * 1024 * 1024) -> list[Finding]:
    """Scan every regular file under ``root`` (symlinks are not followed)."""
    findings: list[Finding] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            with open(full, "rb") as fh:
                data = fh.read(max_file_bytes + 1)
            if len(data) > max_file_bytes:
                findings.append(Finding("file-too-large", "file over the scan limit", rel))
                continue
            findings.extend(scan_text(data.decode("utf-8", errors="replace"), rel))
    return findings
