from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from docugen.verify.manifest import MANIFEST_PATH, MAX_PDFS, check_manifest, check_tree, load_manifest


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


COMMIT = "a" * 40


def build(tmp_path: Path) -> tuple[Path, Path, dict]:
    """A valid output tree, its source tree, and the matching manifest."""
    out, src = tmp_path / "out", tmp_path / "src"
    (out / "docs" / "diagrams").mkdir(parents=True)
    (out / "docs" / ".docugen").mkdir()
    (src / "app").mkdir(parents=True)
    code = b"class Session:\n    pass\n"
    (src / "app" / "session.py").write_bytes(code)
    docs = {
        "docs/overview.md": (b"# Overview\n![c](diagrams/classes.svg)\n", "markdown"),
        "docs/diagrams/classes.mmd": (b"classDiagram\n  class Session\n", "diagram-source"),
        "docs/diagrams/classes.svg": (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "diagram-svg"),
    }
    entries = []
    for rel, (data, kind) in docs.items():
        (out / rel).write_bytes(data)
        entries.append({"path": rel, "kind": kind, "sha256": sha(data),
                        "sources": [{"path": "app/session.py", "sha256": sha(code)}]})
    manifest = {"schema": 1, "generator": {"name": "docugen", "version": "0.0.0", "tools": {"mmdc": "12.2.0"}},
                "source": {"kind": "github", "commit": COMMIT}, "generated_at": "2026-11-02T16:00:00Z",
                "documents": entries}
    (out / MANIFEST_PATH).write_text(json.dumps(manifest))
    return out, src, manifest


def rules(findings) -> set[str]:
    return {f.rule for f in findings}


def test_valid_output_passes_everything(tmp_path: Path) -> None:
    out, src, manifest = build(tmp_path)
    assert check_tree(str(out)) == []
    assert check_manifest(load_manifest(str(out)), str(out), str(src)) == []
    assert check_manifest(manifest) == []


# ── the output tree ──────────────────────────────────────────────────────────────────────────────

def test_files_outside_docs_are_refused(tmp_path: Path) -> None:
    out, _, _ = build(tmp_path)
    (out / ".github" / "workflows").mkdir(parents=True)
    (out / ".github" / "workflows" / "x.yml").write_text("on: push\n")
    (out / "README.md").write_text("# overwritten\n")
    found = check_tree(str(out))
    assert rules(found) == {"outside-docs"}
    assert {f.path for f in found} == {".github/workflows/x.yml", "README.md"}


def test_symlinks_are_refused(tmp_path: Path) -> None:
    out, _, _ = build(tmp_path)
    os.symlink("/etc/passwd", out / "docs" / "passwd.md")
    os.symlink("..", out / "docs" / "up")
    assert rules(check_tree(str(out))) == {"symlink"}


def test_executable_bit_and_hard_links_are_refused(tmp_path: Path) -> None:
    out, _, _ = build(tmp_path)
    os.chmod(out / "docs" / "overview.md", 0o755)
    os.link(out / "docs" / "diagrams" / "classes.mmd", out / "docs" / "copy.mmd")
    assert rules(check_tree(str(out))) == {"executable", "hard-link"}


@pytest.mark.parametrize("name,rule", [
    ("page.html", "extension"),
    ("run.sh", "extension"),
    ("data.json", "extension"),
    ("noext", "extension"),
    (".hidden.md", "path-characters"),
    ("spécial.md", "path-characters"),
    ("a;b.md", "path-characters"),
    ("-flag.md", "path-characters"),
    ("x" * 250 + ".md", "path-characters"),
])
def test_names_and_extensions(tmp_path: Path, name: str, rule: str) -> None:
    out, _, _ = build(tmp_path)
    (out / "docs" / name).write_bytes(b"x")
    assert rule in rules(check_tree(str(out)))


