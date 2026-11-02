"""Policy for the Markdown DocuGen delivers into a customer's repository or zip.

The output of a model that has read hostile text is itself hostile until checked. Markdown renders
on GitHub and in other viewers, so what it may contain is limited to what is inert there:

- no raw HTML except a few formatting tags without attributes (``<script>``, ``<iframe>``, event
  handlers and ``style`` have no way in);
- no remote images (a tracking or exfiltration beacon: the URL can carry data);
- links only to ``https:``/``http:`` URLs or relative paths that stay inside the repository; never
  ``javascript:``, ``data:``, ``file:``, ``vbscript:`` or protocol-relative ``//`` URLs, whatever the
  spelling (whitespace, control characters, percent- and entity-encoding are removed before the check);
- images only as relative paths inside ``docs/`` with an image extension;
- no bidirectional-control or NUL characters, and no zero-width spaces (text that hides from review).

Code (fenced blocks and inline code spans) is inert and is not link- or HTML-checked.
"""

from __future__ import annotations

import html
import posixpath
import re
from urllib.parse import unquote

from .common import Finding

MAX_BYTES = 2 * 1024 * 1024
ALLOWED_TAGS = frozenset({"br", "hr", "details", "summary", "sub", "sup", "kbd"})
IMAGE_EXTENSIONS = (".svg", ".png", ".jpg", ".jpeg", ".gif")
ALLOWED_LINK_SCHEMES = ("http", "https")
DOCS_ROOT = "docs"

_BIDI = re.compile("[‪-‮⁦-⁩\x00]")
_HIDDEN = re.compile("[​⁠]|(?<!^)﻿")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*)>")
_TAG = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9-]*)\b([^>]*)>")
_DECL = re.compile(r"<![A-Za-z\[]|<\?")
_INLINE_LINK = re.compile(r"(!?)\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]*)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_REF_DEF = re.compile(r"^ {0,3}\[([^\]]+)\]:\s*<?(\S+?)>?(?:\s+.*)?$")
_REF_IMAGE = re.compile(r"!\[[^\]]*\]\[([^\]]*)\]")
_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*):")


def _normalise_url(url: str) -> str:
    """Undo the tricks that hide a scheme: entities, percent-encoding, whitespace, control chars."""
    for _ in range(3):
        url = unquote(html.unescape(url))
    return re.sub(r"[\x00-\x20\x7f​⁠﻿]+", "", url)


def check_url(url: str, *, image: bool, doc_path: str) -> str | None:
    """Return the rule a URL breaks, or None. ``doc_path`` is the document's repo-relative path."""
    raw = url
    url = _normalise_url(url)
    if not url or url.startswith("#"):
        return None if not image else "image-not-relative"
    if "\\" in raw or "\\" in url:
        return "link-backslash"
    if url.startswith("//"):
        return "link-protocol-relative"
    m = _SCHEME.match(url.lower())
    if m:
        if image:
            return "image-remote"
        return None if m.group(1) in ALLOWED_LINK_SCHEMES else "link-scheme"
    path = url.split("#", 1)[0].split("?", 1)[0]
    if path.startswith("/"):
        return "link-absolute-path"
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(doc_path), path))
    if resolved == ".." or resolved.startswith("../"):
        return "link-escapes-repository"
    if image:
        if not resolved.startswith(DOCS_ROOT + "/"):
            return "image-outside-docs"
        if not resolved.lower().endswith(IMAGE_EXTENSIONS):
            return "image-extension"
    return None


def check_markdown(text: str, doc_path: str = "docs/doc.md") -> list[Finding]:
    findings: list[Finding] = []
    if len(text.encode("utf-8", errors="replace")) > MAX_BYTES:
        return [Finding("too-large", f"document over {MAX_BYTES} bytes", doc_path)]

    def add(rule: str, message: str, line: int) -> None:
        findings.append(Finding(rule, message, doc_path, line))

    fence: str | None = None
    ref_defs: dict[str, tuple[str, int]] = {}
    image_refs: set[str] = set()
    for n, line in enumerate(text.splitlines(), 1):
        if _BIDI.search(line):
            add("bidi-control", "bidirectional control or NUL character", n)
        if _HIDDEN.search(line):
            add("hidden-character", "zero-width space or word joiner", n)
        m = _FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
            continue
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and line.strip(" `~") == "":
                fence = None
            continue

        prose = _CODE_SPAN.sub("", line)
        for am in _AUTOLINK.finditer(prose):
            rule = check_url(am.group(1), image=False, doc_path=doc_path)
            if rule:
                add(rule, f"autolink to {am.group(1)[:40]!r}", n)
        prose = _AUTOLINK.sub("", prose)
        if _DECL.search(prose):
            add("html-declaration", "HTML declaration, CDATA or processing instruction", n)
        # HTML comments are inert; drop them before looking for tags.
        no_comments = re.sub(r"<!--.*?-->", "", prose)
        reported: set[str] = set()
        for tm in _TAG.finditer(no_comments):
            name = tm.group(2).lower()
            if name in reported:
                continue  # <script> and </script> on one line are one finding
            if name not in ALLOWED_TAGS:
                reported.add(name)
                add("html-tag", f"HTML tag <{name}> is not allowed", n)
            elif tm.group(3).strip(" /"):
                add("html-attribute", f"attributes on <{name}> are not allowed", n)
        for lm in _INLINE_LINK.finditer(prose):
            is_image = lm.group(1) == "!"
            rule = check_url(lm.group(2), image=is_image, doc_path=doc_path)
            if rule:
                add(rule, f"{'image' if is_image else 'link'} to {lm.group(2)[:40]!r}", n)
        for im in _REF_IMAGE.finditer(prose):
            image_refs.add(im.group(1).strip().lower())
        dm = _REF_DEF.match(prose)
        if dm:
            ref_defs[dm.group(1).strip().lower()] = (dm.group(2), n)

    for key, (url, n) in ref_defs.items():
        rule = check_url(url, image=key in image_refs, doc_path=doc_path)
        if rule:
            add(rule, f"reference [{key[:30]}] points to {url[:40]!r}", n)
    return findings
