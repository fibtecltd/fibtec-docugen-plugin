"""The shape of what DocuGen delivers, and its provenance manifest.

``check_tree`` is the allowlist for the output directory the box hands back: only regular files under
``docs/``, with a fixed set of extensions, no symlinks, no executable bits, no hidden files except the
manifest, plain ASCII path characters, and bounded count and size. Anything else means the box wrote
somewhere it should not, or produced a file type the product does not deliver.

PDFs are the one file type the agent never writes: the box prints them itself, after the agent has stopped, when the session
asked for them. So ``check_tree`` refuses a ``.pdf`` unless it is called with ``allow_pdf=True``, and then accepts one only
directly under ``docs/pdf/`` and only if ``pdf_check`` finds nothing in its bytes.

``check_manifest`` validates ``docs/.docugen/manifest.json``: the commit or archive the documents were
generated from, the tools used, and for each document the hash of the document and of every source file
it was derived from. Given the source tree it also verifies those source hashes, so a manifest cannot
claim a provenance the source does not have.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat

from . import pdf_check
from .common import Finding

MANIFEST_PATH = "docs/.docugen/manifest.json"
ALLOWED_EXTENSIONS = (".md", ".mmd", ".puml", ".svg")
PDF_EXTENSION = ".pdf"
PDF_DIR = "docs/pdf/"
MAX_FILES = 2000
MAX_PDFS = 100
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = pdf_check.MAX_BYTES
MAX_PATH_CHARS = 200
_SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*(?:/[A-Za-z0-9][A-Za-z0-9._ -]*)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
DOCUMENT_KINDS = ("markdown", "diagram-source", "diagram-svg", "pdf")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_tree(root: str) -> tuple[list[str], list[Finding]]:
    """Return the repo-relative paths of the regular files under ``root`` and any structural findings."""
    findings: list[Finding] = []
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                findings.append(Finding("symlink", "symbolic link in the output", rel))
            elif stat.S_ISDIR(st.st_mode):
                continue
            elif not stat.S_ISREG(st.st_mode):
                findings.append(Finding("special-file", "not a regular file", rel))
            else:
                files.append(rel)
                if st.st_mode & 0o111:
                    findings.append(Finding("executable", "executable bit set", rel))
                if st.st_nlink > 1:
                    findings.append(Finding("hard-link", "file has more than one link", rel))
                limit = MAX_PDF_BYTES if rel.lower().endswith(PDF_EXTENSION) else MAX_FILE_BYTES
                if st.st_size > limit:
                    findings.append(Finding("file-too-large", f"file over {limit} bytes", rel))
    return files, findings


def is_pdf_path(rel: str) -> bool:
    """Whether ``rel`` (a normalised relative path) is where a PDF may be: a ``.pdf`` directly under ``docs/pdf/``."""
    return rel.lower().endswith(PDF_EXTENSION) and rel.startswith(PDF_DIR) and "/" not in rel[len(PDF_DIR):]


def _read_pdf(root: str, rel: str) -> bytes | None:
    """The bytes of a PDF of the tree, read without following a link, or None if it cannot be read that way."""
    try:
        fd = os.open(os.path.join(root, rel), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        return fh.read(MAX_PDF_BYTES + 1)


def check_tree(root: str, *, allow_pdf: bool = False) -> list[Finding]:
    files, findings = list_tree(root)
    if len(files) > MAX_FILES:
        findings.append(Finding("too-many-files", f"more than {MAX_FILES} files"))
    seen: dict[str, str] = {}
    pdfs = 0
    for rel in files:
        if rel == MANIFEST_PATH:
            continue
        if not rel.startswith("docs/"):
            findings.append(Finding("outside-docs", "output is only accepted under docs/", rel))
            continue
        if len(rel) > MAX_PATH_CHARS or not _SAFE_PATH.match(rel):
            findings.append(Finding("path-characters", "path has characters outside A-Za-z0-9._ -/", rel))
            continue
        if allow_pdf and rel.lower().endswith(PDF_EXTENSION):
            pdfs += 1
            if not is_pdf_path(rel):
                findings.append(Finding("pdf-location", f"PDFs are accepted only directly under {PDF_DIR}", rel))
            elif pdfs <= MAX_PDFS:
                data = _read_pdf(root, rel)
                if data is None:
                    findings.append(Finding("pdf-unreadable", "the PDF cannot be read as a regular file", rel))
                else:
                    findings.extend(pdf_check.check_pdf(data, rel))
        elif not rel.lower().endswith(ALLOWED_EXTENSIONS):
            findings.append(Finding("extension", f"extension not delivered: {os.path.splitext(rel)[1]!r}", rel))
        key = rel.casefold()
        if key in seen:
            findings.append(Finding("duplicate-path", f"differs only by case from {seen[key]}", rel))
        seen[key] = rel
    if pdfs > MAX_PDFS:
        findings.append(Finding("too-many-pdfs", f"more than {MAX_PDFS} PDFs"))
    return findings


def _is_rel_path(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= MAX_PATH_CHARS and not value.startswith("/")
            and ".." not in value.split("/") and "\\" not in value and "\x00" not in value)


def check_manifest(manifest: object, root: str | None = None, source_root: str | None = None, *,
                   allow_pdf: bool = False) -> list[Finding]:
    """Validate a parsed manifest. With ``root`` it also checks the documents on disk; with
    ``source_root`` it also checks the hashes of the source files. A document of kind ``pdf`` is accepted only with
    ``allow_pdf``, and a PDF only is of kind ``pdf``: the kind and the extension must agree."""
    p = MANIFEST_PATH
    out: list[Finding] = []

    def bad(rule: str, msg: str) -> None:
        out.append(Finding(rule, msg, p))

    if not isinstance(manifest, dict):
        return [Finding("manifest-type", "the manifest must be a JSON object", p)]
    if manifest.get("schema") != 1:
        bad("manifest-schema", "schema must be 1")
    gen = manifest.get("generator")
    if not (isinstance(gen, dict) and isinstance(gen.get("name"), str) and isinstance(gen.get("version"), str)
            and isinstance(gen.get("tools", {}), dict)):
        bad("manifest-generator", "generator needs name, version and tools")
    src = manifest.get("source")
    if not isinstance(src, dict) or src.get("kind") not in ("github", "zip"):
        bad("manifest-source", "source.kind must be 'github' or 'zip'")
    else:
        if src["kind"] == "github" and not _COMMIT.match(str(src.get("commit", ""))):
            bad("manifest-source", "a github source needs a 40-character commit")
        if src["kind"] == "zip" and not _SHA256.match(str(src.get("archive_sha256", ""))):
            bad("manifest-source", "a zip source needs archive_sha256")
    if not _RFC3339.match(str(manifest.get("generated_at", ""))):
        bad("manifest-time", "generated_at must be an RFC 3339 timestamp")
    docs = manifest.get("documents")
    if not isinstance(docs, list) or not docs:
        bad("manifest-documents", "documents must be a non-empty list")
        return out
    listed: set[str] = set()
    for i, d in enumerate(docs):
        where = f"documents[{i}]"
        if not isinstance(d, dict) or not _is_rel_path(d.get("path")) or not str(d["path"]).startswith("docs/"):
            bad("manifest-document", f"{where}.path must be a path under docs/")
            continue
        path = str(d["path"])
        if path in listed:
            bad("manifest-document", f"{where}: {path} listed twice")
        listed.add(path)
        if d.get("kind") not in DOCUMENT_KINDS:
            bad("manifest-document", f"{where}.kind must be one of {DOCUMENT_KINDS}")
        elif d["kind"] == "pdf" and not allow_pdf:
            bad("manifest-pdf", f"{where}: a PDF is listed but none was asked for")
        if (d.get("kind") == "pdf") != path.lower().endswith(PDF_EXTENSION):
            bad("manifest-pdf", f"{where}: kind and extension disagree ({path})")
        if not _SHA256.match(str(d.get("sha256", ""))):
            bad("manifest-document", f"{where}.sha256 must be 64 hex characters")
        sources = d.get("sources")
        if not isinstance(sources, list):
            bad("manifest-document", f"{where}.sources must be a list")
            continue
        for j, s in enumerate(sources):
            if not (isinstance(s, dict) and _is_rel_path(s.get("path")) and _SHA256.match(str(s.get("sha256", "")))):
                bad("manifest-source-file", f"{where}.sources[{j}] needs a relative path and sha256")
                continue
            if source_root is not None:
                full = os.path.join(source_root, str(s["path"]))
                if not os.path.isfile(full) or os.path.islink(full):
                    bad("manifest-source-missing", f"{where}: source {s['path']} is not in the source tree")
                elif sha256_file(full) != s["sha256"]:
                    bad("manifest-source-hash", f"{where}: source {s['path']} does not match its recorded hash")
        if root is not None:
            full = os.path.join(root, path)
            if not os.path.isfile(full) or os.path.islink(full):
                bad("manifest-document-missing", f"{path} is listed but not in the output")
            elif sha256_file(full) != d.get("sha256"):
                bad("manifest-document-hash", f"{path} does not match its recorded hash")
    if root is not None:
        files, _ = list_tree(root)
        for rel in files:
            if rel != MANIFEST_PATH and rel not in listed:
                out.append(Finding("manifest-unlisted", "file in the output is not in the manifest", rel))
    return out


def load_manifest(root: str) -> object:
    with open(os.path.join(root, MANIFEST_PATH), "rb") as fh:
        data = fh.read(1 << 20)
    return json.loads(data.decode("utf-8"))