def test_names_differing_only_by_case(tmp_path: Path) -> None:
    out, _, _ = build(tmp_path)
    (out / "docs" / "Overview.md").write_text("x")
    assert "duplicate-path" in rules(check_tree(str(out)))


def test_file_size_and_count_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from docugen.verify import manifest as m
    out, _, _ = build(tmp_path)
    monkeypatch.setattr(m, "MAX_FILE_BYTES", 10)
    assert "file-too-large" in rules(check_tree(str(out)))
    monkeypatch.setattr(m, "MAX_FILE_BYTES", 5 * 1024 * 1024)
    monkeypatch.setattr(m, "MAX_FILES", 2)
    assert "too-many-files" in rules(check_tree(str(out)))


# ── the manifest ─────────────────────────────────────────────────────────────────────────────────

def mutated(tmp_path: Path, edit) -> list:
    out, src, manifest = build(tmp_path)
    m = copy.deepcopy(manifest)
    edit(m)
    return check_manifest(m, str(out), str(src))


@pytest.mark.parametrize("name,edit,rule", [
    ("not an object", lambda m: m.clear() or m.update(schema=2), "manifest-schema"),
    ("schema", lambda m: m.update(schema=2), "manifest-schema"),
    ("generator", lambda m: m.pop("generator"), "manifest-generator"),
    ("source kind", lambda m: m.update(source={"kind": "svn"}), "manifest-source"),
    ("short commit", lambda m: m.update(source={"kind": "github", "commit": "abc123"}), "manifest-source"),
    ("zip without hash", lambda m: m.update(source={"kind": "zip"}), "manifest-source"),
    ("time", lambda m: m.update(generated_at="yesterday"), "manifest-time"),
    ("no documents", lambda m: m.update(documents=[]), "manifest-documents"),
    ("document outside docs", lambda m: m["documents"][0].update(path="README.md"), "manifest-document"),
    ("traversal", lambda m: m["documents"][0].update(path="docs/../../x.md"), "manifest-document"),
    ("kind", lambda m: m["documents"][0].update(kind="script"), "manifest-document"),
    ("document hash format", lambda m: m["documents"][0].update(sha256="xyz"), "manifest-document"),
    ("sources type", lambda m: m["documents"][0].update(sources="app/session.py"), "manifest-document"),
    ("source path", lambda m: m["documents"][0]["sources"][0].update(path="/etc/passwd"), "manifest-source-file"),
    ("source traversal", lambda m: m["documents"][0]["sources"][0].update(path="../x"), "manifest-source-file"),
    ("duplicate document", lambda m: m["documents"].append(copy.deepcopy(m["documents"][0])), "manifest-document"),
])
def test_manifest_rules(tmp_path: Path, name: str, edit, rule: str) -> None:
    if name == "not an object":
        assert rules(check_manifest(["x"])) == {"manifest-type"}
        return
    assert rule in rules(mutated(tmp_path, edit)), name


def test_a_tampered_document_is_detected(tmp_path: Path) -> None:
    out, src, manifest = build(tmp_path)
    (out / "docs" / "overview.md").write_text("# Overview\n[x](javascript:1)\n")
    assert rules(check_manifest(manifest, str(out), str(src))) == {"manifest-document-hash"}


def test_a_source_that_does_not_match_its_recorded_hash(tmp_path: Path) -> None:
    out, src, manifest = build(tmp_path)
    (src / "app" / "session.py").write_text("class Session:\n    changed = True\n")
    assert rules(check_manifest(manifest, str(out), str(src))) == {"manifest-source-hash"}


def test_a_source_that_is_not_in_the_source_tree(tmp_path: Path) -> None:
    out, src, manifest = build(tmp_path)
    (src / "app" / "session.py").unlink()
    assert rules(check_manifest(manifest, str(out), str(src))) == {"manifest-source-missing"}


