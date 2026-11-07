"""One documentation session, start to finish, inside the box.

``python3 -P -m docugen.box --session -- claude ARGS... -p PROMPT`` does three things in order, the first and the last
by trusted code that the agent cannot call:

1. **Pre-step.** Fetch the normalised source from the presigned GET in the session's environment, extract it safely into
   the source directory (``archive_safe``: no traversal, links, devices, bombs; the same limits the ingest step
   applied), and refuse a session with nothing to document.
2. **The agent**, through the supervisor (``supervise.execute``), with every ``DOCUGEN_*`` variable removed from its
   environment: it must never hold the presigned URLs, which are bearer secrets.
3. **Post-step**, only if the session ended ``DONE`` or ``LIMIT_REACHED``. Check the output tree against the allowlist
   (``docs/**``, fixed extensions, no links or executables), print PDFs if the session asked for them (``pdf``: the agent
   never does), build and check the provenance manifest, pack ``docs/`` and the manifest into one reproducible tar and
   upload it to the presigned POST.

Failures are mapped onto the codes of the status vocabulary and nothing else leaves the box: ``NO_SOURCE`` (nothing to
fetch or nothing to document), ``TOO_LARGE`` (a size cap), ``VALIDATION`` (the output is not an acceptable tree),
``INTERNAL`` (anything else). A session that fails before the agent starts never runs the agent; one that fails after it
reports ``FAILED`` whatever the agent claimed. A ``LIMIT_REACHED`` session still packs and uploads what it has: whether
partial output is delivered is the deliver step's decision, not the box's.

Between the agent and the terminal line the post-step prints ``STATUS: PACKAGING`` and one line of numbers,
``OUTPUT: documents=N bytes=N unresolved_sources=N``. A session that asked for PDFs adds ``pdfs=N``, and ``pdf_error=WORD`` when
it asked and none was made (``pdf.ERRORS``): the documents are delivered either way.

Two privilege models, chosen by who runs this process:

- **Root (the model the image is built for).** This process is root: it extracts the source (root-owned, so read-only for
  the agent by ownership), keeps its own files in a private root-only directory, and starts the agent as an unprivileged
  user (uid 1001 by default, never 0), which then cannot read this process's environment or memory. The output directory
  belongs to the agent, so this process never writes in it: it reads the tree (links, devices and hard links are
  refused before anything is opened, and files are opened without following links) and puts the manifest into the
  archive from memory. Whatever the agent plants there cannot make a root process follow it.
- **Not root (the fallback).** Everything runs as one user; after the extraction the source is made read-only by mode
  bits, and the scratch files live in the work directory. The separation is then only as strong as the agent's own
  permission rules and the deliver step, which re-hashes its own copy of the source.

Standard library only; the parts are plain functions so that the same code can run in separate containers later.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import sys
import shutil
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import IO

import docugen
from docugen.verify.archive_safe import Limits, UnsafeArchive, safe_extract
from docugen.verify.manifest import MANIFEST_PATH, check_manifest, check_tree, list_tree

from . import pdf as printing
from . import status, supervise, transfer
from .provenance import SourceInfo, build_manifest, manifest_bytes
from .supervise import EXIT_FAILED, EXIT_USAGE, UsageError, Verdict

MAX_SOURCE_BYTES = Limits().max_archive_bytes
MAX_OUTPUT_BYTES = 25 * 1024 * 1024
DEFAULT_SRC, DEFAULT_OUT, DEFAULT_WORK = "/workspace/src", "/workspace/out", "/workspace/work"
DEFAULT_SCRATCH = "/run/docugen"  # root only; not under /workspace, which the agent can write
DEFAULT_PDF_PARENT = "/run/docugen-pdf"  # readable by the agent's user: the printer, which runs as that user, reads its input here
DEFAULT_TOOLS_FILE = "/opt/docugen/etc/tools.json"
DEFAULT_RELEASE_FILE = "/opt/docugen/etc/release"  # the image's release, written when the image was built (the name its tag has)
_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")  # what the printer accepts as a version: also the shape of an image tag
DEFAULT_AGENT_ID = 1001           # the image's user `claude`
AGENT_NAME, AGENT_HOME = "claude", "/home/claude"
_SIZE_RULES = frozenset({"archive-too-large", "member-too-large", "too-many-files", "compression-bomb"})
_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TRANSFER_CODES = {transfer.TransferError.TOO_LARGE: "TOO_LARGE", transfer.TransferError.NOT_FOUND: "NO_SOURCE"}


class ConfigError(ValueError):
    """The session's environment is missing or malformed. The message is fixed text: it never holds a value."""


