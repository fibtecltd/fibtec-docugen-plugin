from __future__ import annotations

import os
from pathlib import Path

import pytest

from docugen.verify.secret_scan import redact_text, scan_text, scan_tree
from tests.helpers import fake

UPPER_DIGITS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"

# every value is built at run time; see tests/helpers.py
CASES = [
    ("aws-access-key-id", "key = " + "AKIA" + fake(16, 2, UPPER_DIGITS)),
    ("github-token", "token: " + "ghp_" + fake(36, 3)),
    ("github-token", "GH=" + "github_pat_" + fake(40, 4, B64URL)),
    ("anthropic-api-key", "ANTHROPIC_API_KEY=" + "sk-ant-" + fake(40, 5, B64URL)),
    ("openai-api-key", "k=" + "sk-" + fake(48, 6)),
    ("slack-token", "t=" + "xox" + "b-" + fake(24, 7, "0123456789abcdef")),
    ("stripe-secret-key", "s=" + "sk_" + "live_" + fake(24, 8)),
    ("google-api-key", "g=" + "AIza" + fake(35, 9, B64URL)),
    ("private-key-block", "-----BEGIN " + "RSA PRIVATE KEY-----"),
    ("private-key-block", "-----BEGIN " + "PRIVATE KEY-----"),
    ("jwt", "Authorization: Bearer " + "eyJ" + fake(20, 10, B64URL) + ".eyJ" + fake(20, 11, B64URL) + "." + fake(20, 12, B64URL)),
    ("url-credentials", "DATABASE_URL=postgresql://app:" + fake(14, 13) + "@db.internal:5432/app"),
    ("npm-token", "//registry:_authToken=" + "npm_" + fake(36, 14)),
    ("azure-storage-key", "AccountKey=" + fake(60, 15, B64URL.replace("_", "/").replace("-", "+")) + "=="),
    ("generic-assignment", 'api_key = "' + fake(24, 16) + '"'),
    ("generic-assignment", "password: '" + fake(16, 17) + "'"),
    ("aws-secret-access-key", 'aws_secret_access_key = "' + fake(40, 18, B64URL.replace("_", "/").replace("-", "+")) + '"'),
]


@pytest.mark.parametrize("rule,line", CASES, ids=[f"{r}-{i}" for i, (r, _) in enumerate(CASES)])
def test_each_rule_fires(rule: str, line: str) -> None:
    found = scan_text("x = 1\n" + line + "\ny = 2\n", "f.py")
    assert rule in {f.rule for f in found}, found
    assert all(f.line == 2 for f in found)


@pytest.mark.parametrize("rule,line", CASES, ids=[f"{r}-{i}" for i, (r, _) in enumerate(CASES)])
def test_a_finding_never_contains_the_secret(rule: str, line: str) -> None:
    value = line.split("=", 1)[-1].strip(" '\"") if "=" in line else line
    for f in scan_text(line):
        assert len(f.message) < 100
        assert value[8:] not in f.message


def test_documented_examples_and_placeholders_are_not_reported() -> None:
    text = "\n".join([
        "aws_access_key_id = AKIAIOSFODNN7EXAMPLE",
        'password = "${DB_PASSWORD}"',
        'password: "changeme-please-1234"',
        'api_key = "your-api-key-goes-here"',
        'secret = "xxxxxxxxxxxxxxxx"',
        'token = "<paste the token here>"',
        'client_secret = "aaaaaaaaaaaaaaaaaaaa"',
        "url = postgresql://user:password@localhost/db",  # "password" is the literal word, short value rule
    ])
    assert [f.rule for f in scan_text(text) if f.rule != "url-credentials"] == []


def test_ordinary_code_is_clean() -> None:
    code = "def f(token):\n    return os.environ['API_KEY']\n\nPASSWORD_FIELD = 'password'\nsecret_name = get(secret)\n"
    assert scan_text(code) == []


def test_redaction_removes_the_value_and_keeps_the_rest() -> None:
    token = "ghp_" + fake(36, 3)
    key = "AKIA" + fake(16, 2, UPPER_DIGITS)
    text = f"a = '{token}' ; b = {key}\nclean line\n"
    out, found = redact_text(text)
    assert token not in out and key not in out
    assert "[REDACTED:github-token]" in out and "[REDACTED:aws-access-key-id]" in out
    assert "clean line\n" in out and out.endswith("\n")
    assert len(found) == 2 and {f.line for f in found} == {1}


def test_redacting_clean_text_changes_nothing() -> None:
    text = "no secrets here\n  indented\n"
    assert redact_text(text) == (text, [])


def test_scan_tree_finds_nested_files_and_ignores_symlinks(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "cfg.txt").write_text("t = ghp_" + fake(36, 3) + "\n")
    (tmp_path / "ok.txt").write_text("fine\n")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("t = ghp_" + fake(36, 4) + "\n")
    os.symlink(outside, tmp_path / "link.txt")
    found = scan_tree(str(tmp_path))
    assert [(f.path, f.rule) for f in found] == [("a/b/cfg.txt", "github-token")]