def test_listed_but_missing_and_present_but_unlisted(tmp_path: Path) -> None:
    out, src, manifest = build(tmp_path)
    (out / "docs" / "diagrams" / "classes.svg").unlink()
    (out / "docs" / "extra.md").write_text("# not in the manifest\n")
    assert rules(check_manifest(manifest, str(out), str(src))) == {"manifest-document-missing", "manifest-unlisted"}


# ── PDFs: printed by the box when the session asked for them, never written by the agent ────────────

from .test_pdf_check import HIDDEN, page_tree, pdf  # noqa: E402 - the PDF builders live next to the PDF check's tests


def add_pdf(out: Path, manifest: dict, name: str, data: bytes | None = None, *, where: str = "docs/pdf") -> str:
    """Put a PDF into the tree and list it in the manifest; return its path."""
    data = pdf(*page_tree()) if data is None else data
    rel = f"{where}/{name}"
    (out / rel).parent.mkdir(parents=True, exist_ok=True)
    (out / rel).write_bytes(data)
    manifest["documents"].append({"path": rel, "kind": "pdf", "sha256": sha(data), "sources": []})
    (out / MANIFEST_PATH).write_text(json.dumps(manifest))
    return rel


def test_a_pdf_is_refused_unless_the_session_asked_for_one(tmp_path: Path) -> None:
    out, _, manifest = build(tmp_path)
    rel = add_pdf(out, manifest, "documentation.pdf")
    found = check_tree(str(out))
    assert rules(found) == {"extension"} and {f.path for f in found} == {rel}


def test_a_pdf_the_session_asked_for_passes_the_tree_and_the_manifest(tmp_path: Path) -> None:
    out, src, manifest = build(tmp_path)
    add_pdf(out, manifest, "documentation.pdf")
    add_pdf(out, manifest, "overview.pdf", pdf(*page_tree(b"/Lang (en) ")))
    assert check_tree(str(out), allow_pdf=True) == []
    assert check_manifest(load_manifest(str(out)), str(out), str(src), allow_pdf=True) == []


@pytest.mark.parametrize("where", ["docs", "docs/diagrams", "docs/pdf/nested", "docs/PDF", "docs/pdfs"])
def test_a_pdf_is_accepted_only_directly_under_docs_pdf(tmp_path: Path, where: str) -> None:
    out, _, manifest = build(tmp_path)
    rel = add_pdf(out, manifest, "x.pdf", where=where)
    found = check_tree(str(out), allow_pdf=True)
    assert rules(found) == {"pdf-location"} and {f.path for f in found} == {rel}


def test_a_pdf_outside_docs_is_still_outside_docs(tmp_path: Path) -> None:
    out, _, _ = build(tmp_path)
    (out / "x.pdf").write_bytes(pdf(*page_tree()))
    assert rules(check_tree(str(out), allow_pdf=True)) == {"outside-docs"}


def test_a_pdf_that_can_act_is_refused_even_when_pdfs_are_allowed(tmp_path: Path) -> None:
    out, _, manifest = build(tmp_path)
    rel = add_pdf(out, manifest, "evil.pdf", pdf(*page_tree(b"/OpenAction << /S /JavaScript /JS (x) >> ")))
    found = check_tree(str(out), allow_pdf=True)
    assert rules(found) == {"pdf-active-content"} and {f.path for f in found} == {rel}


def test_a_file_that_is_named_pdf_and_is_not_one_is_refused(tmp_path: Path) -> None:
    out, _, manifest = build(tmp_path)
    add_pdf(out, manifest, "x.pdf", b"<html><script>alert(1)</script></html>")
    assert "pdf-header" in rules(check_tree(str(out), allow_pdf=True))


def test_a_symlinked_pdf_is_refused_and_not_read(tmp_path: Path) -> None:
    out, _, _ = build(tmp_path)
    (out / "docs" / "pdf").mkdir()
    target = tmp_path / "outside.pdf"
    target.write_bytes(pdf(*page_tree()))
    os.symlink(target, out / "docs" / "pdf" / "x.pdf")
    assert rules(check_tree(str(out), allow_pdf=True)) == {"symlink"}      # refused before the PDF rules ever open it