class StepFailed(Exception):
    """A step of the session failed. ``code`` is a reason code of the status vocabulary."""

    def __init__(self, code: str) -> None:
        assert code in status.FAILURE_CODES  # noqa: S101 - a programming error, not a session condition
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SessionConfig:
    session_id: str
    source_url: str
    out_url: str
    out_fields: dict[str, str]
    source: SourceInfo
    pdf: str = "none"   # none, each, combined or both (pdf.MODES): the orchestrator's choice, never the agent's

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> SessionConfig:
        session_id = environ.get("DOCUGEN_SESSION_ID", "")
        if not _SESSION_ID.fullmatch(session_id):
            raise ConfigError("DOCUGEN_SESSION_ID is missing or not a UUID")
        try:
            source = SourceInfo.from_environ(dict(environ))
            source_url, out_url = environ.get("DOCUGEN_SOURCE_URL", ""), environ.get("DOCUGEN_OUT_URL", "")
            transfer.check_url(source_url)
            transfer.check_url(out_url)
            fields = transfer.parse_fields(environ.get("DOCUGEN_OUT_FIELDS"))
        except ValueError:
            raise ConfigError("the source description is missing or malformed") from None
        except transfer.TransferError as exc:
            raise ConfigError(f"a session URL or the upload fields are not acceptable ({exc.code})") from None
        try:
            pdf = printing.parse_mode(environ.get("DOCUGEN_PDF"))
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        return cls(session_id, source_url, out_url, fields, source, pdf)


@dataclass(frozen=True)
class AgentIdentity:
    uid: int
    gid: int
    home: str = AGENT_HOME
    name: str = AGENT_NAME


def agent_identity(environ: Mapping[str, str], euid: int) -> AgentIdentity | None:
    """Who the agent runs as: nobody to switch to unless this process is root, and never root itself.

    ``DOCUGEN_AGENT_UID``, ``DOCUGEN_AGENT_GID`` (default 1001) and ``DOCUGEN_AGENT_HOME`` (default ``/home/claude``) come
    from the box's own environment, which only the orchestrator and the image write. An id that is not a plain number from
    1 to 65535, or a home that is not a plain absolute path, is a configuration error.
    """
    if euid != 0:
        return None
    ids = []
    for name in ("DOCUGEN_AGENT_UID", "DOCUGEN_AGENT_GID"):
        text = environ.get(name, str(DEFAULT_AGENT_ID))
        if not (text.isascii() and text.isdigit() and 1 <= int(text) <= 65535):
            raise ConfigError(f"{name} must be a user or group id from 1 to 65535: the agent never runs as root")
        ids.append(int(text))
    home = environ.get("DOCUGEN_AGENT_HOME", AGENT_HOME)
    if not (home.isascii() and re.fullmatch(r"/[A-Za-z0-9._/-]{0,254}", home) and ".." not in home.split("/")):
        raise ConfigError("DOCUGEN_AGENT_HOME must be a plain absolute path")
    return AgentIdentity(ids[0], ids[1], home)


@dataclass(frozen=True)
class OutputSummary:
    documents: int          # without the PDFs
    bytes: int
    unresolved_sources: int
    pdfs: int | None = None          # None: none were asked for
    pdf_error: str | None = None     # one of pdf.ERRORS when some were asked for and none was made

    def line(self) -> str:
        line = f"OUTPUT: documents={self.documents} bytes={self.bytes} unresolved_sources={self.unresolved_sources}"
        if self.pdfs is not None:
            line += f" pdfs={self.pdfs}"
        if self.pdf_error is not None:
            line += f" pdf_error={self.pdf_error}"
        return line


