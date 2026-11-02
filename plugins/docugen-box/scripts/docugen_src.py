#!/usr/bin/env python3
"""docugen-src: look at the source tree of a DocuGen session without being able to leave it.

Claude Code 2.1.294 has no Glob or Grep tool, so the model would search with shell commands, and a shell
command can read anywhere (``/proc/self/environ`` holds the session token). The box therefore allows no
general shell search at all; it allows this script, which can only see the session's source directory.

    docugen-src tree     [PATH] [--depth N] [--max-entries N]
    docugen-src find     GLOB [--path PATH] [--max N]
    docugen-src grep     PATTERN [--path PATH] [--glob GLOB] [-i] [-C N] [--max N]
    docugen-src manifest [--out NAME]

Confinement: every path is resolved with ``realpath`` and refused unless it stays inside the source
directory (``$DOCUGEN_SRC_DIR``, default ``/workspace/src``). Symbolic links are never followed or listed
(ingest removes them; this is the second line). Output is capped, a search has a time limit, and files that
look binary or are very large are skipped, so no command can flood the model's context or hang a session.

``manifest`` records every file's path, size, SHA-256, line count and language in the work directory
(``$DOCUGEN_WORK_DIR``, default ``/workspace/work``), never in the output directory, which is delivered. The
hashes are what the provenance manifest later vouches for, so they come from this tool and not from the model.

Standard library only. Exit status: 0 ok, 1 grep found nothing, 2 refused or bad usage.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import signal
import stat
import sys
from collections.abc import Iterator
from types import FrameType

ROOT_ENV = "DOCUGEN_SRC_DIR"
WORK_ENV = "DOCUGEN_WORK_DIR"
TIMEOUT_ENV = "DOCUGEN_SRC_TIMEOUT"
DEFAULT_ROOT = "/workspace/src"
DEFAULT_WORK = "/workspace/work"
DEFAULT_TIMEOUT = 20

MAX_OUTPUT_BYTES = 64 * 1024
MAX_LINE_CHARS = 240
MAX_GREP_FILE_BYTES = 2 * 1024 * 1024
MAX_PATTERN_CHARS = 200
MAX_FILES = 50_000
SNIFF_BYTES = 8192

LANGUAGES = {
    ".py": "python", ".pyi": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".java": "java", ".kt": "kotlin",
    ".go": "go", ".rs": "rust", ".cs": "csharp", ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".sql": "sql", ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".toml": "toml", ".xml": "xml", ".gradle": "gradle", ".tf": "terraform", ".tfvars": "terraform",
    ".md": "markdown", ".rst": "restructuredtext", ".sh": "shell", ".bash": "shell", ".html": "html",
    ".css": "css", ".proto": "protobuf", ".graphql": "graphql", ".ini": "ini", ".cfg": "ini",
}
LANGUAGE_BY_NAME = {"Dockerfile": "docker", "Makefile": "make", "docker-compose.yml": "yaml",
                    "docker-compose.yaml": "yaml"}


class Refused(Exception):
    """The request would leave the source tree, or is not acceptable."""


class Timeout(Exception):
    """The search ran past its time limit."""


class Output:
    """Collects lines up to a byte budget so one command cannot flood the model's context."""

    def __init__(self, limit: int = MAX_OUTPUT_BYTES) -> None:
        self.limit = limit
        self.used = 0
        self.dropped = 0
        self.lines: list[str] = []

    def add(self, line: str) -> bool:
        size = len(line.encode("utf-8", "replace")) + 1
        if self.used + size > self.limit:
            self.dropped += 1
            return False
        self.used += size
        self.lines.append(line)
        return True

    def flush(self) -> None:
        if self.lines:
            sys.stdout.write("\n".join(self.lines) + "\n")
        if self.dropped:
            sys.stdout.write(f"[output truncated: {self.dropped} more line(s); narrow the path or the pattern]\n")


def source_root() -> str:
    root = os.path.realpath(os.environ.get(ROOT_ENV, DEFAULT_ROOT))
    if not os.path.isdir(root):
        raise Refused(f"the source directory {root} does not exist")
    return root