def test_pdfs_may_be_bigger_than_other_files_up_to_their_own_cap(tmp_path: Path) -> None:
    out, _, manifest = build(tmp_path)
    padding = b"%" + b"p" * (6 * 1024 * 1024) + b"\n"                  # a comment line: over 5 MiB, under 10
    add_pdf(out, manifest, "big.pdf", pdf(*page_tree(), padding))
    assert check_tree(str(out), allow_pdf=True) == []
    (out / "docs" / "big.md").write_bytes(b"# big\n" + b"x" * (6 * 1024 * 1024))
    assert rules(check_tree(str(out), allow_pdf=True)) == {"file-too-large"}


def test_exactly_the_cap_of_pdfs_is_accepted(tmp_path: Path) -> None:
    out, _, manifest = build(tmp_path)
    for i in range(MAX_PDFS):
        add_pdf(out, manifest, f"d{i:03d}.pdf")
    assert check_tree(str(out), allow_pdf=True) == []


def test_the_number_of_pdfs_is_capped(tmp_path: Path) -> None:
    out, _, manifest = build(tmp_path)
    for i in range(MAX_PDFS + 1):
        add_pdf(out, manifest, f"d{i:03d}.pdf")
    assert rules(check_tree(str(out), allow_pdf=True)) == {"too-many-pdfs"}


def test_a_manifest_that_lists_a_pdf_nobody_asked_for_is_refused(tmp_path: Path) -> None:
    out, src, manifest = build(tmp_path)
    add_pdf(out, manifest, "documentation.pdf")
    assert rules(check_manifest(manifest, str(out), str(src))) == {"manifest-pdf"}
    assert check_manifest(manifest, str(out), str(src), allow_pdf=True) == []


def test_the_kind_and_the_extension_of_a_document_must_agree(tmp_path: Path) -> None:
    out, src, manifest = build(tmp_path)
    for entry in manifest["documents"]:
        if entry["path"] == "docs/overview.md":
            entry["kind"] = "pdf"
    assert rules(check_manifest(manifest, allow_pdf=True)) == {"manifest-pdf"}
    out, src, manifest = build(tmp_path / "again")
    rel = add_pdf(out, manifest, "x.pdf")
    for entry in manifest["documents"]:
        if entry["path"] == rel:
            entry["kind"] = "markdown"
    assert rules(check_manifest(manifest, allow_pdf=True)) == {"manifest-pdf"}


def test_a_pdf_that_is_listed_with_the_wrong_hash_is_refused(tmp_path: Path) -> None:
    out, src, manifest = build(tmp_path)
    rel = add_pdf(out, manifest, "x.pdf")
    (out / rel).write_bytes(pdf(*page_tree(b"/Lang (de) ")))
    assert rules(check_manifest(manifest, str(out), str(src), allow_pdf=True)) == {"manifest-document-hash"}


def test_a_pdf_that_is_in_the_tree_and_not_in_the_manifest_is_refused(tmp_path: Path) -> None:
    out, src, manifest = build(tmp_path)
    (out / "docs" / "pdf").mkdir()
    (out / "docs" / "pdf" / "x.pdf").write_bytes(pdf(*page_tree()))
    assert rules(check_manifest(manifest, str(out), str(src), allow_pdf=True)) == {"manifest-unlisted"}


def test_the_hidden_object_trick_is_refused_at_the_tree_level_too(tmp_path: Path) -> None:
    out, _, manifest = build(tmp_path)
    body = b"x\n" + HIDDEN
    add_pdf(out, manifest, "x.pdf", pdf(*page_tree(), b"5 0 obj\n<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream\nendobj\n"))
    assert rules(check_tree(str(out), allow_pdf=True)) == {"pdf-stream-length"}