@dataclass(frozen=True)
class Printer:
    """How PDFs are printed: the tool, who it runs as (a ``(uid, gid)``, None for this user) and where its tree is made."""

    tool: str = printing.DEFAULT_TOOL
    user: tuple[int, int] | None = None
    parent: str | None = None   # default: the scratch directory


def load_tools(path: str) -> dict[str, str]:
    """The tool versions the image recorded, for the manifest. A missing or malformed file gives no tools."""
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read(65536).decode("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or len(data) > 50:
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and 0 < len(k) <= 64 and 0 < len(v) <= 64}


def load_release(path: str) -> str:
    """The release of the image that is running, for the manifest and the cover of the combined PDF: what the image recorded when
    it was built, the name its tag has (``1.1.0``) or ``unreleased`` for a build that is not a release.

    The library's own version when there is no such file or it holds anything but one word of that shape: this is only ever a
    statement about which build made the documents, so it is the library's version or the image's, never something made up."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(257)
        text = raw.decode("ascii").strip()
    except (OSError, ValueError):
        return docugen.__version__
    return text if len(raw) <= 256 and _RELEASE.fullmatch(text) else docugen.__version__


def prepare_scratch(path: str) -> None:
    """Make sure ``path`` is a directory only this process can use, creating it (mode 0700) if needed.

    The files this process keeps between steps (the downloaded source, the archive to upload) live here, and nowhere the
    agent can write: a link planted in a directory the agent controls is how a more privileged process gets tricked.
    """
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        st = os.lstat(path)
    except OSError:
        raise StepFailed("INTERNAL") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise StepFailed("INTERNAL")


def make_read_only(root: str) -> None:
    """Remove the write bits from the whole tree under ``root``, ``root`` included (the fallback when not root)."""
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o222)
        os.chmod(dirpath, stat.S_IMODE(os.lstat(dirpath).st_mode) & ~0o222)


def pre_step(config: SessionConfig, src_dir: str, scratch_dir: str) -> None:
    """Fetch the source and extract it into the empty ``src_dir``. Raises ``StepFailed``."""
    archive = os.path.join(scratch_dir, "source.tar")
    try:
        transfer.fetch(config.source_url, archive, MAX_SOURCE_BYTES)
    except transfer.TransferError as exc:
        raise StepFailed(_TRANSFER_CODES.get(exc.code, "INTERNAL")) from None
    try:
        safe_extract(archive, src_dir, Limits())
    except UnsafeArchive as exc:
        raise StepFailed("TOO_LARGE" if exc.rule in _SIZE_RULES else "INTERNAL") from None
    finally:
        try:
            os.unlink(archive)
        except FileNotFoundError:
            pass
    files, _ = list_tree(src_dir)
    if not files:
        raise StepFailed("NO_SOURCE")


def package(out_dir: str, dest: str, manifest_json: bytes) -> int:
    """Pack ``docs/`` of ``out_dir`` and the manifest into the new tar ``dest``, reproducibly, and return its size.

    Only regular files (opened without following a link), sorted; owner, time and mode as ``TarInfo`` defaults them. A
    manifest the agent left in ``out_dir`` is not packed: the one given replaces it.
    """
    files, findings = list_tree(out_dir)
    if findings:
        raise StepFailed("VALIDATION")
    entries: list[tuple[str, bytes | None]] = [(rel, None) for rel in files if rel.startswith("docs/") and rel != MANIFEST_PATH]
    entries.append((MANIFEST_PATH, manifest_json))
    entries.sort(key=lambda entry: entry[0])
    try:
        raw = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise StepFailed("INTERNAL") from None
    with os.fdopen(raw, "wb") as handle, tarfile.open(fileobj=handle, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, data in entries:
            info = tarfile.TarInfo(name=rel)
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                continue
            try:
                fd = os.open(os.path.join(out_dir, rel), os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                raise StepFailed("VALIDATION") from None
            with os.fdopen(fd, "rb") as fh:
                info.size = os.fstat(fh.fileno()).st_size
                # TarInfo's defaults are what is wanted (mode 0644, owner 0, no names, time 0); a test pins them
                tar.addfile(info, fh)
    size = os.path.getsize(dest)
    if size > MAX_OUTPUT_BYTES:
        raise StepFailed("TOO_LARGE")
    return size


def _source_id(source: SourceInfo) -> str:
    return f"github:{source.commit}" if source.kind == "github" else f"zip:sha256:{source.archive_sha256}"


def post_step(config: SessionConfig, src_dir: str, out_dir: str, scratch_dir: str, tools: dict[str, str],
              out: IO[str], now: datetime | None = None, printer: Printer | None = None,
              release: str | None = None) -> OutputSummary:
    """Check, print PDFs if asked, document the provenance of, pack and upload the output. Raises ``StepFailed``.

    ``release`` is the image's release (``load_release``): the generator's version in the manifest and on the cover of the combined
    PDF. The version of the library when it is not given.

    Writes nothing in ``out_dir``: with PDFs it works on a private copy of the documents, which is removed at the end.
    """
    version = release or docugen.__version__
    out.write(status.format_status("PACKAGING") + "\n")
    out.flush()
    if check_tree(out_dir):  # the agent's output: no PDF may be in it, whatever was asked for
        raise StepFailed("VALIDATION")
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tree, printed, work = out_dir, None, None
    try:
        if config.pdf != "none":
            printer = printer or Printer()
            parent = printer.parent or scratch_dir
            os.makedirs(parent, mode=0o755, exist_ok=True)
            if printer.user is not None:
                os.chmod(parent, 0o755)
            work = tempfile.mkdtemp(prefix="pdf-", dir=parent)
            printed = printing.print_documents(
                config.pdf, out_dir, work, source_id=_source_id(config.source), generated_at=moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                generator_version=version, tool=printer.tool, user=printer.user, max_output_bytes=MAX_OUTPUT_BYTES)
            if printed.stage is not None:
                tree = printed.stage
        has_pdfs = tree is not out_dir
        manifest, unresolved = build_manifest(tree, src_dir, config.source, generator_version=version, tools=tools,
                                              now=moment, pdf_documents=printed.printed_from if has_pdfs and printed else None)
        if not manifest["documents"]:
            raise StepFailed("VALIDATION")  # nothing to deliver
        if has_pdfs and check_tree(tree, allow_pdf=True):
            raise StepFailed("VALIDATION")
        if check_manifest(manifest, root=tree, source_root=src_dir, allow_pdf=has_pdfs):
            raise StepFailed("INTERNAL")
        archive = os.path.join(scratch_dir, "docs.tar")
        try:
            size = package(tree, archive, manifest_bytes(manifest))
            transfer.upload(config.out_url, config.out_fields, archive, MAX_OUTPUT_BYTES)
        except transfer.TransferError as exc:
            raise StepFailed("TOO_LARGE" if exc.code == transfer.TransferError.TOO_LARGE else "INTERNAL") from None
        finally:
            try:
                os.unlink(archive)
            except FileNotFoundError:
                pass
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    documents = sum(1 for entry in manifest["documents"] if entry["kind"] != "pdf")
    return OutputSummary(documents, size, len(unresolved), printed.pdfs if printed else None, printed.error if printed else None)


def run_session(argv: list[str], environ: Mapping[str, str], *, src_dir: str = DEFAULT_SRC, out_dir: str = DEFAULT_OUT,
                work_dir: str = DEFAULT_WORK, scratch_dir: str | None = None, tools_file: str = DEFAULT_TOOLS_FILE,
                release_file: str = DEFAULT_RELEASE_FILE, out: IO[str] | None = None, now: datetime | None = None, drop_privileges: bool = True,
                pdf_tool: str = printing.DEFAULT_TOOL) -> int:
    """The whole session. Returns the exit code of ``supervise``: 0 done, 1 failed, 2 started wrongly, 3 limit reached.

    ``drop_privileges=False`` never switches the agent to another user, whoever runs this (the tests, which cannot assume
    their uid, use it); otherwise the agent is switched exactly when this process is root.
    """
    out = out or sys.stdout
    try:
        if not argv or not any(a in ("-p", "--print") for a in argv[1:]):
            raise UsageError("the agent must be started headless (-p PROMPT)")
        config = SessionConfig.from_environ(environ)
        identity = agent_identity(environ, os.geteuid()) if drop_privileges else None
    except (UsageError, ConfigError) as exc:
        sys.stderr.write(f"docugen-box: {exc}\n")
        return EXIT_USAGE
    # The agent's own work directory is the agent's. What this process keeps for itself goes where only it can look.
    scratch = scratch_dir or (DEFAULT_SCRATCH if identity else work_dir)

    try:
        prepare_scratch(scratch)
        pre_step(config, src_dir, scratch)
        if identity is None:
            make_read_only(src_dir)  # one user: the mode bits are all there is
    except StepFailed as exc:
        return supervise.finish(out, Verdict("FAILED", exc.code, EXIT_FAILED), None)
    except Exception:  # noqa: BLE001 - a traceback can hold paths from the customer's source: say only that it failed
        return supervise.finish(out, Verdict("FAILED", "INTERNAL", EXIT_FAILED), None)

    agent_env = {k: v for k, v in environ.items() if not k.startswith("DOCUGEN_")}
    if identity is not None:
        agent_env.update(HOME=identity.home, USER=identity.name, LOGNAME=identity.name)
    try:
        verdict, result = supervise.execute(argv, out=out, env=agent_env,
                                            user=(identity.uid, identity.gid) if identity else None)
    except UsageError as exc:
        sys.stderr.write(f"docugen-box: {exc}\n")
        return EXIT_USAGE
    except Exception:  # noqa: BLE001 - see above
        return supervise.finish(out, Verdict("FAILED", "INTERNAL", EXIT_FAILED), None)

    if verdict.word in ("DONE", "LIMIT_REACHED"):
        try:
            printer = Printer(pdf_tool, (identity.uid, identity.gid) if identity else None,
                              DEFAULT_PDF_PARENT if identity else scratch)
            summary = post_step(config, src_dir, out_dir, scratch, load_tools(tools_file), out, now, printer, load_release(release_file))
            out.write(summary.line() + "\n")
        except StepFailed as exc:
            verdict = Verdict("FAILED", exc.code, EXIT_FAILED)
        except Exception:  # noqa: BLE001 - see above
            verdict = Verdict("FAILED", "INTERNAL", EXIT_FAILED)
    return supervise.finish(out, verdict, result)


def main(argv: list[str]) -> int:
    """``--session -- claude ARGS...``; the directories come from ``DOCUGEN_SRC_DIR``, ``DOCUGEN_OUT_DIR``,
    ``DOCUGEN_WORK_DIR`` and ``DOCUGEN_SCRATCH_DIR``."""
    if argv[:1] == ["--"]:
        argv = argv[1:]
    env = dict(os.environ)
    try:
        return _main(argv, env)
    except Exception:  # noqa: BLE001 - last resort: nothing of an exception is printed
        sys.stdout.write(status.format_status("FAILED", "INTERNAL") + "\n")
        sys.stdout.flush()
        return EXIT_FAILED


def _main(argv: list[str], env: dict[str, str]) -> int:
    return run_session(argv, env, src_dir=env.get("DOCUGEN_SRC_DIR", DEFAULT_SRC), out_dir=env.get("DOCUGEN_OUT_DIR", DEFAULT_OUT),
                       work_dir=env.get("DOCUGEN_WORK_DIR", DEFAULT_WORK), scratch_dir=env.get("DOCUGEN_SCRATCH_DIR"),
                       tools_file=env.get("DOCUGEN_TOOLS_FILE", DEFAULT_TOOLS_FILE),
                       release_file=env.get("DOCUGEN_RELEASE_FILE", DEFAULT_RELEASE_FILE))
