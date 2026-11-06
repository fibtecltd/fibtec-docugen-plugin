"""Allowlist check for the PDFs DocuGen prints from its own documents.

A PDF can run JavaScript, launch a program, open a URL, carry an attachment, and hide any of that in a compressed object
stream. DocuGen's PDFs are printed in the box from documents that have already passed the Markdown and SVG policies, but the
deliver step never trusts the box's report, so the bytes are checked again here. This is a fail-closed scanner, not a PDF
parser: it knows the plain structure a printer such as Chromium writes, and refuses a file it cannot read that way.

What it does:

- the file starts with a PDF header, ends with ``%%EOF``, and is no bigger than ``MAX_BYTES``;
- it is read as a sequence of structure and streams. Every stream must have a direct ``/Length``, ``endstream`` must follow
  exactly there, and neither ``endstream`` nor ``endobj`` may appear earlier inside the data: a viewer that ends a stream at
  the first ``endstream`` (many do) would otherwise read as structure what this scan took for data. The data of a stream is
  not scanned (it is compressed, and arbitrary bytes in it are not names);
- in the structure, every name is decoded (``/J#61vaScript`` is ``/JavaScript``) and none may be one of ``FORBIDDEN_NAMES``:
  scripts, launch and submit actions, attachments, forms, media, encryption, and object streams (which would hide names from
  a scan like this one);
- a ``/URI`` action must name an http or https address of at most ``MAX_URI_CHARS`` characters, with no spaces or control
  characters; internal links (``/Dest``) are fine;
- at most ``MAX_PAGES`` pages.

A finding never holds the content of a file, only the name of the rule and, for a URI, its first characters.
Standard library only.
"""

from __future__ import annotations

import re

from .common import Finding

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 2000
MAX_URI_CHARS = 2048
MAX_FINDINGS = 20

FORBIDDEN_NAMES = frozenset({
    "JavaScript", "JS", "OpenAction", "AA", "Launch", "SubmitForm", "ResetForm", "ImportData", "GoToR", "GoToE", "Rendition",
    "EmbeddedFile", "EmbeddedFiles", "FileAttachment", "RichMedia", "Movie", "Sound", "Screen", "3D",
    "AcroForm", "Widget", "XFA", "Encrypt", "Crypt", "ObjStm",
})

_HEADER = re.compile(rb"%PDF-[12]\.\d")
_NAME = re.compile(rb"/([^\x00\t\n\f\r ()<>\[\]{}/%]*)")
_HEX_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
_STREAM_KEYWORD = re.compile(rb"stream(?:\r\n|\n)")
_LENGTH = re.compile(rb"/Length[ \t\r\n]+(\d+)(?!\d)(?![ \t\r\n]+\d+[ \t\r\n]+R)")
_END_OF_STREAM = re.compile(rb"(?:\r\n|\n|\r)?endstream")
_PAGE = re.compile(rb"/Type[ \t\r\n]*/Page(?![A-Za-z0-9])")
_URI_SCHEME = re.compile(r"https?://", re.IGNORECASE)
_WHITESPACE = b" \t\r\n\f\x00"


def _decode_name(raw: bytes) -> str:
    return _HEX_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), raw).decode("latin-1")


def _split(data: bytes) -> tuple[list[bytes], str | None]:
    """The structure of the file as chunks (stream data removed), or the rule that says why it cannot be read that way."""
    chunks: list[bytes] = []
    pos = 0
    while True:
        keyword = _STREAM_KEYWORD.search(data, pos)
        if keyword is None:
            chunks.append(data[pos:])
            return chunks, None
        head = data[pos:keyword.start()]
        chunks.append(head)
        header = head[head.rfind(b"obj"):] if b"obj" in head else head
        lengths = _LENGTH.findall(header)
        if not lengths:
            return chunks, "pdf-stream-length"
        end = keyword.end() + int(lengths[-1])
        closing = _END_OF_STREAM.match(data, end)
        if closing is None or data.find(b"endstream", keyword.end(), end) >= 0 or data.find(b"endobj", keyword.end(), end) >= 0:
            return chunks, "pdf-stream-length"
        pos = closing.end()


def _string_after(chunk: bytes, pos: int) -> bytes | None:
    """The PDF string (literal or hexadecimal) that starts at or after ``pos``, decoded, or None if a name or dictionary does."""
    while pos < len(chunk) and chunk[pos] in _WHITESPACE:
        pos += 1
    if pos >= len(chunk):
        return None
    if chunk[pos:pos + 1] == b"(":
        out = bytearray()
        depth = 0
        i = pos
        while i < len(chunk):
            c = chunk[i:i + 1]
            if c == b"\\":
                nxt = chunk[i + 1:i + 2]
                if nxt and nxt in b"01234567":  # an octal escape: one to three digits
                    digits = re.match(rb"[0-7]{1,3}", chunk[i + 1:]).group(0)  # type: ignore[union-attr]
                    out.append(int(digits, 8) & 0xFF)
                    i += 1 + len(digits)
                    continue
                if nxt in (b"\r", b"\n"):  # a backslash before a line ending continues the line
                    i += 3 if chunk[i + 1:i + 3] == b"\r\n" else 2
                    continue
                out += {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}.get(nxt, nxt)
                i += 2
                continue
            if c == b"(":
                depth += 1
                if depth > 1:
                    out += c
            elif c == b")":
                depth -= 1
                if depth == 0:
                    return bytes(out)
                out += c
            else:
                out += c
            i += 1
        return b"\x00unterminated"
    if chunk[pos:pos + 1] == b"<" and chunk[pos + 1:pos + 2] != b"<":
        close = chunk.find(b">", pos)
        if close < 0:
            return b"\x00unterminated"
        digits = bytes(b for b in chunk[pos + 1:close] if b not in _WHITESPACE)
        if len(digits) % 2:
            digits += b"0"
        try:
            return bytes.fromhex(digits.decode("ascii"))
        except ValueError:
            return b"\x00unreadable"
    return None


def check_pdf(data: bytes, path: str = "") -> list[Finding]:
    """The findings for the bytes of one PDF; an empty list means it may be delivered."""
    findings: list[Finding] = []

    def add(rule: str, message: str) -> None:
        if len(findings) < MAX_FINDINGS:
            findings.append(Finding(rule, message, path))

    if len(data) > MAX_BYTES:
        return [Finding("pdf-too-large", f"PDF over {MAX_BYTES} bytes", path)]
    if not _HEADER.match(data):
        add("pdf-header", "not a PDF: the file does not start with a PDF header")
    if b"%%EOF" not in data[-1024:]:
        add("pdf-trailer", "no end-of-file marker at the end of the file")
    chunks, problem = _split(data)
    if problem:
        add(problem, "a stream has no direct length, or its length does not end where the stream does")
    pages = 0
    for chunk in chunks:
        pages += len(_PAGE.findall(chunk))
        for match in _NAME.finditer(chunk):
            name = _decode_name(match.group(1))
            if name in FORBIDDEN_NAMES:
                add("pdf-active-content", f"/{name} is not allowed in a PDF DocuGen delivers")
            elif name == "URI":
                value = _string_after(chunk, match.end())
                if value is None:
                    continue  # /S /URI names the kind of action; the address is the /URI entry that holds a string
                text = value.decode("latin-1")
                if len(text) > MAX_URI_CHARS or not _URI_SCHEME.match(text) or re.search(r"[\x00-\x20\x7f]", text):
                    add("pdf-uri", f"a link that is not an http or https address: {text[:40]!r}")
    if pages > MAX_PAGES:
        add("pdf-pages", f"more than {MAX_PAGES} pages")
    return findings