def resolve(root: str, rel: str) -> str:
    """Resolve ``rel`` (relative to the source root, or absolute) and refuse anything outside the root."""
    if "\x00" in rel:
        raise Refused("NUL character in a path")
    full = os.path.realpath(rel if os.path.isabs(rel) else os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        raise Refused("that path is outside the source tree")
    return full


def relpath(root: str, full: str) -> str:
    rel = os.path.relpath(full, root).replace(os.sep, "/")
    return "." if rel == "." else rel


def walk_files(root: str, start: str) -> Iterator[tuple[str, str, os.stat_result]]:
    """Regular files under ``start``, in a stable order. Symbolic links are skipped, never followed."""
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            if stat.S_ISREG(st.st_mode):
                yield relpath(root, full), full, st


def language_of(name: str) -> str:
    return LANGUAGE_BY_NAME.get(name) or LANGUAGES.get(os.path.splitext(name)[1].lower(), "other")


def looks_binary(head: bytes) -> bool:
    return b"\x00" in head


def clip(text: str) -> str:
    text = text.rstrip("\r\n")
    return text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + " [line cut]"


# ── tree ─────────────────────────────────────────────────────────────────────────────────────────

def cmd_tree(args: argparse.Namespace) -> int:
    root = source_root()
    start = resolve(root, args.path)
    if not os.path.isdir(start):
        raise Refused("tree needs a directory")
    out = Output()
    state = {"count": 0, "more": 0}

    def visit(directory: str, depth: int) -> None:
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name))
        for entry in entries:
            if entry.is_symlink():
                continue
            if state["count"] >= args.max_entries:
                state["more"] += 1
                continue
            rel = relpath(root, entry.path)
            if entry.is_dir(follow_symlinks=False):
                state["count"] += 1
                out.add(f"d {rel}/")
                if depth < args.depth:
                    visit(entry.path, depth + 1)
            elif entry.is_file(follow_symlinks=False):
                state["count"] += 1
                out.add(f"f {rel}  {entry.stat(follow_symlinks=False).st_size} bytes")

    visit(start, 1)
    if state["more"]:
        out.add(f"[{state['more']} more entries not listed; raise --max-entries or list a subdirectory]")
    out.flush()
    return 0


# ── find ─────────────────────────────────────────────────────────────────────────────────────────

def glob_matches(glob: str, rel: str) -> bool:
    """A glob without a slash is matched against the file name, one with a slash against the whole path.
    ``*`` also matches ``/`` (fnmatch semantics), so ``src/*.py`` finds nested files too."""
    target = rel if "/" in glob else rel.rsplit("/", 1)[-1]
    return fnmatch.fnmatchcase(target, glob)


def cmd_find(args: argparse.Namespace) -> int:
    root = source_root()
    start = resolve(root, args.path)
    if not os.path.isdir(start):
        raise Refused("--path needs a directory")
    out = Output()
    found = 0
    for rel, _full, st in walk_files(root, start):
        if glob_matches(args.glob, rel):
            found += 1
            if found <= args.max:
                out.add(f"{rel}  {st.st_size} bytes")
    if found > args.max:
        out.add(f"[{found - args.max} more match(es); narrow the glob or --path]")
    out.flush()
    return 0 if found else 1


# ── grep ─────────────────────────────────────────────────────────────────────────────────────────

def _on_alarm(_signum: int, _frame: FrameType | None) -> None:
    raise Timeout()


def cmd_grep(args: argparse.Namespace) -> int:
    root = source_root()
    start = resolve(root, args.path)
    if len(args.pattern) > MAX_PATTERN_CHARS:
        raise Refused(f"the pattern is longer than {MAX_PATTERN_CHARS} characters")
    try:
        rx = re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
    except re.error as exc:
        raise Refused(f"invalid regular expression: {exc}") from exc
    out = Output()
    matches = 0
    skipped_big = 0
    seconds = int(os.environ.get(TIMEOUT_ENV, DEFAULT_TIMEOUT))
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(max(1, seconds))
    try:
        files = [(os.path.relpath(start, root).replace(os.sep, "/"), start, os.lstat(start))] \
            if os.path.isfile(start) else walk_files(root, start)
        for rel, full, st in files:
            if args.glob and not glob_matches(args.glob, rel):
                continue
            if st.st_size > MAX_GREP_FILE_BYTES:
                skipped_big += 1
                continue
            with open(full, "rb") as fh:
                data = fh.read()
            if looks_binary(data[:SNIFF_BYTES]):
                continue
            lines = data.decode("utf-8", errors="replace").splitlines()
            wanted: dict[int, bool] = {}
            for i, line in enumerate(lines):
                if rx.search(line):
                    matches += 1
                    if matches > args.max:
                        break
                    for j in range(max(0, i - args.context), min(len(lines), i + args.context + 1)):
                        wanted[j] = wanted.get(j, False) or j == i
            for j in sorted(wanted):
                out.add(f"{rel}{':' if wanted[j] else '-'}{j + 1}{':' if wanted[j] else '-'} {clip(lines[j])}")
            if matches > args.max:
                break
    except Timeout:
        signal.alarm(0)
        out.flush()
        raise Refused(f"the search stopped after {seconds} s; narrow --path or --glob or simplify the pattern") from None
    finally:
        signal.alarm(0)
    if matches > args.max:
        out.add(f"[stopped after {args.max} matches; narrow the pattern, --path or --glob]")
    if skipped_big:
        out.add(f"[{skipped_big} file(s) over {MAX_GREP_FILE_BYTES} bytes were not searched]")
    out.flush()
    return 0 if matches else 1


