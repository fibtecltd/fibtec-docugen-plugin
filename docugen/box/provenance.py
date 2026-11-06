"""The provenance manifest of a session's output: ``docs/.docugen/manifest.json``.

``docugen.verify.manifest`` defines what a valid manifest is and checks one. Nothing built one until this module: the
session skill tells the model to leave ``docs/.docugen/`` alone because the pipeline writes it, and this is the pipeline's
side, run by the box after the agent has finished.

Where each fact comes from, and why the model cannot change it:

- source hashes are computed here from the source tree, which is root-owned and read-only for the agent, and which the
  deliver step still holds in its own copy and re-hashes;
- document hashes are computed here from what the agent wrote, after it has stopped;
- the source (a commit, or the SHA-256 of the uploaded archive) and the tools are given by the caller, from the
  orchestrator's environment file and the image, never from the agent's text;
- which files a document was derived from is the one thing the agent says: each Markdown document ends with a
  ``## Sources`` section of repository paths, one per line. A path that is not a regular file in the source tree is not
  recorded (and is returned to the caller), so a manifest never claims a provenance the source does not have. A diagram
  and its rendering take the sources of the Markdown documents that embed them. A PDF takes the sources of the Markdown
  documents it was printed from, which the caller says (``pdf_documents``: the box knows what it printed); a PDF it does not
  mention takes the sources of all documents, never fewer than it may have been derived from.

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from docugen.verify.manifest import DOCUMENT_KINDS, MANIFEST_PATH, list_tree, sha256_file

GENERATOR_NAME = "fibtec-docugen"
_SOURCES_HEADING = re.compile(r"^##[ \t]+sources[ \t]*$", re.IGNORECASE)
_ANY_HEADING = re.compile(r"^#{1,6}[ \t]")
_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+")
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
_EMBED = re.compile(r"\]\(([^)\s#]+\.svg)(?:#[^)\s]*)?\)")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_CITED_PER_DOCUMENT = 500
_MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
_EXTENSION_KIND = {".md": "markdown", ".mmd": "diagram-source", ".puml": "diagram-source", ".svg": "diagram-svg", ".pdf": "pdf"}


@dataclass(frozen=True)
class SourceInfo:
    """What the documents were generated from, as the orchestrator knows it."""

    kind: str
    commit: str | None = None
    archive_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "github" and self.commit is not None and _COMMIT.fullmatch(self.commit) and self.archive_sha256 is None:
            return
        if self.kind == "zip" and self.archive_sha256 is not None and _SHA256.fullmatch(self.archive_sha256) and self.commit is None:
            return
        raise ValueError("a source is a github commit (40 hex characters) or a zip archive hash (64 hex characters)")

    @classmethod
    def from_environ(cls, environ: dict[str, str] | os._Environ[str]) -> SourceInfo:
        kind = environ.get("DOCUGEN_SOURCE_KIND", "")
        if kind == "github":
            return cls("github", commit=environ.get("DOCUGEN_SOURCE_COMMIT"))
        if kind == "zip":
            return cls("zip", archive_sha256=environ.get("DOCUGEN_SOURCE_ARCHIVE_SHA256"))
        raise ValueError("DOCUGEN_SOURCE_KIND must be github or zip")

    def as_manifest(self) -> dict[str, str]:
        if self.kind == "github":
            return {"kind": "github", "commit": str(self.commit)}
        return {"kind": "zip", "archive_sha256": str(self.archive_sha256)}


def cited_paths(markdown: str) -> list[str]:
    """The repository paths in the ``## Sources`` section of a Markdown document, in order, without duplicates.

    One path per line; a bullet, backticks and a ``:line`` suffix are tolerated. The last such section counts.
    """
    lines = markdown.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _SOURCES_HEADING.match(line.strip()):
            start = i + 1
    if start is None:
        return []
    out: list[str] = []
    for line in lines[start:]:
        text = line.strip()
        if _ANY_HEADING.match(text):
            break
        text = _LIST_MARKER.sub("", text).strip().strip("`").strip()
        text = _LINE_SUFFIX.sub("", text)
        if text and text not in out:
            out.append(text)
        if len(out) >= _MAX_CITED_PER_DOCUMENT:
            break
    return out


def _resolve_source(src_root: str, cited: str) -> str | None:
    """The cited path as a normalised relative path if it is a regular, non-symlink file inside ``src_root``."""
    if not cited or "\x00" in cited or "\\" in cited or cited.startswith("/"):
        return None
    parts = cited.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    full = os.path.join(src_root, *parts)
    try:
        st = os.lstat(full)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    root = os.path.realpath(src_root)
    if os.path.realpath(full) != os.path.join(root, *parts):  # a symlinked directory on the way
        return None
    return "/".join(parts)


def _embedded_svgs(markdown_path: str, markdown: str) -> set[str]:
    out: set[str] = set()
    base = os.path.dirname(markdown_path)
    for target in _EMBED.findall(markdown):
        if "://" in target or target.startswith("/"):
            continue
        out.add(os.path.normpath(os.path.join(base, target)).replace(os.sep, "/"))
    return out


def build_manifest(out_root: str, src_root: str, source: SourceInfo, *, generator_version: str,
                   tools: dict[str, str], now: datetime | None = None,
                   pdf_documents: Mapping[str, tuple[str, ...]] | None = None) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """The manifest of the documents under ``out_root/docs``, and the citations that could not be recorded.

    ``pdf_documents`` maps the path of a PDF (``docs/pdf/NAME.pdf``) to the Markdown documents it was printed from, as
    paths relative to ``docs/``.

    The second value lists ``(document, cited path)`` for every cited path that is not a regular file in the source tree.
    The manifest is neither serialised nor checked here: ``manifest_bytes`` serialises it, and
    ``docugen.verify.manifest.check_manifest`` checks it (the box does both before it packages anything).
    """
    files, _findings = list_tree(out_root)  # structural problems are check_tree's to report, not this function's
    documents: dict[str, dict[str, Any]] = {}
    markdown_text: dict[str, str] = {}
    unresolved: list[tuple[str, str]] = []
    for rel in files:
        if rel == MANIFEST_PATH or not rel.startswith("docs/"):
            continue
        kind = _EXTENSION_KIND.get(os.path.splitext(rel)[1].lower())
        if kind is None:
            continue
        full = os.path.join(out_root, rel)
        documents[rel] = {"path": rel, "kind": kind, "sha256": sha256_file(full), "sources": []}
        if kind == "markdown" and os.path.getsize(full) <= _MAX_DOCUMENT_BYTES:
            with open(full, encoding="utf-8", errors="replace") as fh:
                markdown_text[rel] = fh.read()

    sources_of: dict[str, list[str]] = {}
    for rel, text in markdown_text.items():
        resolved: list[str] = []
        for cited in cited_paths(text):
            path = _resolve_source(src_root, cited)
            if path is None:
                unresolved.append((rel, cited))
            elif path not in resolved:
                resolved.append(path)
        sources_of[rel] = sorted(resolved)

    inherited: dict[str, set[str]] = {}
    for rel, text in markdown_text.items():
        for svg in _embedded_svgs(rel, text):
            inherited.setdefault(svg, set()).update(sources_of[rel])

    hashes: dict[str, str] = {}

    def source_entries(paths: set[str] | list[str]) -> list[dict[str, str]]:
        entries = []
        for path in sorted(paths):
            if path not in hashes:
                hashes[path] = sha256_file(os.path.join(src_root, path))
            entries.append({"path": path, "sha256": hashes[path]})
        return entries

    every_source = {path for paths in sources_of.values() for path in paths}
    for rel, doc in documents.items():
        if doc["kind"] == "markdown":
            doc["sources"] = source_entries(sources_of.get(rel, []))
        elif doc["kind"] == "pdf":
            printed_from = (pdf_documents or {}).get(rel)
            doc["sources"] = source_entries(every_source if printed_from is None else
                                            {path for name in printed_from for path in sources_of.get("docs/" + name, [])})
        else:
            stem = os.path.splitext(rel)[0]
            doc["sources"] = source_entries(inherited.get(stem + ".svg", set()))

    assert all(d["kind"] in DOCUMENT_KINDS for d in documents.values())  # noqa: S101 - a table above is out of step
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    manifest = {
        "schema": 1,
        "generator": {"name": GENERATOR_NAME, "version": generator_version, "tools": dict(sorted(tools.items()))},
        "source": source.as_manifest(),
        "generated_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "documents": [documents[rel] for rel in sorted(documents)],
    }
    return manifest, unresolved


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """The manifest as it goes into the output archive: canonical JSON (sorted keys, one trailing newline).

    It is returned, not written. The box's trusted code may run as root, and the output directory belongs to the agent: a
    root process that created a file there would follow any link the agent had left in the way. The manifest goes
    straight into the archive from memory, and a manifest the agent left in the tree is never packed.
    """
    return (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8")
