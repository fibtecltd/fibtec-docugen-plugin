"""Printing the documentation set as PDFs, inside the box, after the agent has stopped.

A session may ask for PDFs (``DOCUGEN_PDF``: ``none``, ``each``, ``combined`` or ``both``; absent means ``none``). The agent
never prints them and never learns that they were asked for: like every ``DOCUGEN_*`` variable the setting is removed from
its environment, and the output tree the agent writes is still checked without PDFs allowed. Trusted code does it all, in
this order:

1. The agent's output has passed ``check_tree``. Its documents (``docs/**``, regular files only) are copied, without
   following a link, into a private tree that the printer can read and cannot change.
2. The printer, ``docugen-pdf``, runs as the unprivileged user with an empty environment, no input but that tree and no
   output but one directory of its own, and a time limit. It is told exactly which PDFs to write and in which order; it
   decides nothing about the set. (It lives in the box image, with Chromium, which already draws the diagrams.)
3. Every file it wrote is read back without following a link and checked by ``pdf_check``. Anything wrong, anywhere, and none
   of its PDFs is kept: a partial or doubtful set is never delivered.
4. What is kept is copied into the private tree under ``docs/pdf/``, and the caller builds the manifest, checks it with PDFs
   allowed, and packs that tree.

Printing is an extra, not the product. If it cannot be done (the tool is not in the image, it fails, it takes too long, a
file is not acceptable, the output would be too big) the session still delivers the documents, and the ``OUTPUT`` line says
that no PDF was made and why, in one fixed word. The documents were paid for; losing them over a PDF would be the wrong trade.

The contract with the printer, which ``docugen/bin/docugen-pdf`` of the box image implements::

    docugen-pdf --in DIR --out DIR --source-id TEXT --generated-at RFC3339 --generator-version TEXT
                --doc PATH [--doc PATH ...]      the Markdown documents under DIR, in reading order
                [--combined NAME.pdf]            one PDF of all of them, in that order
                [--single PATH=NAME.pdf ...]     one PDF of one document

It writes exactly the files it was asked for into ``--out`` and prints nothing that matters: its output is discarded. In the
box image it is ``/usr/local/bin/docugen-pdf``, a wrapper in ``claude-docker-ecs`` (``docugen/bin/``).

Standard library only.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import signal
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass

from docugen.verify import pdf_check
from docugen.verify.manifest import MANIFEST_PATH, MAX_PDF_BYTES, PDF_DIR, list_tree

MODES = ("none", "each", "combined", "both")
COMBINED_NAME = "documentation.pdf"
# The reading order of the combined PDF: the index, the onboarding guide, the architecture overview, the API reference.
# Any other Markdown document follows, in path order.
LEAD_ORDER = ("README.md", "onboarding.md", "architecture.md", "api-reference.md")
DEFAULT_TOOL = "/usr/local/bin/docugen-pdf"   # next to the box's other wrappers (mmdc, plantuml, docugen-verify)
TOOL_TIMEOUT_SECONDS = 300
# The tar the box uploads is capped (``session.MAX_OUTPUT_BYTES``); PDFs are kept only if the whole still fits, with room.
OUTPUT_HEADROOM_BYTES = 1024 * 1024

# The words the OUTPUT line may carry after ``pdf_error=``: fixed text, never anything the printer or a file said.
ERRORS = ("no-documents", "tool-missing", "tool-failed", "timeout", "invalid", "too-large", "internal")


def parse_mode(value: str | None) -> str:
    """The mode a session asked for. Absent or empty is ``none``; anything else that is not a mode is an error."""
    if value is None or value == "":
        return "none"
    if value not in MODES:
        raise ValueError("DOCUGEN_PDF is not one of none, each, combined, both")
    return value


def document_order(markdown: Iterable[str]) -> list[str]:
    """The Markdown documents (paths relative to ``docs/``) in reading order: the lead documents that exist, then the rest."""
    present = set(markdown)
    lead = [name for name in LEAD_ORDER if name in present]
    rest = sorted(present - set(lead), key=lambda name: (name.casefold(), name))
    return lead + rest


def single_name(document: str, taken: set[str]) -> str:
    """The file name of the PDF of one document: its path with ``/`` as ``-``, never the combined name, never taken twice."""
    stem = document[:-3] if document.lower().endswith(".md") else document
    stem = stem.replace("/", "-")
    candidate, n = f"{stem}.pdf", 1
    while candidate == COMBINED_NAME or candidate in taken:
        n += 1
        candidate = f"{stem}-{n}.pdf"
    return candidate


@dataclass(frozen=True)
class PrintPlan:
    documents: tuple[str, ...]
    singles: tuple[tuple[str, str], ...]   # (document, PDF name), in document order
    combined: str | None

    @property
    def names(self) -> list[str]:
        return ([self.combined] if self.combined else []) + [name for _, name in self.singles]

    @property
    def printed_from(self) -> dict[str, tuple[str, ...]]:
        """For each PDF, as ``docs/pdf/NAME.pdf``, the documents (relative to ``docs/``) it was printed from."""
        out = {f"{PDF_DIR}{name}": (document,) for document, name in self.singles}
        if self.combined:
            out[f"{PDF_DIR}{self.combined}"] = self.documents
        return out


def make_plan(markdown: Iterable[str], mode: str) -> PrintPlan | None:
    """What to print for ``mode``, or None when there is nothing to print (no mode, or no Markdown document)."""
    documents = document_order(markdown)
    if mode == "none" or not documents:
        return None
    taken: set[str] = set()
    singles: list[tuple[str, str]] = []
    if mode in ("each", "both"):
        for document in documents:
            name = single_name(document, taken)
            taken.add(name)
            singles.append((document, name))
    return PrintPlan(tuple(documents), tuple(singles), COMBINED_NAME if mode in ("combined", "both") else None)


@dataclass(frozen=True)
class PrintResult:
    """``stage`` is the private tree to build the manifest from and pack (it holds the PDFs), or None if nothing was printed."""

    stage: str | None
    pdfs: int
    error: str | None   # one of ERRORS, or None
    printed_from: dict[str, tuple[str, ...]] | None = None   # PDF path -> its documents, for the manifest


class _Failed(Exception):
    def __init__(self, error: str) -> None:
        assert error in ERRORS  # noqa: S101 - a programming error
        super().__init__(error)
        self.error = error


def _copy_in(source: str, dest: str, mode: int) -> int:
    """Copy one regular file, opened without following a link, into a new file. Returns its size."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as src:
        info = os.fstat(src.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise _Failed("internal")
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return info.st_size


def stage_documents(out_dir: str, stage: str) -> tuple[list[str], int]:
    """Copy the documents of ``out_dir`` into ``stage`` and return the Markdown ones (relative to ``docs/``) and the bytes."""
    files, findings = list_tree(out_dir)
    if findings:
        raise _Failed("internal")
    markdown: list[str] = []
    total = 0
    for rel in files:
        if not rel.startswith("docs/") or rel == MANIFEST_PATH or rel.startswith(PDF_DIR):
            continue
        target = os.path.join(stage, *rel.split("/"))
        os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
        total += _copy_in(os.path.join(out_dir, rel), target, 0o644)
        if rel.lower().endswith(".md"):
            markdown.append(rel[len("docs/"):])
    return markdown, total


def _run_tool(tool: str, args: list[str], *, home: str, user: tuple[int, int] | None) -> None:
    identity = {} if user is None else {"user": user[0], "group": user[1], "extra_groups": []}
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "TMPDIR": home, "LANG": "C.UTF-8"}
    try:
        proc = subprocess.Popen([tool, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=env, cwd=home, start_new_session=True, **identity)
    except FileNotFoundError:
        raise _Failed("tool-missing") from None
    except (OSError, ValueError):
        raise _Failed("tool-failed") from None
    try:
        code = proc.wait(timeout=TOOL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill(proc)
        raise _Failed("timeout") from None
    finally:
        _kill(proc)  # whatever the tool left running (a browser) goes with it
    if code != 0:
        raise _Failed("tool-failed")


def _kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - a process that survives SIGKILL
        pass


def _collect(printed: str, names: list[str]) -> dict[str, bytes]:
    """The PDFs the printer wrote, read without following a link and checked. All of them, or ``_Failed``."""
    kept: dict[str, bytes] = {}
    for name in names:
        try:
            fd = os.open(os.path.join(printed, name), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise _Failed("invalid") from None
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or info.st_size > MAX_PDF_BYTES:
                raise _Failed("invalid")
            data = fh.read(MAX_PDF_BYTES + 1)
        if pdf_check.check_pdf(data, name):
            raise _Failed("invalid")
        kept[name] = data
    return kept


def print_documents(mode: str, out_dir: str, work_dir: str, *, source_id: str, generated_at: str, generator_version: str,
                    tool: str = DEFAULT_TOOL, user: tuple[int, int] | None = None, max_output_bytes: int) -> PrintResult:
    """Print the documents of ``out_dir`` for ``mode`` in the new directory ``work_dir``.

    On success ``stage`` is ``work_dir/final``: the documents and, under ``docs/pdf/``, the PDFs. The caller removes
    ``work_dir`` when it is done with ``stage``. ``user`` is the (uid, gid) the printer runs as; None runs it as this user.
    Never raises for a printing problem: that is a ``PrintResult`` without a stage and with the reason.
    """
    previous_umask = os.umask(0o022)  # the printer is another user: what it reads must be readable whatever the umask was
    try:
        stage = os.path.join(work_dir, "final")
        printed = os.path.join(work_dir, "out")
        os.makedirs(stage, mode=0o755)
        os.makedirs(printed, mode=0o755)
        os.chmod(work_dir, 0o755)
        if user is not None:
            os.chown(printed, user[0], user[1])
        markdown, total = stage_documents(out_dir, stage)
        plan = make_plan(markdown, mode)
        if plan is None:
            return PrintResult(None, 0, "no-documents")
        args = ["--in", os.path.join(stage, "docs"), "--out", printed, "--source-id", source_id,
                "--generated-at", generated_at, "--generator-version", generator_version]
        for document in plan.documents:
            args += ["--doc", document]
        if plan.combined:
            args += ["--combined", plan.combined]
        for document, name in plan.singles:
            args += ["--single", f"{document}={name}"]
        _run_tool(tool, args, home=printed, user=user)
        files = _collect(printed, plan.names)
        if total + sum(len(data) for data in files.values()) + OUTPUT_HEADROOM_BYTES > max_output_bytes:
            raise _Failed("too-large")
        target_dir = os.path.join(stage, *PDF_DIR.strip("/").split("/"))
        os.makedirs(target_dir, mode=0o755)
        for name, data in files.items():
            fd = os.open(posixpath.join(target_dir, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        return PrintResult(stage, len(files), None, plan.printed_from)
    except _Failed as exc:
        return PrintResult(None, 0, exc.error)
    except Exception:  # noqa: BLE001 - a path or a message can hold the customer's words: say only that it failed
        return PrintResult(None, 0, "internal")
    finally:
        os.umask(previous_umask)
