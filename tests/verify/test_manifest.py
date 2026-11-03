from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from docugen.verify.manifest import MANIFEST_PATH, check_manifest, check_tree, load_manifest


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
