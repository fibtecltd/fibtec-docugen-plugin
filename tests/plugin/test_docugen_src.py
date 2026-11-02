"""docugen-src: it must show the model the source tree, and nothing outside it."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "docugen-box" / "scripts" / "docugen_src.py"


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    src, work, outside = tmp_path / "src", tmp_path / "work", tmp_path / "outside"
    for d in (src / "app", src / "docs", src / "node_modules", work, outside):
        d.mkdir(parents=True, exist_ok=True)
    (src / "app" / "models.py").write_text("class Tenant:\n    pass\n\nclass Session:\n    pass\n")
    (src / "app" / "api.py").write_text("def create():\n    return 1\n")
    (src / "README.md").write_text("# Title\nSee TENANT docs\n")
    (src / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    (src / "no-newline.txt").write_text("a\nb")
    (outside / "secret.txt").write_text("token=outside-the-box\n")
    return {"src": src, "work": work, "outside": outside, "root": tmp_path}


def run(tree: dict[str, Path], *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    e = {"PATH": os.environ.get("PATH", ""), "DOCUGEN_SRC_DIR": str(tree["src"]), "DOCUGEN_WORK_DIR": str(tree["work"])}
    e.update(env or {})
    return subprocess.run([sys.executable, "-P", str(SCRIPT), *args], capture_output=True, text=True, env=e, check=False)


# ── what it shows ────────────────────────────────────────────────────────────────────────────────

def test_tree_lists_directories_first_with_sizes(tree: dict[str, Path]) -> None:
    r = run(tree, "tree")
    assert r.returncode == 0
    lines = r.stdout.splitlines()
    assert lines[0].startswith("d app/") and "f app/models.py" in r.stdout
    assert any(line.startswith("f README.md") for line in lines)


def test_tree_depth_and_entry_limits(tree: dict[str, Path]) -> None:
    assert "app/models.py" not in run(tree, "tree", "--depth", "1").stdout
    r = run(tree, "tree", "--max-entries", "2")
    assert "more entries not listed" in r.stdout


def test_tree_of_a_subdirectory(tree: dict[str, Path]) -> None:
    r = run(tree, "tree", "app")
    assert "f app/api.py" in r.stdout and "README.md" not in r.stdout


def test_find_matches_names_and_paths(tree: dict[str, Path]) -> None:
    sizes = {n: (tree["src"] / "app" / n).stat().st_size for n in ("api.py", "models.py")}
    assert run(tree, "find", "*.py").stdout.splitlines() == [
        f"app/api.py  {sizes['api.py']} bytes", f"app/models.py  {sizes['models.py']} bytes"]
    assert "app/models.py" in run(tree, "find", "app/*.py").stdout
    nothing = run(tree, "find", "*.nonexistent")
    assert nothing.returncode == 1 and nothing.stdout == ""


def test_grep_prints_path_line_and_text(tree: dict[str, Path]) -> None:
    r = run(tree, "grep", r"^class \w+", "--glob", "*.py")
    assert r.returncode == 0
    assert r.stdout.splitlines() == ["app/models.py:1: class Tenant:", "app/models.py:4: class Session:"]


def test_grep_options(tree: dict[str, Path]) -> None:
    assert run(tree, "grep", "tenant").returncode == 1          # case sensitive by default
    assert "README.md:2:" in run(tree, "grep", "-i", "tenant docs").stdout
    ctx = run(tree, "grep", "def create", "-C", "1").stdout.splitlines()
    assert ctx == ["app/api.py:1: def create():", "app/api.py-2-     return 1"]
    assert len(run(tree, "grep", "class", "--max", "1").stdout.splitlines()) == 2  # one match + the notice


def test_grep_skips_binary_files_and_oversized_lines_are_cut(tree: dict[str, Path]) -> None:
    (tree["src"] / "blob.bin").write_bytes(b"findme\x00\x01\x02")
    (tree["src"] / "long.txt").write_text("findme " + "x" * 1000 + "\n")
    r = run(tree, "grep", "findme")
    assert "blob.bin" not in r.stdout
    assert "long.txt:1:" in r.stdout and "[line cut]" in r.stdout and len(r.stdout) < 400


def test_grep_a_single_file(tree: dict[str, Path]) -> None:
    assert run(tree, "grep", "pass", "--path", "app/models.py").stdout.count("\n") == 2


# ── manifest ─────────────────────────────────────────────────────────────────────────────────────

def test_manifest_records_hashes_lines_and_languages(tree: dict[str, Path]) -> None:
    r = run(tree, "manifest")
    assert r.returncode == 0 and "files, " in r.stdout and "python" in r.stdout
    doc = json.loads((tree["work"] / "files.json").read_text())
    by_path = {f["path"]: f for f in doc["files"]}
    models = by_path["app/models.py"]
    assert models["sha256"] == hashlib.sha256((tree["src"] / "app" / "models.py").read_bytes()).hexdigest()
    assert (models["lines"], models["language"], models["binary"]) == (5, "python", False)
    assert by_path["no-newline.txt"]["lines"] == 2          # a last line without a newline still counts
    assert by_path["logo.png"]["binary"] is True and by_path["logo.png"]["lines"] == 0
    assert doc["totals"]["files"] == len(doc["files"]) and doc["totals"]["by_language"]["python"]["files"] == 2


def test_manifest_goes_to_the_work_directory_never_the_source(tree: dict[str, Path]) -> None:
    before = sorted(p.name for p in tree["src"].rglob("*"))
    run(tree, "manifest")
    assert sorted(p.name for p in tree["src"].rglob("*")) == before


@pytest.mark.parametrize("name", ["../escape.json", "/tmp/escape.json", "sub/../../escape.json", ".", ""])
def test_manifest_out_must_stay_inside_the_work_directory(tree: dict[str, Path], name: str) -> None:
    r = run(tree, "manifest", "--out", name)
    assert r.returncode == 2 and "refused" in r.stderr
    assert not (tree["root"] / "escape.json").exists()


# ── confinement ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    ["tree", "/"], ["tree", ".."], ["tree", "../.."], ["tree", "app/../.."], ["tree", "/etc"], ["tree", "/proc/self"],
    ["find", "*", "--path", "/etc"], ["find", "*", "--path", ".."],
    ["grep", "root", "--path", "/etc/passwd"], ["grep", "x", "--path", "../outside"], ["grep", "x", "--path", "/proc/self/environ"],
])
def test_paths_outside_the_source_are_refused(tree: dict[str, Path], args: list[str]) -> None:
    r = run(tree, *args)
    assert r.returncode == 2 and "outside the source tree" in r.stderr and r.stdout == ""


def test_symlinks_are_never_followed(tree: dict[str, Path]) -> None:
    os.symlink(tree["outside"], tree["src"] / "linked-dir")
    os.symlink(tree["outside"] / "secret.txt", tree["src"] / "linked-file")
    assert "linked" not in run(tree, "tree").stdout
    assert "linked" not in run(tree, "find", "*").stdout
    assert "outside-the-box" not in run(tree, "grep", "token").stdout
    assert "linked" not in "".join(f["path"] for f in json.loads((run(tree, "manifest") and (tree["work"] / "files.json").read_text()))["files"])
    # naming a link directly resolves it with realpath and is refused
    for args in (["tree", "linked-dir"], ["grep", "token", "--path", "linked-file"], ["grep", "token", "--path", "linked-dir"]):
        r = run(tree, *args)
        assert r.returncode == 2 and "outside the source tree" in r.stderr


def test_a_missing_source_directory_is_refused(tree: dict[str, Path]) -> None:
    r = run(tree, "tree", env={"DOCUGEN_SRC_DIR": str(tree["root"] / "nope")})
    assert r.returncode == 2 and "does not exist" in r.stderr


# ── limits ───────────────────────────────────────────────────────────────────────────────────────

def test_output_is_capped(tree: dict[str, Path]) -> None:
    for i in range(3000):
        (tree["src"] / "docs" / f"file-{i:05d}-{'x' * 40}.md").write_text("hit\n")
    r = run(tree, "find", "*.md", "--max", "100000")
    assert len(r.stdout.encode()) <= 64 * 1024 + 200 and "output truncated" in r.stdout


def test_a_pattern_that_never_finishes_is_stopped(tree: dict[str, Path]) -> None:
    (tree["src"] / "evil.txt").write_text("a" * 60 + "b\n")
    r = run(tree, "grep", "(a+)+$", env={"DOCUGEN_SRC_TIMEOUT": "2"})
    assert r.returncode == 2 and "stopped after 2 s" in r.stderr


def test_bad_patterns_and_arguments(tree: dict[str, Path]) -> None:
    assert run(tree, "grep", "(unclosed").returncode == 2
    assert "longer than" in run(tree, "grep", "a" * 500).stderr
    assert run(tree, "tree", "--depth", "-1").returncode == 2
    assert run(tree, "frobnicate").returncode == 2


def test_the_script_uses_only_the_standard_library() -> None:
    import ast
    imported = set()
    for node in ast.walk(ast.parse(SCRIPT.read_text())):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names)
