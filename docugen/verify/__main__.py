"""Command line for the validators.

    python -I -m docugen.verify archive ARCHIVE
    python -I -m docugen.verify secrets  PATH [PATH ...]
    python -I -m docugen.verify markdown [--root DIR] FILE_OR_DIR [...]
    python -I -m docugen.verify svg      FILE [FILE ...]
    python -I -m docugen.verify diagram  [--render] FILE [FILE ...]
    python -I -m docugen.verify tree     DIR [--source DIR] [--allow-pdf]
    python -I -m docugen.verify pdf      FILE [FILE ...]

Exit status: 0 nothing found, 1 findings, 2 usage or I/O error. ``--json`` prints one JSON array.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import archive_safe, diagram_check, manifest, md_policy, pdf_check, secret_scan, svg_sanitize
from .common import Finding, read_text_limited


MARKDOWN_EXT = (".md",)
SVG_EXT = (".svg",)
PDF_EXT = (".pdf",)
DIAGRAM_EXT = (".mmd", ".mermaid", ".puml", ".plantuml", ".pu")


def _files(paths: list[str], exts: tuple[str, ...] | None = None) -> list[str]:
    """Files named on the command line, plus the files under any directory that have one of ``exts``.

    A file named explicitly is always checked, whatever its extension: the caller asked for it. A directory
    is searched, so only the kind of file the check is about is taken from it (``markdown docs/`` must not
    run the Markdown rules over the ``.svg`` files next to the documents).
    """
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p, followlinks=False):
                dirnames.sort()
                out.extend(os.path.join(dirpath, f) for f in sorted(filenames)
                           if exts is None or f.lower().endswith(exts))
        else:
            out.append(p)
    return out


def _run(args: argparse.Namespace) -> list[Finding]:
    findings: list[Finding] = []
    if args.cmd == "archive":
        return archive_safe.check_archive(args.paths[0])
    if args.cmd == "secrets":
        for p in args.paths:
            findings.extend(secret_scan.scan_tree(p) if os.path.isdir(p)
                            else secret_scan.scan_text(read_text_limited(p, 5 << 20), p))
        return findings
    if args.cmd == "markdown":
        for p in _files(args.paths, MARKDOWN_EXT):
            # judged by the repository-relative path: relative links and images resolve against it
            rel = os.path.relpath(p, args.root).replace(os.sep, "/")
            findings.extend(md_policy.check_markdown(read_text_limited(p, md_policy.MAX_BYTES), rel))
        return findings
    if args.cmd == "svg":
        for p in _files(args.paths, SVG_EXT):
            with open(p, "rb") as fh:
                findings.extend(svg_sanitize.check_svg(fh.read(svg_sanitize.MAX_BYTES + 1), p))
        return findings
    if args.cmd == "pdf":
        for p in _files(args.paths, PDF_EXT):
            with open(p, "rb") as fh:
                findings.extend(pdf_check.check_pdf(fh.read(pdf_check.MAX_BYTES + 1), p))
        return findings
    if args.cmd == "diagram":
        for p in _files(args.paths, DIAGRAM_EXT):
            found = diagram_check.check_source(read_text_limited(p, diagram_check.MAX_BYTES), p)
            findings.extend(found)
            if args.render and not found:  # render every file whose source passed, whatever the others did
                findings.extend(diagram_check.render_check(p))
        return findings
    if args.cmd == "tree":
        findings.extend(manifest.check_tree(args.paths[0], allow_pdf=args.allow_pdf))
        try:
            data = manifest.load_manifest(args.paths[0])
        except (OSError, ValueError) as exc:
            findings.append(Finding("manifest-missing", f"cannot read {manifest.MANIFEST_PATH}: {type(exc).__name__}"))
        else:
            findings.extend(manifest.check_manifest(data, args.paths[0], args.source, allow_pdf=args.allow_pdf))
        return findings
    raise AssertionError(args.cmd)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m docugen.verify", description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="print the findings as one JSON array")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("archive", "secrets", "markdown", "svg", "diagram", "tree", "pdf"):
        sp = sub.add_parser(name)
        sp.add_argument("paths", nargs="+" if name != "archive" and name != "tree" else 1)
        if name == "markdown":
            sp.add_argument("--root", default=".", help="the repository root that document paths are relative to (default: the current directory)")
        if name == "diagram":
            sp.add_argument("--render", action="store_true", help="also run mmdc or plantuml")
        if name == "tree":
            sp.add_argument("--source", help="the source tree, to verify the recorded source hashes")
            sp.add_argument("--allow-pdf", action="store_true",
                            help="accept the PDFs the box prints under docs/pdf/ (only for a session that asked for them)")
    args = ap.parse_args(argv)
    try:
        findings = _run(args)
    except (OSError, ValueError) as exc:
        print(f"docugen.verify: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([f.to_dict() for f in findings], indent=1))
    else:
        for f in findings:
            print(f)
        print(f"{len(findings)} finding(s)" if findings else "clean")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
