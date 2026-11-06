"""Printing the documentation set: the plan, the private tree, the printer's one chance, and the checks on what it wrote."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from docugen.box import pdf as printing
from docugen.box.pdf import (COMBINED_NAME, ERRORS, LEAD_ORDER, MODES, document_order, make_plan, parse_mode, print_documents,
                             single_name)
from docugen.verify.manifest import check_tree
from tests.box.fake_printer import GOOD, make_printer

NOW_TEXT = "2026-11-03T12:00:00Z"
DOCS = {
    "docs/README.md": "# Project\n\nIndex.\n\n## Sources\nREADME.md\n",
    "docs/onboarding.md": "# Onboarding\n",
    "docs/architecture.md": "# Architecture\n\n![c](diagrams/c.svg)\n",
    "docs/api-reference.md": "# API\n",
    "docs/diagrams/c.mmd": "classDiagram\n  class A\n",
    "docs/diagrams/c.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
}
BIG_ENOUGH = 25 * 1024 * 1024


def put(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    out = tmp_path / "out"
    put(out, DOCS)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    return out, work


def run(trees, tool: str, mode: str = "both", **kw):
    out, work = trees
    return print_documents(mode, str(out), str(work / "print"), source_id="github:" + "a" * 40, generated_at=NOW_TEXT,
                           generator_version="0.0.0", tool=tool, max_output_bytes=kw.pop("max_output_bytes", BIG_ENOUGH), **kw)


# ── the setting ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [(None, "none"), ("", "none"), *[(m, m) for m in MODES]])
def test_the_modes(value: str | None, expected: str) -> None:
    assert parse_mode(value) == expected


@pytest.mark.parametrize("value", ["pdf", "NONE", "Each", "all", " both", "both ", "each,combined", "0", "true"])
def test_anything_else_is_refused(value: str) -> None:
    with pytest.raises(ValueError) as caught:
        parse_mode(value)
    assert value.strip() not in str(caught.value) or value.strip() in ("none", "each", "both")  # a value is never echoed


# ── the plan ────────────────────────────────────────────────────────────────────────────────────

def test_the_reading_order_is_the_index_onboarding_architecture_and_the_api_reference() -> None:
    assert LEAD_ORDER == ("README.md", "onboarding.md", "architecture.md", "api-reference.md")
    assert document_order(["api-reference.md", "architecture.md", "onboarding.md", "README.md"]) == list(LEAD_ORDER)


def test_the_order_of_the_rest_does_not_depend_on_the_order_they_come_in() -> None:
    rest = ["z.md", "y.md", "guides/b.md", "guides/a.md", "Beta.md", "alpha.md", "m.md", "k.md"]
    expected = ["alpha.md", "Beta.md", "guides/a.md", "guides/b.md", "k.md", "m.md", "y.md", "z.md"]
    for arrangement in (rest, list(reversed(rest)), sorted(rest, key=len), sorted(rest, key=lambda n: n[::-1])):
        assert document_order(["README.md", *arrangement]) == ["README.md", *expected]


def test_other_documents_follow_in_path_order_and_missing_lead_documents_are_skipped() -> None:
    assert document_order(["zeta.md", "guides/setup.md", "api-reference.md", "Alpha.md", "README.md"]) == [
        "README.md", "api-reference.md", "Alpha.md", "guides/setup.md", "zeta.md"]
    assert document_order([]) == []


def test_a_document_is_named_after_its_path_and_never_after_the_combined_pdf() -> None:
    assert single_name("README.md", set()) == "README.pdf"
    assert single_name("guides/setup.md", set()) == "guides-setup.pdf"
    assert single_name("documentation.md", set()) == "documentation-2.pdf"
    assert single_name("guides-setup.md", {"guides-setup.pdf"}) == "guides-setup-2.pdf"


@pytest.mark.parametrize("mode, combined, singles", [
    ("each", None, 4), ("combined", COMBINED_NAME, 0), ("both", COMBINED_NAME, 4)])
def test_the_plan_for_each_mode(mode: str, combined: str | None, singles: int) -> None:
    plan = make_plan(["api-reference.md", "README.md", "onboarding.md", "architecture.md"], mode)
    assert plan is not None and plan.combined == combined and len(plan.singles) == singles
    assert plan.documents == LEAD_ORDER
    assert plan.names == ([combined] if combined else []) + [name for _, name in plan.singles]


def test_there_is_no_plan_without_a_mode_or_without_documents() -> None:
    assert make_plan(["README.md"], "none") is None
    assert make_plan([], "both") is None


def test_the_plan_says_what_each_pdf_is_printed_from() -> None:
    plan = make_plan(["README.md", "guides/setup.md"], "both")
    assert plan is not None and plan.printed_from == {
        "docs/pdf/documentation.pdf": ("README.md", "guides/setup.md"),
        "docs/pdf/README.pdf": ("README.md",), "docs/pdf/guides-setup.pdf": ("guides/setup.md",)}


# ── printing ────────────────────────────────────────────────────────────────────────────────────

def test_the_printer_gets_the_contract_and_its_pdfs_land_under_docs_pdf(tmp_path: Path, trees) -> None:
    tool, record = make_printer(tmp_path)
    result = run(trees, tool)
    assert (result.error, result.pdfs) == (None, 5) and result.stage is not None
    stage = Path(result.stage)
    assert sorted(p.name for p in (stage / "docs" / "pdf").iterdir()) == [
        "README.pdf", "api-reference.pdf", "architecture.pdf", "documentation.pdf", "onboarding.pdf"]
    assert all((stage / "docs" / "pdf" / n).read_bytes() == GOOD for n in os.listdir(stage / "docs" / "pdf"))
    assert check_tree(str(stage), allow_pdf=True) == []                                  # what the caller will pack is acceptable

    got = json.loads(record.read_text())
    args = got["args"]
    assert args[:2] == ["--in", str(stage / "docs")] and args[2] == "--out"
    flags = [(args[i], args[i + 1]) for i in range(0, len(args), 2)]
    assert ("--source-id", "github:" + "a" * 40) in flags and ("--generated-at", NOW_TEXT) in flags and ("--generator-version", "0.0.0") in flags
    assert [v for f, v in flags if f == "--doc"] == list(LEAD_ORDER)
    assert [v for f, v in flags if f == "--combined"] == ["documentation.pdf"]
    assert [v for f, v in flags if f == "--single"] == ["README.md=README.pdf", "onboarding.md=onboarding.pdf",
                                                         "architecture.md=architecture.pdf", "api-reference.md=api-reference.pdf"]


def test_the_printer_sees_the_documents_and_nothing_else_and_an_empty_environment(tmp_path: Path, trees) -> None:
    out, _ = trees
    put(out, {"docs/.docugen/manifest.json": "{}"})
    tool, record = make_printer(tmp_path)
    result = run(trees, tool)
    got = json.loads(record.read_text())
    assert got["in_listing"] == sorted(p[len("docs/"):] for p in DOCS)                    # the manifest is not among them
    assert set(got["env"]) <= {"PATH", "HOME", "TMPDIR", "LANG"}
    assert got["cwd"] == str(Path(result.stage).parent / "out") and got["pgid_is_own"]


def test_the_agents_tree_is_not_touched(tmp_path: Path, trees) -> None:
    out, _ = trees
    tool, _ = make_printer(tmp_path)
    run(trees, tool)
    assert {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()} == set(DOCS)


@pytest.mark.parametrize("mode, names", [("each", 4), ("combined", 1)])
def test_a_mode_prints_only_what_it_names(tmp_path: Path, trees, mode: str, names: int) -> None:
    tool, _ = make_printer(tmp_path)
    result = run(trees, tool, mode)
    assert (result.error, result.pdfs) == (None, names)
    assert len(list((Path(result.stage) / "docs" / "pdf").iterdir())) == names


def test_files_the_printer_was_not_asked_for_are_ignored(tmp_path: Path, trees) -> None:
    tool, _ = make_printer(tmp_path, "extra")
    result = run(trees, tool, "combined")
    assert result.error is None and sorted(os.listdir(Path(result.stage) / "docs" / "pdf")) == ["documentation.pdf"]


@pytest.mark.parametrize("behaviour, error", [("fail", "tool-failed"), ("partial", "invalid"), ("hostile", "invalid"),
                                              ("symlink", "invalid"), ("notpdf", "invalid")])
def test_a_printer_that_goes_wrong_leaves_no_pdf_at_all(tmp_path: Path, trees, behaviour: str, error: str) -> None:
    tool, _ = make_printer(tmp_path, behaviour)
    result = run(trees, tool)
    assert (result.stage, result.pdfs, result.error) == (None, 0, error)


def test_a_printer_that_is_not_in_the_image(tmp_path: Path, trees) -> None:
    result = run(trees, str(tmp_path / "no-such-tool"))
    assert (result.stage, result.pdfs, result.error) == (None, 0, "tool-missing")


def test_a_printer_that_hangs_is_killed_and_reported(tmp_path: Path, trees, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(printing, "TOOL_TIMEOUT_SECONDS", 1)
    tool, record = make_printer(tmp_path, "sleep")
    started = time.monotonic()
    result = run(trees, tool)
    assert (result.stage, result.error) == (None, "timeout") and time.monotonic() - started < 30
    pid = json.loads(record.read_text())["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_pdfs_that_would_not_fit_the_upload_are_dropped_not_the_documents(tmp_path: Path, trees) -> None:
    tool, _ = make_printer(tmp_path, "big")
    result = run(trees, tool, max_output_bytes=printing.OUTPUT_HEADROOM_BYTES + 100_000)
    assert (result.stage, result.pdfs, result.error) == (None, 0, "too-large")


def test_a_session_with_no_markdown_has_nothing_to_print(tmp_path: Path) -> None:
    out, work = tmp_path / "out", tmp_path / "work"
    put(out, {"docs/diagrams/c.mmd": "classDiagram\n  class A\n"})
    work.mkdir()
    tool, record = make_printer(tmp_path)
    result = print_documents("both", str(out), str(work / "print"), source_id="x", generated_at=NOW_TEXT, generator_version="0",
                             tool=tool, max_output_bytes=BIG_ENOUGH)
    assert (result.stage, result.error) == (None, "no-documents") and not record.exists()


def test_a_tree_with_a_link_in_it_is_not_printed(tmp_path: Path, trees) -> None:
    out, _ = trees
    os.symlink("/etc/passwd", out / "docs" / "x.md")
    tool, record = make_printer(tmp_path)
    assert run(trees, tool).error == "internal" and not record.exists()


def test_the_errors_are_a_fixed_vocabulary() -> None:
    assert set(ERRORS) == {"no-documents", "tool-missing", "tool-failed", "timeout", "invalid", "too-large", "internal"}
    assert all(e.replace("-", "").isalpha() for e in ERRORS)
