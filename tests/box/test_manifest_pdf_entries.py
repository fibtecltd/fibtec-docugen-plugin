"""Slice Q0 of the PDF pass plan (docs/fibtec-docugen-pdf-pass-plan.md in fibtec-docugen).

The PDF pass starts from a result that was already delivered: the service holds its zip (the documents and the manifest) and has
no source tree. Two questions have to be answered before anything is built on that:

1. Does ``check_manifest(..., source_root=None, allow_pdf=True)`` accept a manifest that lists PDFs? (Yes, below.)
2. Can the service append the PDF entries to the delivered manifest from that manifest alone, and get exactly what the box
   writes when it prints during a session? (Yes, below, for every print mode.)

``append_pdf_entries`` is the reference for what the pass's service code (slice Q1's helper) has to do. It lives here, in a
test, until that helper exists; then these tests should call the helper instead.

What these tests do NOT settle, because the plan leaves it open: what ``generated_at`` and the generator's version are for a
pass. Here both stay as they were, so that the comparison with the box's manifest is exact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docugen.box.pdf import make_plan
from docugen.box.provenance import manifest_bytes
from docugen.verify.manifest import MANIFEST_PATH, check_manifest, check_tree
from tests.verify.test_pdf_check import page_tree, pdf

from .test_provenance import build, put, sha, trees  # noqa: F401 - ``trees`` is a fixture used by name below

LANGUAGES = (b"en", b"fr", b"de", b"it", b"es", b"nl")


def rules(findings: list) -> set[str]:
    return {f.rule for f in findings}


def markdown_names(out: Path) -> list[str]:
    return sorted(str(p.relative_to(out / "docs")) for p in (out / "docs").rglob("*.md"))


def write_pdfs(out: Path, paths: list[str]) -> dict[str, bytes]:
    """A distinct, valid PDF at each path (``docs/pdf/NAME.pdf``)."""
    made = {}
    for i, rel in enumerate(sorted(paths)):
        made[rel] = pdf(*page_tree(b"/Lang (" + LANGUAGES[i % len(LANGUAGES)] + b") "))
    put(out, made)
    return made


def append_pdf_entries(delivered: dict, printed_from: dict[str, tuple[str, ...]], pdf_bytes: dict[str, bytes]) -> dict:
    """What the service does with a delivered manifest and the PDFs it was handed: no source tree, no trust in the box.

    A PDF's sources are the sources of the documents it was printed from, each source once, in path order. A document the
    manifest does not list (which the service never asks for) adds nothing, as in ``build_manifest``.
    """
    listed = {d["path"]: d for d in delivered["documents"]}
    entries = []
    for pdf_path, names in printed_from.items():
        merged: dict[str, str] = {}
        for name in names:
            for source in listed.get("docs/" + name, {"sources": []})["sources"]:
                merged[source["path"]] = source["sha256"]
        entries.append({"path": pdf_path, "kind": "pdf", "sha256": sha(pdf_bytes[pdf_path]),
                        "sources": [{"path": path, "sha256": merged[path]} for path in sorted(merged)]})
    return {**delivered, "documents": sorted([*delivered["documents"], *entries], key=lambda d: d["path"])}


def delivered_then_printed(src: Path, out: Path, mode: str) -> tuple[dict, dict, dict[str, tuple[str, ...]], dict[str, bytes]]:
    """The manifest of a result delivered without PDFs, the manifest the box would have written had it printed, and what printed."""
    delivered, _ = build(src, out)
    plan = make_plan(markdown_names(out), mode)
    assert plan is not None
    printed_from = plan.printed_from
    pdf_bytes = write_pdfs(out, list(printed_from))
    boxed, _ = build(src, out, pdf_documents=printed_from)
    return delivered, boxed, printed_from, pdf_bytes


@pytest.mark.parametrize("mode", ["each", "combined", "both"])
def test_a_manifest_that_lists_pdfs_passes_without_a_source_tree(trees: tuple[Path, Path], mode: str) -> None:  # noqa: F811
    src, out = trees
    _, boxed, _, _ = delivered_then_printed(src, out, mode)
    assert check_tree(str(out), allow_pdf=True) == []
    assert check_manifest(boxed, root=str(out), source_root=None, allow_pdf=True) == []
    assert check_manifest(boxed, allow_pdf=True) == []


def test_the_same_manifest_is_refused_unless_pdfs_are_allowed(trees: tuple[Path, Path]) -> None:  # noqa: F811
    src, out = trees
    _, boxed, printed_from, _ = delivered_then_printed(src, out, "both")
    found = check_manifest(boxed, root=str(out), source_root=None)
    assert rules(found) == {"manifest-pdf"} and len(found) == len(printed_from)


@pytest.mark.parametrize("mode", ["each", "combined", "both"])
def test_the_service_can_append_the_pdf_entries_exactly_as_the_box_writes_them(trees: tuple[Path, Path], mode: str) -> None:  # noqa: F811
    src, out = trees
    delivered, boxed, printed_from, pdf_bytes = delivered_then_printed(src, out, mode)
    appended = append_pdf_entries(delivered, printed_from, pdf_bytes)
    assert appended == boxed
    assert manifest_bytes(appended) == manifest_bytes(boxed)
    assert check_manifest(appended, root=str(out), source_root=None, allow_pdf=True) == []


def test_a_source_cited_by_two_documents_is_listed_once_in_the_combined_pdf(trees: tuple[Path, Path]) -> None:  # noqa: F811
    src, out = trees
    delivered, boxed, printed_from, pdf_bytes = delivered_then_printed(src, out, "combined")
    entry = next(d for d in append_pdf_entries(delivered, printed_from, pdf_bytes)["documents"] if d["kind"] == "pdf")
    paths = [s["path"] for s in entry["sources"]]
    assert paths == ["README.md", "app/api.py", "app/models.py", "infra/stack.py"]   # app/api.py is cited by index.md and api.md
    assert entry == next(d for d in boxed["documents"] if d["kind"] == "pdf")


def test_a_pdf_of_a_document_that_cites_nothing_has_no_sources_in_either_manifest(trees: tuple[Path, Path]) -> None:  # noqa: F811
    src, out = trees
    put(out, {"docs/notes.md": "# Notes\n\nNothing here points at the source.\n"})
    delivered, boxed, printed_from, pdf_bytes = delivered_then_printed(src, out, "each")
    assert next(d for d in boxed["documents"] if d["path"] == "docs/pdf/notes.pdf")["sources"] == []
    assert append_pdf_entries(delivered, printed_from, pdf_bytes) == boxed


def test_the_delivered_manifest_is_not_changed_by_appending(trees: tuple[Path, Path]) -> None:  # noqa: F811
    src, out = trees
    delivered, _, printed_from, pdf_bytes = delivered_then_printed(src, out, "both")
    before = manifest_bytes(delivered)
    append_pdf_entries(delivered, printed_from, pdf_bytes)
    assert manifest_bytes(delivered) == before


def test_without_a_source_tree_a_made_up_source_hash_is_not_caught_so_the_service_must_derive_it(trees: tuple[Path, Path]) -> None:  # noqa: F811
    """A known limit, pinned on purpose: only a source tree can tell a made-up source hash from a real one.

    The pass has no source tree. So it must never accept the box's manifest for the PDFs: it derives the entries from the
    manifest it already verified (``append_pdf_entries``). If ``check_manifest`` ever learns to check this without a source
    tree, replace this test.
    """
    src, out = trees
    _, boxed, _, _ = delivered_then_printed(src, out, "combined")
    entry = next(d for d in boxed["documents"] if d["kind"] == "pdf")
    entry["sources"][0]["sha256"] = "0" * 64
    assert check_manifest(boxed, root=str(out), source_root=None, allow_pdf=True) == []
    assert rules(check_manifest(boxed, root=str(out), source_root=str(src), allow_pdf=True)) == {"manifest-source-hash"}


def test_a_pdf_that_changed_or_went_missing_or_was_added_is_caught_from_the_tree_alone(trees: tuple[Path, Path]) -> None:  # noqa: F811
    src, out = trees
    delivered, _, printed_from, pdf_bytes = delivered_then_printed(src, out, "both")
    appended = append_pdf_entries(delivered, printed_from, pdf_bytes)
    first, second = sorted(printed_from)[:2]

    (out / first).write_bytes(pdf_bytes[first] + b"\n%changed\n")
    assert "manifest-document-hash" in rules(check_manifest(appended, root=str(out), source_root=None, allow_pdf=True))
    (out / first).write_bytes(pdf_bytes[first])

    (out / second).unlink()
    assert "manifest-document-missing" in rules(check_manifest(appended, root=str(out), source_root=None, allow_pdf=True))
    (out / second).write_bytes(pdf_bytes[second])

    put(out, {"docs/pdf/extra.pdf": pdf(*page_tree(b"/Lang (sv) "))})
    assert rules(check_manifest(appended, root=str(out), source_root=None, allow_pdf=True)) == {"manifest-unlisted"}


def test_the_manifest_file_itself_is_never_a_listed_document(trees: tuple[Path, Path]) -> None:  # noqa: F811
    src, out = trees
    delivered, _, printed_from, pdf_bytes = delivered_then_printed(src, out, "both")
    appended = append_pdf_entries(delivered, printed_from, pdf_bytes)
    assert MANIFEST_PATH not in {d["path"] for d in appended["documents"]}
