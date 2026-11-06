"""A whole session that asks for PDFs: the setting reaches trusted code only, the PDFs are printed, checked and packed, and a
printer that goes wrong costs the PDFs and never the documents."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from docugen.box import session
from docugen.box.session import ConfigError, OutputSummary, SessionConfig
from docugen.verify.manifest import MANIFEST_PATH, check_manifest, check_tree
from tests.box.fake_printer import GOOD, make_printer
from tests.box.test_session import COMMIT, DOCS, NOW, SESSION_ID, status_lines, world  # noqa: F401 - `world` is a fixture

PDF_TEXT = "%PDF-1.7\n%%EOF\n"


def uploaded(w) -> tuple[list[str], dict, dict[str, bytes]]:
    (_, payload), = w.backend.uploads
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        files = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}  # type: ignore[union-attr]
    return sorted(files), json.loads(files[MANIFEST_PATH]), files


def output_line(lines: list[str]) -> str:
    return next(ln for ln in lines if ln.startswith("OUTPUT: "))


# ── a session that asked ────────────────────────────────────────────────────────────────────────

def test_a_session_that_asked_for_both_delivers_the_documents_and_their_pdfs(world, tmp_path: Path) -> None:
    w = world()
    w.environ["DOCUGEN_PDF"] = "both"
    second = "# Second\n\n## Sources\nREADME.md\n"
    w.environ["FAKE_WRITE"] = json.dumps({**DOCS, "docs/second.md": second})
    tool, record = make_printer(tmp_path)
    rc, lines = w.run(pdf_tool=tool)
    assert rc == 0 and lines[-1] == "STATUS: DONE"
    assert output_line(lines).endswith(" unresolved_sources=0 pdfs=3") and output_line(lines).startswith("OUTPUT: documents=4 ")

    names, manifest, files = uploaded(w)
    assert names == sorted([*DOCS, "docs/second.md", MANIFEST_PATH, "docs/pdf/documentation.pdf", "docs/pdf/index.pdf", "docs/pdf/second.pdf"])
    assert files["docs/pdf/index.pdf"] == GOOD
    entries = {d["path"]: d for d in manifest["documents"]}
    assert all(entries[name]["kind"] == "pdf" for name in names if name.endswith(".pdf"))
    cited = {name: [s["path"] for s in entries[name]["sources"]] for name in names if name.endswith(".pdf")}
    assert cited == {"docs/pdf/index.pdf": ["app/api.py", "app/models.py"],      # the sources of the document each was printed from
                     "docs/pdf/second.pdf": ["README.md"],
                     "docs/pdf/documentation.pdf": ["README.md", "app/api.py", "app/models.py"]}
    assert [d["kind"] for d in manifest["documents"]].count("pdf") == 3

    # what was uploaded is what the validators accept when PDFs were asked for, and not when they were not
    extracted = tmp_path / "delivered"
    for name, data in files.items():
        (extracted / name).parent.mkdir(parents=True, exist_ok=True)
        (extracted / name).write_bytes(data)
    assert check_tree(str(extracted), allow_pdf=True) == []
    assert check_manifest(manifest, root=str(extracted), source_root=str(w.src), allow_pdf=True) == []
    assert {f.rule for f in check_manifest(manifest, root=str(extracted), source_root=str(w.src))} == {"manifest-pdf"}
    assert {f.rule for f in check_tree(str(extracted))} == {"extension"}

    # the printer was told the moment and the source the manifest records, and the working tree is gone
    args = json.loads(record.read_text())["args"]
    assert args[args.index("--generated-at") + 1] == manifest["generated_at"] == "2026-11-03T12:00:00Z"
    assert args[args.index("--source-id") + 1] == f"github:{COMMIT}"
    assert list(w.work.iterdir()) == []
    assert {str(p.relative_to(w.out)) for p in w.out.rglob("*") if p.is_file()} == {*DOCS, "docs/second.md"}   # nothing written in the agent's tree


@pytest.mark.parametrize("mode, pdfs", [("each", ["docs/pdf/index.pdf"]), ("combined", ["docs/pdf/documentation.pdf"])])
def test_each_and_combined_deliver_only_what_they_name(world, tmp_path: Path, mode: str, pdfs: list[str]) -> None:
    w = world()
    w.environ["DOCUGEN_PDF"] = mode
    tool, _ = make_printer(tmp_path)
    rc, lines = w.run(pdf_tool=tool)
    names, _, _ = uploaded(w)
    assert rc == 0 and output_line(lines).endswith(" pdfs=1") and [n for n in names if n.endswith(".pdf")] == pdfs


@pytest.mark.parametrize("setting", [None, "none", ""])
def test_a_session_that_did_not_ask_never_runs_the_printer(world, tmp_path: Path, setting: str | None) -> None:
    w = world()
    if setting is not None:
        w.environ["DOCUGEN_PDF"] = setting
    tool, record = make_printer(tmp_path)
    rc, lines = w.run(pdf_tool=tool)
    assert rc == 0 and "pdfs=" not in output_line(lines) and "pdf_error" not in output_line(lines) and not record.exists()
    assert not [n for n in uploaded(w)[0] if n.endswith(".pdf")]


# ── a printer that goes wrong costs the PDFs, not the documents ─────────────────────────────────

@pytest.mark.parametrize("behaviour, error", [("fail", "tool-failed"), ("hostile", "invalid"), ("partial", "invalid"), ("notpdf", "invalid")])
def test_a_printer_that_goes_wrong_still_delivers_the_documents(world, tmp_path: Path, behaviour: str, error: str) -> None:
    w = world()
    w.environ["DOCUGEN_PDF"] = "both"
    tool, _ = make_printer(tmp_path, behaviour)
    rc, lines = w.run(pdf_tool=tool)
    assert rc == 0 and lines[-1] == "STATUS: DONE"
    assert output_line(lines).endswith(f" pdfs=0 pdf_error={error}")
    names, manifest, _ = uploaded(w)
    assert names == sorted([*DOCS, MANIFEST_PATH]) and all(d["kind"] != "pdf" for d in manifest["documents"])
    assert list(w.work.iterdir()) == []


def test_a_printer_that_is_not_in_the_image_still_delivers_the_documents(world, tmp_path: Path) -> None:
    w = world()
    w.environ["DOCUGEN_PDF"] = "combined"
    rc, lines = w.run(pdf_tool=str(tmp_path / "missing"))
    assert rc == 0 and output_line(lines).endswith(" pdfs=0 pdf_error=tool-missing") and len(w.backend.uploads) == 1


# ── the agent has no way to make or to ask for a PDF ────────────────────────────────────────────

@pytest.mark.parametrize("asked", ["none", "both"])
def test_a_pdf_the_agent_wrote_is_refused_whatever_the_session_asked(world, tmp_path: Path, asked: str) -> None:
    w = world()
    w.environ["DOCUGEN_PDF"] = asked
    w.environ["FAKE_WRITE"] = json.dumps({**DOCS, "docs/pdf/x.pdf": PDF_TEXT})
    tool, record = make_printer(tmp_path)
    rc, lines = w.run(pdf_tool=tool)
    assert rc == 1 and status_lines(lines)[-1] == "STATUS: FAILED VALIDATION" and not w.backend.uploads and not record.exists()


def test_the_agent_never_sees_the_setting(world, tmp_path: Path) -> None:
    w = world()
    w.environ["DOCUGEN_PDF"] = "both"
    tool, _ = make_printer(tmp_path)
    w.run(pdf_tool=tool)
    seen = json.loads(w.record.read_text())["env"]
    assert "DOCUGEN_PDF" not in seen and not [name for name in seen if name.startswith("DOCUGEN_")]


# ── the setting ─────────────────────────────────────────────────────────────────────────────────

def test_the_config_takes_the_setting_and_defaults_to_none(world) -> None:
    w = world()
    assert SessionConfig.from_environ(w.environ).pdf == "none"
    assert SessionConfig.from_environ({**w.environ, "DOCUGEN_PDF": "each"}).pdf == "each"
    assert SessionConfig.from_environ({**w.environ, "DOCUGEN_PDF": ""}).pdf == "none"


@pytest.mark.parametrize("value", ["pdf", "NONE", "both ", "each,combined", "../x", "x" * 500])
def test_a_setting_that_is_not_a_mode_starts_nothing_and_is_not_echoed(world, capsys: pytest.CaptureFixture[str], value: str) -> None:
    w = world()
    w.environ["DOCUGEN_PDF"] = value
    rc, lines = w.run()
    err = capsys.readouterr().err
    assert rc == session.EXIT_USAGE and lines == [] and w.backend.gets == 0
    assert err.strip() == "docugen-box: DOCUGEN_PDF is not one of none, each, combined, both" and value not in err.replace("none, each, combined, both", "")
    with pytest.raises(ConfigError):
        SessionConfig.from_environ(w.environ)


def test_the_output_line_with_pdfs() -> None:
    assert OutputSummary(3, 1024, 2).line() == "OUTPUT: documents=3 bytes=1024 unresolved_sources=2"
    assert OutputSummary(3, 1024, 2, 4).line() == "OUTPUT: documents=3 bytes=1024 unresolved_sources=2 pdfs=4"
    assert OutputSummary(3, 1024, 2, 0, "timeout").line() == "OUTPUT: documents=3 bytes=1024 unresolved_sources=2 pdfs=0 pdf_error=timeout"
