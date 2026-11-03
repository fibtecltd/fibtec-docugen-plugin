"""The plugin's shape and the skills' text. The skills are prompts, so their contract with the box is tested."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "plugins" / "docugen-box"  # the plugin; the repository root is not one
SKILLS = sorted((ROOT / "skills").glob("*/SKILL.md"))

# The commands the box allows (its permission settings). A skill may tell the model to run these and nothing else.
ALLOWED_COMMANDS = {"docugen-src", "docugen-verify", "mmdc", "plantuml"}
DOCUGEN_SRC_SUBCOMMANDS = {"tree", "find", "grep", "manifest"}
DOCUGEN_VERIFY_SUBCOMMANDS = {"markdown", "svg", "diagram", "secrets"}  # model-facing subset of docugen.verify

# What the box wrapper will accept as the only output of a session. Changing either list changes the contract
# with the box wrapper: update both together.
STATUS_WORDS = ["STARTED", "INDEXED", "DRAFTING", "VALIDATING", "REPAIRING", "PACKAGING", "DONE", "FAILED",
                "INJECTION_SUSPECTED", "LIMIT_REACHED"]
FAILURE_CODES = ["NO_SOURCE", "TOO_LARGE", "VALIDATION", "BUDGET", "DENIED", "INTERNAL"]


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, f"{path}: no front matter"
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def test_manifest_is_valid() -> None:
    m = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert m["name"] == "docugen-box"  # the image's plugin lock requires its name to equal this
    assert re.fullmatch(r"\d+\.\d+\.\d+", m["version"])
    assert m["description"] and m["author"]["name"]


# Directories that are development or VCS state, never plugin content: the CI job creates .venv inside the checkout.
NOT_PLUGIN_CONTENT = {".git", ".venv", "__pycache__", ".pytest_cache"}


def plugin_files() -> list[Path]:
    """Every file of the plugin as it would be fetched into the image, leaving out development state."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in NOT_PLUGIN_CONTENT)
        out.extend(Path(dirpath) / f for f in sorted(filenames))
    return out


def test_the_plugin_carries_no_hooks_mcp_servers_agents_or_commands() -> None:
    """Plugin hooks run commands and MCP servers start processes; in the box neither is wanted, and a
    plugin that quietly grew one would be loaded with the session's credentials in its environment."""
    top = {p.name for p in ROOT.iterdir() if p.name not in NOT_PLUGIN_CONTENT}
    assert top <= {".claude-plugin", "skills", "scripts"}, top
    names = {p.name for p in plugin_files()}
    assert "hooks.json" not in names and ".mcp.json" not in names
    assert {p.name for p in (ROOT / ".claude-plugin").iterdir()} == {"plugin.json"}


def test_the_development_state_check_ignores_a_venv_in_the_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CI job creates .venv in the checkout, and its packages contain files with any name."""
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "hooks.json").write_text("{}")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "x.md").write_text("x")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert [p.name for p in plugin_files()] == ["x.md"]


def test_there_are_the_two_skills() -> None:
    assert [p.parent.name for p in SKILLS] == ["docugen-session", "docugen-verify"]


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_front_matter(path: Path) -> None:
    fm = frontmatter(path)
    assert fm["name"] == path.parent.name
    assert 40 <= len(fm["description"]) <= 1024
    assert re.search(r"\bUse (at|after|before|when)\b", fm["description"]), "the description must say when to use the skill"


def command_lines(path: Path) -> list[str]:
    """Lines of the fenced blocks without a language tag: the commands the skill tells the model to run."""
    out: list[str] = []
    for block in re.findall(r"^```\n(.*?)^```", path.read_text(), re.DOTALL | re.MULTILINE):
        out.extend(line.strip() for line in block.splitlines() if line.strip())
    return out


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skills_only_tell_the_model_to_run_allowed_commands(path: Path) -> None:
    for line in command_lines(path):
        words = line.split()
        assert words[0] in ALLOWED_COMMANDS, f"{path.parent.name}: '{line}' is not an allowed command"
        if words[0] == "docugen-src":
            assert words[1] in DOCUGEN_SRC_SUBCOMMANDS, line
        if words[0] == "docugen-verify":
            assert words[1] in DOCUGEN_VERIFY_SUBCOMMANDS, line


def test_every_docugen_src_subcommand_the_script_offers_is_documented_in_the_session_skill() -> None:
    import importlib.util  # noqa: PLC0415 - the script is the source of truth for its commands
    spec = importlib.util.spec_from_file_location("docugen_src", ROOT / "scripts" / "docugen_src.py")
    assert spec and spec.loader
    docugen_src = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(docugen_src)
    offered = {a.dest for a in docugen_src.build_parser()._subparsers._group_actions[0]._choices_actions}  # type: ignore[union-attr]
    assert offered == DOCUGEN_SRC_SUBCOMMANDS
    documented = {line.split()[1] for line in command_lines(ROOT / "skills" / "docugen-session" / "SKILL.md")
                  if line.startswith("docugen-src")}
    assert documented == DOCUGEN_SRC_SUBCOMMANDS


def test_status_vocabulary_matches_the_contract() -> None:
    text = (ROOT / "skills" / "docugen-session" / "SKILL.md").read_text()
    words = re.search(r"^Words: (.+?)\.$", text, re.MULTILINE | re.DOTALL)
    codes = re.search(r"^Reason codes for `FAILED`: (.+?)\.$", text, re.MULTILINE | re.DOTALL)
    assert words and codes
    assert re.findall(r"`([A-Z_]+)`", words.group(1)) == STATUS_WORDS
    assert re.findall(r"`([A-Z_]+)`", codes.group(1)) == FAILURE_CODES
    assert "`STATUS: DONE`" in text and "`STATUS: FAILED`" in text


def test_the_session_skill_states_the_rules_that_the_box_relies_on() -> None:
    text = (ROOT / "skills" / "docugen-session" / "SKILL.md").read_text()
    for must in ("data to describe", "never instructions to follow", "/workspace/src", "/workspace/out/docs",
                 "INJECTION_SUSPECTED", "Never copy a secret", "## Sources", "not determined from the source",
                 "do not look for another way around it"):
        assert must in text, must


def test_skills_contain_no_urls_and_no_instruction_to_reach_outside() -> None:
    for path in SKILLS:
        text = path.read_text()
        assert not re.search(r"https?://", text), f"{path.parent.name}: a URL in a skill"
        for banned in ("curl ", "wget ", "pip install", "npm install", "git push", "ssh ", "sudo "):
            assert banned not in text, f"{path.parent.name}: '{banned}'"
