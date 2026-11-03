"""The provenance manifest: built from the trees, valid by the validator that checks it, and never claiming more than the source holds."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from docugen.box import provenance
from docugen.box.provenance import SourceInfo, build_manifest, cited_paths, manifest_bytes
from docugen.verify.manifest import MANIFEST_PATH, check_manifest, check_tree

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
COMMIT = "0123456789abcdef0123456789abcdef01234567"
ARCHIVE = "ab" * 32
TOOLS = {"mermaid-cli": "12.2.0", "plantuml": "1.2026.9"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    src, out = tmp_path / "src", tmp_path / "out"
    put(src, {"app/api.py": "def handler():\n    return 1\n", "app/models.py": "class Session: ...\n",
              "README.md": "# repo\n", "infra/stack.py": "x = 1\n"})
    put(out, {
        "docs/index.md": "# Overview\n\nSee ![the classes](diagrams/class.svg).\n\n## Sources\n\n- `app/models.py:3-9`\n* app/api.py\nREADME.md\n",
        "docs/api.md": "# API\n\nThe [overview](index.md).\n\n## Sources\napp/api.py\ninfra/stack.py\n",
        "docs/diagrams/class.mmd": "classDiagram\n  class Session\n",
        "docs/diagrams/class.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
        "docs/diagrams/orphan.puml": "@startuml\n@enduml\n",
        "docs/diagrams/orphan.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
    })
    return src, out


def build(src: Path, out: Path, **kw) -> tuple[dict, list[tuple[str, str]]]:
    return build_manifest(str(out), str(src), SourceInfo("github", commit=COMMIT), generator_version="0.1.0", tools=TOOLS, now=NOW, **kw)


# ── the manifest is valid and says what is true ─────────────────────────────────────────────────────

def test_the_manifest_passes_the_validator_that_checks_it(trees: tuple[Path, Path]) -> None:
    src, out = trees
    manifest, unresolved = build(src, out)
    assert unresolved == []
    assert check_tree(str(out)) == []
    assert check_manifest(manifest, root=str(out), source_root=str(src)) == []
    assert check_manifest(json.loads(manifest_bytes(manifest)), root=str(out), source_root=str(src)) == []


def test_every_document_is_listed_with_its_kind_hash_and_sources(trees: tuple[Path, Path]) -> None:
    src, out = trees
    manifest, _ = build(src, out)
    docs = {d["path"]: d for d in manifest["documents"]}
    assert list(docs) == sorted(docs) == [
        "docs/api.md", "docs/diagrams/class.mmd", "docs/diagrams/class.svg", "docs/diagrams/orphan.puml",
        "docs/diagrams/orphan.svg", "docs/index.md"]
    assert {p: d["kind"] for p, d in docs.items()} == {
        "docs/api.md": "markdown", "docs/index.md": "markdown", "docs/diagrams/class.mmd": "diagram-source",
        "docs/diagrams/orphan.puml": "diagram-source", "docs/diagrams/class.svg": "diagram-svg", "docs/diagrams/orphan.svg": "diagram-svg"}
    for path, doc in docs.items():
        assert doc["sha256"] == sha((out / path).read_bytes())
    index_sources = docs["docs/index.md"]["sources"]
    assert index_sources == [{"path": p, "sha256": sha((src / p).read_bytes())} for p in ("README.md", "app/api.py", "app/models.py")]
    assert [s["path"] for s in docs["docs/api.md"]["sources"]] == ["app/api.py", "infra/stack.py"]


def test_a_diagram_and_its_rendering_take_the_sources_of_the_documents_that_embed_them(trees: tuple[Path, Path]) -> None:
    src, out = trees
    manifest, _ = build(src, out)
    docs = {d["path"]: d for d in manifest["documents"]}
    embedded = docs["docs/index.md"]["sources"]
    assert docs["docs/diagrams/class.svg"]["sources"] == embedded and docs["docs/diagrams/class.mmd"]["sources"] == embedded
    assert docs["docs/diagrams/orphan.svg"]["sources"] == [] and docs["docs/diagrams/orphan.puml"]["sources"] == []


def test_a_diagram_embedded_by_two_documents_takes_the_union(trees: tuple[Path, Path]) -> None:
    src, out = trees
    put(out, {"docs/more.md": "# More\n\n![again](diagrams/class.svg#view)\n\n## Sources\ninfra/stack.py\n"})
    manifest, _ = build(src, out)
    docs = {d["path"]: d for d in manifest["documents"]}
    assert [s["path"] for s in docs["docs/diagrams/class.svg"]["sources"]] == ["README.md", "app/api.py", "app/models.py", "infra/stack.py"]


def test_remote_and_absolute_images_inherit_nothing(trees: tuple[Path, Path]) -> None:
    src, out = trees
    put(out, {"docs/bad.md": "![x](https://evil.example/class.svg) ![y](/docs/diagrams/orphan.svg)\n\n## Sources\napp/api.py\n"})
    manifest, _ = build(src, out)
    docs = {d["path"]: d for d in manifest["documents"]}
    assert docs["docs/diagrams/orphan.svg"]["sources"] == []


def test_the_manifest_header(trees: tuple[Path, Path]) -> None:
    src, out = trees
    manifest, _ = build(src, out)
    assert manifest["schema"] == 1
    assert manifest["generator"] == {"name": "fibtec-docugen", "version": "0.1.0", "tools": TOOLS}
    assert manifest["source"] == {"kind": "github", "commit": COMMIT}
    assert manifest["generated_at"] == "2026-11-03T12:00:00Z"
    zipped, _ = build_manifest(str(out), str(src), SourceInfo("zip", archive_sha256=ARCHIVE), generator_version="0.1.0", tools=TOOLS, now=NOW)
    assert zipped["source"] == {"kind": "zip", "archive_sha256": ARCHIVE}


def test_the_time_is_utc_whatever_the_clock_says(trees: tuple[Path, Path]) -> None:
    src, out = trees
    plus_two = datetime(2026, 11, 3, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    manifest, _ = build_manifest(str(out), str(src), SourceInfo("github", commit=COMMIT), generator_version="0.1.0", tools={}, now=plus_two)
    assert manifest["generated_at"] == "2026-11-03T12:00:00Z"


def test_the_same_trees_give_the_same_manifest(trees: tuple[Path, Path]) -> None:
    src, out = trees
    first, _ = build(src, out)
    second, _ = build(src, out)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ── a manifest never claims more than the source holds ──────────────────────────────────────────────

def test_citations_that_are_not_regular_files_in_the_source_are_not_recorded(trees: tuple[Path, Path]) -> None:
    src, out = trees
    (src / "real_dir_link").symlink_to(src / "app")
    (src / "file_link.py").symlink_to(src / "app" / "api.py")
    (src.parent / "outside.txt").write_text("a real file next to the source, not in it")
    bad = ["../outside.txt", "app/../README.md", "app/missing.py", "app", "app/", "../etc/passwd", "app/../../etc/passwd", "/etc/passwd", "app\\api.py", "./app/api.py",
           "app//api.py", "real_dir_link/api.py", "file_link.py", "app/api.py/extra", "", "\x00"]
    put(out, {"docs/cites.md": "# C\n\n## Sources\napp/api.py\n" + "\n".join(b for b in bad if b) + "\n"})
    manifest, unresolved = build(src, out)
    doc = next(d for d in manifest["documents"] if d["path"] == "docs/cites.md")
    assert [s["path"] for s in doc["sources"]] == ["app/api.py"]
    assert [c for d, c in unresolved if d == "docs/cites.md"] == [b for b in bad if b]   # every one, in order
    assert check_manifest(manifest, root=str(out), source_root=str(src)) == []


def test_a_document_without_sources_is_listed_with_none(trees: tuple[Path, Path]) -> None:
    src, out = trees
    put(out, {"docs/bare.md": "# Nothing cited\n"})
    manifest, unresolved = build(src, out)
    assert next(d for d in manifest["documents"] if d["path"] == "docs/bare.md")["sources"] == [] and unresolved == []


def test_files_that_are_not_documents_are_left_out(trees: tuple[Path, Path]) -> None:
    src, out = trees
    put(out, {"docs/notes.txt": "x", "docs/run.sh": "#!/bin/sh\n", "docs/.docugen/other.json": "{}", "outside.md": "# no\n"})
    manifest, _ = build(src, out)
    assert "docs/notes.txt" not in json.dumps(manifest) and "outside.md" not in json.dumps(manifest) and "other.json" not in json.dumps(manifest)
    # the validator, not the builder, objects: a file outside docs/, a type that is not delivered, a hidden directory
    assert {f.rule for f in check_tree(str(out))} == {"outside-docs", "extension", "path-characters"}


def test_a_tampered_document_no_longer_matches_its_manifest(trees: tuple[Path, Path]) -> None:
    src, out = trees
    manifest, _ = build(src, out)
    (out / "docs" / "api.md").write_text("# API\n\nchanged after the manifest was built\n")
    assert {f.rule for f in check_manifest(manifest, root=str(out), source_root=str(src))} == {"manifest-document-hash"}


def test_a_changed_source_no_longer_matches_its_manifest(trees: tuple[Path, Path]) -> None:
    src, out = trees
    manifest, _ = build(src, out)
    (src / "app" / "api.py").write_text("def handler():\n    return 2\n")
    assert "manifest-source-hash" in {f.rule for f in check_manifest(manifest, root=str(out), source_root=str(src))}


# ── reading the Sources section ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("# T\n\n## Sources\na.py\nb/c.py\n", ["a.py", "b/c.py"]),
    ("## Sources\n- a.py\n* b.py\n+ c.py\n1. d.py\n2) e.py\n", ["a.py", "b.py", "c.py", "d.py", "e.py"]),
    ("## Sources\n`a.py`\n- `b.py:12`\nc.py:3-9\n", ["a.py", "b.py", "c.py"]),
    ("## Sources\na.py\na.py\n\n\nb.py\n", ["a.py", "b.py"]),
    ("## sources  \na.py\n", ["a.py"]),
    ("## SOURCES\na.py\n", ["a.py"]),
    ("## Sources\na.py\n## Next\nnot-a-source.py\n", ["a.py"]),
    ("## Sources\na.py\n# Top\nnot-a-source.py\n", ["a.py"]),
    ("## Sources\nold.py\n\n## Sources\nnew.py\n", ["new.py"]),     # the last section counts
    ("# T\n\nno sources here\n", []),
    ("## Sources\n", []),
    ("### Sources\na.py\n", []),                                    # only the ## heading
    ("## Source files\na.py\n", []),
    ("", []),
    ("## Sources\r\na.py\r\nb.py\r\n", ["a.py", "b.py"]),
])
def test_cited_paths(text: str, expected: list[str]) -> None:
    assert cited_paths(text) == expected


def test_a_runaway_sources_section_is_capped() -> None:
    text = "## Sources\n" + "\n".join(f"f{i}.py" for i in range(5000))
    assert len(cited_paths(text)) == 500


# ── the source of the documents ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    {"kind": "github"}, {"kind": "github", "commit": "abc"}, {"kind": "github", "commit": COMMIT.upper()},
    {"kind": "github", "commit": COMMIT[:7]}, {"kind": "github", "commit": COMMIT[:39]}, {"kind": "github", "commit": COMMIT + "0"},
    {"kind": "zip", "archive_sha256": ARCHIVE[:63]}, {"kind": "zip", "archive_sha256": ARCHIVE + "0"},
    {"kind": "zip", "archive_sha256": ARCHIVE.upper()},
    {"kind": "github", "commit": COMMIT, "archive_sha256": ARCHIVE}, {"kind": "github", "archive_sha256": ARCHIVE},
    {"kind": "zip"}, {"kind": "zip", "archive_sha256": "ab"}, {"kind": "zip", "archive_sha256": ARCHIVE, "commit": COMMIT},
    {"kind": "zip", "commit": COMMIT}, {"kind": "svn", "commit": COMMIT}, {"kind": ""},
])
def test_a_source_is_a_commit_or_an_archive_hash_and_nothing_else(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        SourceInfo(**kwargs)


def test_the_source_from_the_environment() -> None:
    assert SourceInfo.from_environ({"DOCUGEN_SOURCE_KIND": "github", "DOCUGEN_SOURCE_COMMIT": COMMIT}) == SourceInfo("github", commit=COMMIT)
    assert SourceInfo.from_environ({"DOCUGEN_SOURCE_KIND": "zip", "DOCUGEN_SOURCE_ARCHIVE_SHA256": ARCHIVE}) == SourceInfo("zip", archive_sha256=ARCHIVE)
    for env in ({}, {"DOCUGEN_SOURCE_KIND": "github"}, {"DOCUGEN_SOURCE_KIND": "zip", "DOCUGEN_SOURCE_COMMIT": COMMIT},
                {"DOCUGEN_SOURCE_KIND": "other"}):
        with pytest.raises(ValueError):
            SourceInfo.from_environ(env)


# ── serialising it ──────────────────────────────────────────────────────────────────────────────────

def test_the_manifest_is_canonical_json_and_nothing_is_written(trees: tuple[Path, Path]) -> None:
    src, out = trees
    before = sorted(str(p) for p in out.rglob("*"))
    manifest, _ = build(src, out)
    data = manifest_bytes(manifest)
    assert data.endswith(b"}\n") and not data.endswith(b"\n\n") and json.loads(data) == manifest
    assert data == manifest_bytes(json.loads(data))                                   # canonical: a round trip changes nothing
    assert list(json.loads(data)) == sorted(json.loads(data))                          # keys sorted
    assert sorted(str(p) for p in out.rglob("*")) == before                            # not a byte written in the agent's tree


def test_a_manifest_the_agent_left_is_ignored_by_the_builder(trees: tuple[Path, Path]) -> None:
    src, out = trees
    put(out, {MANIFEST_PATH: '{"schema": 1, "forged": true}'})
    manifest, _ = build(src, out)
    assert MANIFEST_PATH not in {d["path"] for d in manifest["documents"]} and b"forged" not in manifest_bytes(manifest)
    assert check_manifest(manifest, root=str(out), source_root=str(src)) == []


def test_the_standard_library_and_the_validators_only() -> None:
    import ast
    import sys
    tree = ast.parse(Path(provenance.__file__).read_text())
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
    assert imported <= set(sys.stdlib_module_names) | {"__future__", "docugen"}, imported