# ── manifest ─────────────────────────────────────────────────────────────────────────────────────

def describe_file(full: str, name: str) -> dict[str, object]:
    h = hashlib.sha256()
    lines = 0
    last = b""
    binary = False
    first = True
    with open(full, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            if first:
                binary = looks_binary(chunk[:SNIFF_BYTES])
                first = False
            h.update(chunk)
            if not binary:
                lines += chunk.count(b"\n")
            last = chunk[-1:]
    size = os.path.getsize(full)
    if not binary and size and last != b"\n":
        lines += 1
    return {"size": size, "sha256": h.hexdigest(), "lines": 0 if binary else lines,
            "language": "binary" if binary else language_of(name), "binary": binary}


def cmd_manifest(args: argparse.Namespace) -> int:
    root = source_root()
    work = os.path.realpath(os.environ.get(WORK_ENV, DEFAULT_WORK))
    target = os.path.realpath(os.path.join(work, args.out))
    if target != work and not target.startswith(work + os.sep) or target == work:
        raise Refused("--out must be a file name inside the work directory")
    files: list[dict[str, object]] = []
    totals: dict[str, dict[str, int]] = {}
    for rel, full, _st in walk_files(root, root):
        if len(files) >= MAX_FILES:
            raise Refused(f"more than {MAX_FILES} files in the source tree")
        info = describe_file(full, os.path.basename(full))
        files.append({"path": rel, **info})
        lang = totals.setdefault(str(info["language"]), {"files": 0, "lines": 0, "bytes": 0})
        lang["files"] += 1
        lang["lines"] += int(info["lines"])  # type: ignore[call-overload]
        lang["bytes"] += int(info["size"])  # type: ignore[call-overload]
    doc = {"schema": 1, "files": files,
           "totals": {"files": len(files), "lines": sum(v["lines"] for v in totals.values()),
                      "bytes": sum(v["bytes"] for v in totals.values()), "by_language": totals}}
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    out = Output()
    t = doc["totals"]
    out.add(f"{t['files']} files, {t['lines']} lines, {t['bytes']} bytes")  # type: ignore[index]
    for lang, v in sorted(totals.items(), key=lambda kv: -kv[1]["lines"]):
        out.add(f"  {lang:<18} {v['files']:>6} files {v['lines']:>9} lines")
    biggest = sorted((f for f in files if not f["binary"]), key=lambda f: -int(f["lines"]))[:10]  # type: ignore[call-overload]
    out.add("largest text files:")
    for f in biggest:
        out.add(f"  {f['path']}  {f['lines']} lines")
    out.add(f"written: {os.path.relpath(target, work)} (in the work directory, not in the output)")
    out.flush()
    return 0


# ── command line ─────────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="docugen-src", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tree", help="list a directory, depth-limited")
    t.add_argument("path", nargs="?", default=".")
    t.add_argument("--depth", type=int, default=3)
    t.add_argument("--max-entries", type=int, default=300)
    f = sub.add_parser("find", help="files whose name or path matches a glob")
    f.add_argument("glob")
    f.add_argument("--path", default=".")
    f.add_argument("--max", type=int, default=200)
    g = sub.add_parser("grep", help="regular-expression search in text files")
    g.add_argument("pattern")
    g.add_argument("--path", default=".")
    g.add_argument("--glob", default="")
    g.add_argument("-i", "--ignore-case", action="store_true")
    g.add_argument("-C", "--context", type=int, default=0)
    g.add_argument("--max", type=int, default=100)
    m = sub.add_parser("manifest", help="record path, size, sha256, lines and language of every file")
    m.add_argument("--out", default="files.json")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name in ("depth", "max_entries", "max", "context"):
        if getattr(args, name, 0) < 0:
            print(f"docugen-src: --{name.replace('_', '-')} must not be negative", file=sys.stderr)
            return 2
    handlers = {"tree": cmd_tree, "find": cmd_find, "grep": cmd_grep, "manifest": cmd_manifest}
    try:
        return handlers[args.cmd](args)
    except Refused as exc:
        print(f"docugen-src: refused: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"docugen-src: {type(exc).__name__}: {exc.strerror or exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
