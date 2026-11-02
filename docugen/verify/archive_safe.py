"""Safe extraction of an uploaded source archive (zip, tar, tar.gz).

This is the ingest step's front door. The archive is hostile until proven otherwise, so nothing is
extracted by a library call that trusts the archive: every member name is validated, every byte is
counted as it is written, and the first violation aborts with ``UnsafeArchive``.

Rejected (the whole archive): absolute paths, ``..`` components, backslashes, drive letters, control
characters, symlinks, hard links, devices, FIFOs, sparse files, encrypted zip members, names that differ
only by case, a file and a directory with the same name, more than ``max_files`` members, a member or a
total over the byte caps, a zip member whose real size differs from its header, a compression ratio
above ``max_ratio``, and any format that is not zip, tar or gzip-compressed tar.

Skipped (recorded, not fatal): version-control and dependency directories, files that usually hold
secrets, and binary files. The caller sends only what is left to the model.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from typing import IO

from .common import Finding

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE = re.compile(r"^[A-Za-z]:")

SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
                       ".mypy_cache", ".pytest_cache", ".gradle"})
SECRET_FILE_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
                        "id_rsa", "id_rsa.*", "id_dsa", "id_ecdsa", "id_ed25519", "id_ed25519.*",
                        ".npmrc", ".pypirc", ".netrc", ".git-credentials", "credentials",
                        "*.tfstate", "*.tfstate.backup", "terraform.tfvars")
# Files that look like secrets but are templates meant to be committed.
SECRET_FILE_ALLOW = (".env.example", ".env.sample", ".env.template", ".env.dist")
_SNIFF_BYTES = 8192
_CHUNK = 64 * 1024


@dataclass(frozen=True)
class Limits:
    max_archive_bytes: int = 50 * 1024 * 1024
    max_files: int = 20_000
    max_total_bytes: int = 300 * 1024 * 1024
    max_member_bytes: int = 20 * 1024 * 1024
    max_ratio: int = 200  # uncompressed / compressed, checked on zip members above 1 MiB
    max_depth: int = 32
    max_component_chars: int = 255
    max_path_chars: int = 1024


class UnsafeArchive(Exception):
    """The archive violates a rule; nothing from it may be used."""

    def __init__(self, rule: str, message: str, path: str = "") -> None:
        super().__init__(f"[{rule}] {message}" + (f" ({path!r})" if path else ""))
        self.rule = rule
        self.message = message
        self.path = path

    def to_finding(self) -> Finding:
        return Finding(self.rule, self.message, self.path)


@dataclass
class ExtractResult:
    files: int = 0
    bytes_written: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)


def normalize_member_name(name: str, limits: Limits) -> tuple[str, bool]:
    """Return ``(relative posix path, is_dir)`` or raise ``UnsafeArchive``."""
    if "\x00" in name or _CONTROL.search(name):
        raise UnsafeArchive("control-character", "control character in a member name", repr(name))
    if "\\" in name:
        raise UnsafeArchive("backslash", "backslash in a member name", name)
    if name.startswith("/") or _DRIVE.match(name):
        raise UnsafeArchive("absolute-path", "absolute path in a member name", name)
    is_dir = name.endswith("/")
    parts: list[str] = []
    for part in name.split("/"):
        if part in ("", "."):
            continue  # "./x" is how tar writes paths; "a//b" collapses
        if part == "..":
            raise UnsafeArchive("path-traversal", "'..' in a member name", name)
        if len(part) > limits.max_component_chars:
            raise UnsafeArchive("name-too-long", "path component too long", name[:80])
        parts.append(part)
    if not parts:
        return "", True  # the archive root itself
    if len(parts) > limits.max_depth:
        raise UnsafeArchive("too-deep", f"more than {limits.max_depth} levels", name[:80])
    rel = "/".join(parts)
    if len(rel) > limits.max_path_chars:
        raise UnsafeArchive("name-too-long", "path too long", rel[:80])
    return rel, is_dir


def skip_reason(rel: str) -> str | None:
    """Why a path is not extracted, or None. Applies to a normalised relative path."""
    parts = rel.split("/")
    for d in parts[:-1]:
        if d in SKIP_DIRS:
            return f"directory:{d}"
    base = parts[-1]
    if base in SKIP_DIRS:
        return f"directory:{base}"
    if base.lower() in SECRET_FILE_ALLOW:
        return None
    for pat in SECRET_FILE_PATTERNS:
        if fnmatch.fnmatchcase(base.lower(), pat):
            return "secret-file"
    return None


def detect_format(path: str) -> str:
    with open(path, "rb") as fh:
        head = fh.read(512)
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if head[:2] == b"\x1f\x8b":
        return "tar.gz"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    raise UnsafeArchive("unknown-format", "not a zip, tar or tar.gz archive")


class _Extractor:
    def __init__(self, dest: str, limits: Limits) -> None:
        self.dest = os.path.realpath(dest)
        self.limits = limits
        self.result = ExtractResult()
        self.members = 0
        self.seen: dict[str, bool] = {}  # casefolded path -> is_dir
        self.total = 0

    def member(self, name: str) -> tuple[str, bool]:
        self.members += 1
        if self.members > self.limits.max_files:
            raise UnsafeArchive("too-many-files", f"more than {self.limits.max_files} members")
        rel, is_dir = normalize_member_name(name, self.limits)
        if not rel:
            return "", True
        key = rel.casefold()
        if key in self.seen:
            raise UnsafeArchive("duplicate-path", "two members with the same name (ignoring case)", rel)
        self.seen[key] = is_dir
        parent = posixpath.dirname(key)
        while parent:
            if self.seen.get(parent) is False:
                raise UnsafeArchive("path-conflict", "a file and a directory share a name", rel)
            parent = posixpath.dirname(parent)
        return rel, is_dir

    def dir_member(self, rel: str) -> None:
        """A directory entry of the archive: created unless it is a skipped directory."""
        if skip_reason(rel):
            return
        self.make_dir(rel)

    def make_dir(self, rel: str) -> None:
        target = os.path.join(self.dest, rel)
        try:
            os.makedirs(target, mode=0o755, exist_ok=True)
        except (FileExistsError, NotADirectoryError) as exc:
            raise UnsafeArchive("path-conflict", "a file and a directory share a name", rel) from exc
        self._inside(target, rel)

    def _inside(self, target: str, rel: str) -> None:
        real = os.path.realpath(target)
        if real != self.dest and not real.startswith(self.dest + os.sep):
            raise UnsafeArchive("path-traversal", "member resolves outside the destination", rel)

    def write(self, rel: str, src: IO[bytes], declared: int | None) -> None:
        reason = skip_reason(rel)
        if reason:
            self.result.skipped.append((rel, reason))
            return
        first = src.read(_SNIFF_BYTES)
        if b"\x00" in first:
            self.result.skipped.append((rel, "binary"))
            return
        target = os.path.join(self.dest, rel)
        parent = os.path.dirname(target)
        if parent != self.dest:
            self.make_dir(os.path.relpath(parent, self.dest))
        self._inside(parent, rel)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags, 0o644)
        except FileExistsError as exc:
            raise UnsafeArchive("duplicate-path", "member already exists", rel) from exc
        written = 0
        try:
            with os.fdopen(fd, "wb") as out:
                chunk = first
                while chunk:
                    written += len(chunk)
                    self.total += len(chunk)
                    if written > self.limits.max_member_bytes:
                        raise UnsafeArchive("member-too-large", "member over the size cap", rel)
                    if self.total > self.limits.max_total_bytes:
                        raise UnsafeArchive("archive-too-large", "extracted bytes over the total cap", rel)
                    if declared is not None and written > declared:
                        raise UnsafeArchive("size-mismatch", "member is larger than its header says", rel)
                    out.write(chunk)
                    chunk = src.read(_CHUNK)
        except BaseException:
            try:
                os.unlink(target)
            except OSError:
                pass
            raise
        if declared is not None and written != declared:
            os.unlink(target)
            raise UnsafeArchive("size-mismatch", "member is smaller than its header says", rel)
        self.result.files += 1
        self.result.bytes_written += written


def _extract_zip(path: str, ex: _Extractor) -> None:
    lim = ex.limits
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise UnsafeArchive("bad-archive", "not a readable zip file") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > lim.max_files:
            raise UnsafeArchive("too-many-files", f"more than {lim.max_files} members")
        for info in infos:
            rel, is_dir = ex.member(info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode and mode not in (0o100000, 0o040000):
                raise UnsafeArchive("special-file", "symlink, device or other non-regular member", info.filename)
            if info.flag_bits & 0x1:
                raise UnsafeArchive("encrypted", "encrypted zip member", info.filename)
            if not rel:
                continue
            if is_dir or info.is_dir():
                ex.dir_member(rel)
                continue
            if info.file_size > lim.max_member_bytes:
                raise UnsafeArchive("member-too-large", "member over the size cap", info.filename)
            if info.file_size > 1024 * 1024 and info.compress_size and \
                    info.file_size / info.compress_size > lim.max_ratio:
                raise UnsafeArchive("compression-bomb", "compression ratio over the cap", info.filename)
            try:
                with zf.open(info) as src:
                    ex.write(rel, src, info.file_size)
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
                raise UnsafeArchive("bad-archive", "unreadable zip member", info.filename) from exc


def _extract_tar(path: str, ex: _Extractor, mode: str) -> None:
    try:
        tf = tarfile.open(path, mode=mode)  # type: ignore[call-overload]  # streaming, sequential
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise UnsafeArchive("bad-archive", "not a readable tar file") from exc
    with tf:
        try:
            for info in tf:
                rel, is_dir = ex.member(info.name)
                if info.issparse() or not (info.isreg() or info.isdir()):
                    raise UnsafeArchive("special-file", "symlink, hard link, device, FIFO or sparse member", info.name)
                if not rel:
                    continue
                if info.isdir():
                    ex.dir_member(rel)
                    continue
                if info.size > ex.limits.max_member_bytes:
                    raise UnsafeArchive("member-too-large", "member over the size cap", info.name)
                src = tf.extractfile(info)
                if src is None:
                    raise UnsafeArchive("special-file", "unreadable member", info.name)
                with src:
                    ex.write(rel, src, info.size)
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise UnsafeArchive("bad-archive", f"unreadable tar data: {type(exc).__name__}") from exc


def safe_extract(archive: str, dest: str, limits: Limits | None = None) -> ExtractResult:
    """Extract ``archive`` into the existing, empty directory ``dest`` or raise ``UnsafeArchive``.

    On failure ``dest`` may hold a partial tree: the caller discards it.
    """
    limits = limits or Limits()
    if os.path.getsize(archive) > limits.max_archive_bytes:
        raise UnsafeArchive("archive-too-large", f"archive over {limits.max_archive_bytes} bytes")
    if not os.path.isdir(dest) or os.listdir(dest):
        raise UnsafeArchive("bad-destination", "destination must be an existing empty directory")
    kind = detect_format(archive)
    ex = _Extractor(dest, limits)
    if kind == "zip":
        _extract_zip(archive, ex)
    elif kind == "tar.gz":
        _extract_tar(archive, ex, "r|gz")
    else:
        _extract_tar(archive, ex, "r|")
    return ex.result


def check_archive(archive: str, limits: Limits | None = None) -> list[Finding]:
    """Extract into a throwaway directory and report the first violation, if any."""
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="docugen-archive-")
    try:
        safe_extract(archive, tmp, limits)
    except UnsafeArchive as exc:
        return [exc.to_finding()]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return []
