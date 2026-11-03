"""One documentation session, start to finish, inside the box.

``python3 -P -m docugen.box --session -- claude ARGS... -p PROMPT`` does three things in order, the first and the last
by trusted code that the agent cannot call:

1. **Pre-step.** Fetch the normalised source from the presigned GET in the session's environment, extract it safely into
   the source directory (``archive_safe``: no traversal, links, devices, bombs; the same limits the ingest step
   applied), and refuse a session with nothing to document.
2. **The agent**, through the supervisor (``supervise.execute``), with every ``DOCUGEN_*`` variable removed from its
   environment: it must never hold the presigned URLs, which are bearer secrets.
3. **Post-step**, only if the session ended ``DONE`` or ``LIMIT_REACHED``. Check the output tree against the allowlist
   (``docs/**``, fixed extensions, no links or executables), write the provenance manifest, check it against the
   source, pack ``docs/`` into one reproducible tar and upload it to the presigned POST.

Failures are mapped onto the codes of the status vocabulary and nothing else leaves the box: ``NO_SOURCE`` (nothing to
fetch or nothing to document), ``TOO_LARGE`` (a size cap), ``VALIDATION`` (the output is not an acceptable tree),
``INTERNAL`` (anything else). A session that fails before the agent starts never runs the agent; one that fails after it
reports ``FAILED`` whatever the agent claimed. A ``LIMIT_REACHED`` session still packs and uploads what it has: whether
partial output is delivered is the deliver step's decision, not the box's.

Between the agent and the terminal line the post-step prints ``STATUS: PACKAGING`` and one line of numbers,
``OUTPUT: documents=N bytes=N unresolved_sources=N``.

Standard library only; the parts are plain functions so that the same code can run in separate containers later.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import IO

import docugen
from docugen.verify.archive_safe import Limits, UnsafeArchive, safe_extract
from docugen.verify.manifest import MANIFEST_PATH, check_manifest, check_tree, list_tree, load_manifest

from . import status, supervise, transfer
from .provenance import SourceInfo, build_manifest, write_manifest
from .supervise import EXIT_FAILED, EXIT_USAGE, UsageError, Verdict

MAX_SOURCE_BYTES = Limits().max_archive_bytes
MAX_OUTPUT_BYTES = 25 * 1024 * 1024
DEFAULT_SRC, DEFAULT_OUT, DEFAULT_WORK = "/workspace/src", "/workspace/out", "/workspace/work"
DEFAULT_TOOLS_FILE = "/opt/docugen/etc/tools.json"
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
        return cls(session_id, source_url, out_url, fields, source)


@dataclass(frozen=True)
class OutputSummary:
    documents: int
    bytes: int
    unresolved_sources: int

    def line(self) -> str:
        return f"OUTPUT: documents={self.documents} bytes={self.bytes} unresolved_sources={self.unresolved_sources}"


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


def pre_step(config: SessionConfig, src_dir: str, work_dir: str) -> None:
    """Fetch the source and extract it into the empty ``src_dir``. Raises ``StepFailed``."""
    archive = os.path.join(work_dir, "source.tar")
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


def package(out_dir: str, dest: str) -> int:
    """Pack ``docs/`` of ``out_dir`` into the new tar ``dest``, reproducibly, and return its size.

    Only regular files that are still regular files when opened (``O_NOFOLLOW``); sorted; owner, time and mode fixed.
    """
    files, findings = list_tree(out_dir)
    if findings:
        raise StepFailed("VALIDATION")
    with tarfile.open(dest, "w", format=tarfile.PAX_FORMAT) as tar:
        for rel in sorted(f for f in files if f.startswith("docs/")):
            try:
                fd = os.open(os.path.join(out_dir, rel), os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                raise StepFailed("VALIDATION") from None
            with os.fdopen(fd, "rb") as fh:
                info = tarfile.TarInfo(name=rel)
                info.size = os.fstat(fh.fileno()).st_size
                # TarInfo's defaults are what is wanted (mode 0644, owner 0, no names, time 0); a test pins them
                tar.addfile(info, fh)
    size = os.path.getsize(dest)
    if size > MAX_OUTPUT_BYTES:
        raise StepFailed("TOO_LARGE")
    return size


def post_step(config: SessionConfig, src_dir: str, out_dir: str, work_dir: str, tools: dict[str, str],
              out: IO[str], now: datetime | None = None) -> OutputSummary:
    """Check, document the provenance of, pack and upload the output. Raises ``StepFailed``."""
    out.write(status.format_status("PACKAGING") + "\n")
    out.flush()
    if check_tree(out_dir):
        raise StepFailed("VALIDATION")
    manifest, unresolved = build_manifest(out_dir, src_dir, config.source, generator_version=docugen.__version__,
                                          tools=tools, now=now)
    if not manifest["documents"]:
        raise StepFailed("VALIDATION")  # nothing to deliver
    try:
        write_manifest(out_dir, manifest)
        if check_manifest(load_manifest(out_dir), root=out_dir, source_root=src_dir):
            raise StepFailed("INTERNAL")
    except (OSError, ValueError):
        raise StepFailed("INTERNAL") from None
    archive = os.path.join(work_dir, "docs.tar")
    try:
        size = package(out_dir, archive)
        transfer.upload(config.out_url, config.out_fields, archive, MAX_OUTPUT_BYTES)
    except transfer.TransferError as exc:
        raise StepFailed("TOO_LARGE" if exc.code == transfer.TransferError.TOO_LARGE else "INTERNAL") from None
    finally:
        try:
            os.unlink(archive)
        except FileNotFoundError:
            pass
    return OutputSummary(len(manifest["documents"]), size, len(unresolved))


def run_session(argv: list[str], environ: Mapping[str, str], *, src_dir: str = DEFAULT_SRC, out_dir: str = DEFAULT_OUT,
                work_dir: str = DEFAULT_WORK, tools_file: str = DEFAULT_TOOLS_FILE, out: IO[str] | None = None,
                now: datetime | None = None) -> int:
    """The whole session. Returns the exit code of ``supervise``: 0 done, 1 failed, 2 started wrongly, 3 limit reached."""
    out = out or sys.stdout
    try:
        if not argv or not any(a in ("-p", "--print") for a in argv[1:]):
            raise UsageError("the agent must be started headless (-p PROMPT)")
        config = SessionConfig.from_environ(environ)
    except (UsageError, ConfigError) as exc:
        sys.stderr.write(f"docugen-box: {exc}\n")
        return EXIT_USAGE

    try:
        pre_step(config, src_dir, work_dir)
    except StepFailed as exc:
        return supervise.finish(out, Verdict("FAILED", exc.code, EXIT_FAILED), None)
    except Exception:  # noqa: BLE001 - a traceback can hold paths from the customer's source: say only that it failed
        return supervise.finish(out, Verdict("FAILED", "INTERNAL", EXIT_FAILED), None)

    agent_env = {k: v for k, v in environ.items() if not k.startswith("DOCUGEN_")}
    try:
        verdict, result = supervise.execute(argv, out=out, env=agent_env)
    except UsageError as exc:
        sys.stderr.write(f"docugen-box: {exc}\n")
        return EXIT_USAGE
    except Exception:  # noqa: BLE001 - see above
        return supervise.finish(out, Verdict("FAILED", "INTERNAL", EXIT_FAILED), None)

    if verdict.word in ("DONE", "LIMIT_REACHED"):
        try:
            summary = post_step(config, src_dir, out_dir, work_dir, load_tools(tools_file), out, now)
            out.write(summary.line() + "\n")
        except StepFailed as exc:
            verdict = Verdict("FAILED", exc.code, EXIT_FAILED)
        except Exception:  # noqa: BLE001 - see above
            verdict = Verdict("FAILED", "INTERNAL", EXIT_FAILED)
    return supervise.finish(out, verdict, result)


def main(argv: list[str]) -> int:
    """``--session -- claude ARGS...``; the directories come from ``DOCUGEN_SRC_DIR``, ``DOCUGEN_OUT_DIR`` and ``DOCUGEN_WORK_DIR``."""
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
                       work_dir=env.get("DOCUGEN_WORK_DIR", DEFAULT_WORK), tools_file=env.get("DOCUGEN_TOOLS_FILE", DEFAULT_TOOLS_FILE))
