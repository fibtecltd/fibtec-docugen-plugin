"""A whole session in the box: fetch the source, run the agent, check, document and upload the output."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import pytest

import docugen
from docugen.box import session, transfer
from docugen.box import supervise as supervise_module
from docugen.box.session import ConfigError, OutputSummary, SessionConfig, StepFailed, load_release, load_tools
from docugen.verify.archive_safe import Limits as RealLimits
from docugen.verify.manifest import MANIFEST_PATH, check_manifest, check_tree
from tests.box.test_supervise import FAKE, ROOT, event_assistant, event_result, stream_file, supervisor_env
from tests.box.test_transfer import parse_multipart, reply, serve

SESSION_ID = "0b6f3a52-5c0e-4a52-9f0e-2a1d9c7c1111"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
SECRET = "SECRETSIGNATURE0123456789"
SOURCE_FILES = {"app/api.py": "def handler():\n    return 1\n", "app/models.py": "class Session: ...\n", "README.md": "# repo\n"}
INDEX = "# Overview\n\nSee ![classes](diagrams/class.svg).\n\n## Sources\napp/api.py\napp/models.py:1\n"
DOCS = {"docs/index.md": INDEX, "docs/diagrams/class.mmd": "classDiagram\n  class Session\n",
        "docs/diagrams/class.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>"}


def tar_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, text in sorted(files.items()):
            data = text.encode()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Backend:
    """Serves the source and records what is uploaded."""

    def __init__(self, source: bytes, source_status: int = 200, upload_status: int = 204, usage_status: int = 204) -> None:
        self.source, self.source_status, self.upload_status, self.usage_status = source, source_status, upload_status, usage_status
        self.uploads: list[tuple[dict[str, str], bytes]] = []
        self.usage_uploads: list[tuple[dict[str, str], bytes]] = []       # what was posted to /usage, apart from the output
        self.usage_bodies: list[bytes] = []                                 # the same, whole, for the file's name
        self.order: list[str] = []                                          # "usage" and "docs", in the order they arrived
        self.agent_had_ended: list[bool] = []                               # at each usage post: had the agent already run and left?
        self.probe: Callable[[], bool] | None = None
        self.gets = 0

    def __call__(self, handler: BaseHTTPRequestHandler, body: bytes) -> None:
        if handler.command == "POST" and handler.path.startswith("/usage"):
            self.order.append("usage")
            if self.probe is not None:
                self.agent_had_ended.append(self.probe())
            if self.usage_status == 204:
                self.usage_bodies.append(body)
                parts = parse_multipart(handler.headers["Content-Type"], body)
                self.usage_uploads.append(({k: v.decode() for k, v in parts[:-1]}, parts[-1][1]))
            return reply(handler, self.usage_status)
        if handler.command == "POST":
            self.order.append("docs")
        if handler.command == "GET":
            self.gets += 1
            return reply(handler, self.source_status, self.source if self.source_status == 200 else b"<Error/>")
        if self.upload_status == 204:
            parts = parse_multipart(handler.headers["Content-Type"], body)
            self.uploads.append(({k: v.decode() for k, v in parts[:-1]}, parts[-1][1]))
        return reply(handler, self.upload_status)


class World:
    def __init__(self, tmp: Path, backend: Backend, base: str) -> None:
        self.src, self.out, self.work = tmp / "src", tmp / "out", tmp / "work"
        for d in (self.src, self.out):
            d.mkdir()
        self.work.mkdir(mode=0o700)
        self.tmp, self.backend, self.base = tmp, backend, base
        self.record = tmp / "agent.json"
        self.environ: dict[str, str] = {
            "PATH": os.environ["PATH"], "ANTHROPIC_API_KEY": "sk-not-real", "FAKE_OUT_DIR": str(self.out),
            "FAKE_RECORD": str(self.record), "FAKE_WRITE": json.dumps(DOCS),
            "DOCUGEN_SESSION_ID": SESSION_ID, "DOCUGEN_SOURCE_URL": f"{base}/source?sig={SECRET}",
            "DOCUGEN_OUT_URL": f"{base}/upload", "DOCUGEN_OUT_FIELDS": json.dumps({"key": f"out/t/{SESSION_ID}/docs.tar", "policy": SECRET}),
            "DOCUGEN_SOURCE_KIND": "github", "DOCUGEN_SOURCE_COMMIT": COMMIT,
        }

    def stream(self, events: list[Any], agent_exit: int = 0) -> None:
        self.environ["FAKE_STREAM"] = str(stream_file(self.tmp, events))
        self.environ["FAKE_EXIT"] = str(agent_exit)

    def run(self, *, argv: list[str] | None = None, tools: str | None = None, scratch: str | None = None,
            **extra: Any) -> tuple[int, list[str]]:
        out = io.StringIO()
        argv = argv if argv is not None else [sys.executable, str(FAKE), "-p", "go"]
        rc = session.run_session(argv, self.environ, src_dir=str(self.src), out_dir=str(self.out), work_dir=str(self.work),
                                 tools_file=tools or str(self.tmp / "no-tools.json"), out=out, now=NOW, drop_privileges=False,
                                 scratch_dir=scratch, **extra)
        return rc, out.getvalue().splitlines()


@pytest.fixture
def world(tmp_path: Path):
    """A World against a backend that serves a good source; ``make(backend)`` swaps in another."""
    contexts: list[Any] = []

    def make(backend: Backend | None = None, sub: str | None = None) -> World:
        """``sub`` names a directory of its own, for a second world in the same test (a session to compare with)."""
        backend = backend or Backend(tar_bytes(SOURCE_FILES))
        context = serve(backend)
        base, _hits = context.__enter__()
        contexts.append(context)
        home = tmp_path
        if sub is not None:
            home = tmp_path / sub
            home.mkdir()
        w = World(home, backend, base)
        w.stream([event_assistant("STATUS: DRAFTING"), event_result("STATUS: DONE")])
        return w

    yield make
    for context in contexts:
        context.__exit__(None, None, None)


def status_lines(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.startswith("STATUS: ")]


# ── a good session ──────────────────────────────────────────────────────────────────────────────────

def test_a_good_session_fetches_runs_packs_and_uploads(world) -> None:
    w = world()
    rc, lines = w.run()
    assert rc == 0
    assert lines[0] == "STATUS: DRAFTING" and lines[1] == "STATUS: PACKAGING"
    assert lines[2].startswith("OUTPUT: documents=3 bytes=") and lines[2].endswith(" unresolved_sources=0")
    assert lines[3].startswith("USAGE: ") and lines[-1] == "STATUS: DONE" and len(lines) == 5

    # the source was extracted where the agent could read it, and the work directory is clean again
    assert (w.src / "app" / "api.py").read_text() == SOURCE_FILES["app/api.py"]
    assert list(w.work.iterdir()) == []

    # exactly one upload, with the fields first and the file last
    (fields, payload), = w.backend.uploads
    assert list(fields) == ["key", "policy"] and fields["key"].endswith("/docs.tar")
    assert w.backend.gets == 1
    assert lines[2].split("bytes=")[1].split(" ")[0] == str(len(payload))

    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        names = tar.getnames()
        assert names == sorted(names) == ["docs/.docugen/manifest.json", "docs/diagrams/class.mmd", "docs/diagrams/class.svg", "docs/index.md"]
        for member in tar.getmembers():   # reproducible: nothing about who or when
            assert (member.mtime, member.uid, member.gid, member.uname, member.gname, member.mode) == (0, 0, 0, "", "", 0o644)
            assert member.isreg()
        manifest = json.loads(tar.extractfile("docs/.docugen/manifest.json").read())  # type: ignore[union-attr]
    assert manifest["source"] == {"kind": "github", "commit": COMMIT} and manifest["generated_at"] == "2026-11-03T12:00:00Z"
    index = next(d for d in manifest["documents"] if d["path"] == "docs/index.md")
    assert [s["path"] for s in index["sources"]] == ["app/api.py", "app/models.py"]

    # what was uploaded is what the validators accept, against the source the box extracted
    assert check_tree(str(w.out)) == []
    assert check_manifest(manifest, root=str(w.out), source_root=str(w.src)) == []

    # the supervisor wrote nothing in the agent's tree: not the manifest, not a temporary file
    written = {str(p.relative_to(w.out)) for p in w.out.rglob("*") if p.is_file()}
    assert written == set(DOCS) and not (w.out / MANIFEST_PATH).exists()


def test_the_agent_never_sees_the_session_secrets(world) -> None:
    w = world()
    w.run()
    names = json.loads(w.record.read_text())["env"]
    assert not [n for n in names if n.startswith("DOCUGEN_")]
    assert "ANTHROPIC_API_KEY" in names and "PATH" in names and "PYTHONPATH" not in names
    assert w.record.read_text().count(SECRET) == 0


def test_the_manifest_records_the_tools_of_the_image(world, tmp_path: Path) -> None:
    w = world()
    tools = tmp_path / "tools.json"
    tools.write_text(json.dumps({"mermaid-cli": "12.2.0", "plantuml": "1.2026.9", "bad": 3, "": "x"}))
    w.run(tools=str(tools))
    (_, payload), = w.backend.uploads
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        manifest = json.loads(tar.extractfile("docs/.docugen/manifest.json").read())  # type: ignore[union-attr]
    assert manifest["generator"]["tools"] == {"mermaid-cli": "12.2.0", "plantuml": "1.2026.9"}


def uploaded_manifest(w) -> dict:
    (_, payload), = w.backend.uploads
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        return json.loads(tar.extractfile("docs/.docugen/manifest.json").read())  # type: ignore[union-attr]


def test_the_manifest_names_the_release_of_the_image(world, tmp_path: Path) -> None:
    w = world()
    release = tmp_path / "release"
    release.write_text("1.1.0\n")
    rc, _ = w.run(release_file=str(release))
    assert rc == 0 and uploaded_manifest(w)["generator"]["version"] == "1.1.0"


def test_without_a_release_file_the_manifest_names_the_version_of_the_library(world) -> None:
    w = world()
    rc, _ = w.run()
    assert rc == 0 and uploaded_manifest(w)["generator"]["version"] == docugen.__version__


@pytest.mark.parametrize("content, expected", [
    ("1.1.0\n", "1.1.0"), ("1.1.0", "1.1.0"), ("  1.2.0-rc.1  \n\n", "1.2.0-rc.1"), ("unreleased\n", "unreleased"),
    ("1.2.0+build.5", "1.2.0+build.5"), ("a" * 64, "a" * 64), ("0", "0"),
    ("", None), ("\n", None), ("a" * 65, None), ("1.1.0\n1.2.0", None), ("1 .0", None), ("-1", None), (".1", None), ("../x", None),
    ("1.1.0 # a comment", None), ("v1/1", None), ("1.1.0\x00", None), ("\u00e91.0", None),
])
def test_load_release(tmp_path: Path, content: str, expected: str | None) -> None:
    path = tmp_path / "release"
    path.write_bytes(content.encode("utf-8"))
    assert load_release(str(path)) == (expected if expected is not None else docugen.__version__)


def test_a_missing_or_unreadable_release_file_is_the_version_of_the_library(tmp_path: Path) -> None:
    assert load_release(str(tmp_path / "missing")) == docugen.__version__
    assert load_release(str(tmp_path)) == docugen.__version__     # a directory
    huge = tmp_path / "huge"
    huge.write_bytes(b"1.1.0" + b" " * 100_000 + b"x")
    assert load_release(str(huge)) == docugen.__version__, "only the start of a file is read, and a long one is not a release"


def test_the_entry_point_takes_the_release_file_from_the_environment_and_defaults_to_the_images(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(session, "run_session", lambda argv, env, **kw: seen.update(kw) or 0)
    assert session._main(["claude", "-p", "x"], {"DOCUGEN_RELEASE_FILE": "/somewhere/release"}) == 0
    assert seen["release_file"] == "/somewhere/release"
    session._main(["claude", "-p", "x"], {})
    assert seen["release_file"] == session.DEFAULT_RELEASE_FILE == "/opt/docugen/etc/release"


def test_unresolved_citations_are_counted_and_do_not_fail_the_session(world) -> None:
    w = world()
    w.environ["FAKE_WRITE"] = json.dumps({**DOCS, "docs/more.md": "# More\n\n## Sources\napp/missing.py\napp/api.py\n../etc/passwd\n"})
    rc, lines = w.run()
    assert rc == 0 and lines[-1] == "STATUS: DONE"
    assert next(ln for ln in lines if ln.startswith("OUTPUT:")).endswith("unresolved_sources=2")


def test_a_zip_source_is_accepted_too(world) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, text in SOURCE_FILES.items():
            zf.writestr(rel, text)
    w = world(Backend(buf.getvalue()))
    rc, lines = w.run()
    assert rc == 0 and lines[-1] == "STATUS: DONE" and (w.src / "README.md").exists()


# ── the session's spend ─────────────────────────────────────────────────────────────────────────────

USAGE_SECRET = "USAGESIGNATURE9876543210"
USAGE_KEY = f"out/t/{SESSION_ID}/usage.json"


def with_usage(w: World, **change: str) -> World:
    """Give the session somewhere to say what it cost: a second presigned POST, on the same loopback backend."""
    w.environ.update({"DOCUGEN_USAGE_URL": f"{w.base}/usage", "DOCUGEN_USAGE_FIELDS": json.dumps({"key": USAGE_KEY, "policy": USAGE_SECRET}), **change})
    return w


def test_a_good_session_tells_the_service_what_it_cost_and_nothing_else_changes(world) -> None:
    plain = world(sub="plain")
    plain_rc, plain_lines = plain.run()
    w = with_usage(world())
    rc, lines = w.run()
    assert (rc, lines) == (plain_rc, plain_lines)              # not a line more, not another exit code
    (fields, payload), = w.backend.usage_uploads
    assert fields == {"key": USAGE_KEY, "policy": USAGE_SECRET} and payload == b'{"cost_usd":0.0123}'
    assert b'name="file"; filename="usage.json"' in w.backend.usage_bodies[0]
    assert len(w.backend.uploads) == 1 and w.backend.uploads[0][0]["key"].endswith("/docs.tar")   # the output went where it always goes
    assert list(w.work.iterdir()) == [] and plain.backend.usage_uploads == []                      # no file left; no variables, no upload


def test_the_spend_is_told_after_the_agent_has_ended_and_before_the_documents_are_packed(world) -> None:
    w = with_usage(world())
    w.backend.probe = lambda: w.record.exists()               # the fake agent writes its record when it runs
    w.run()
    assert w.backend.order == ["usage", "docs"] and w.backend.agent_had_ended == [True]


@pytest.mark.parametrize("events,agent_exit,words", [
    ([event_assistant("STATUS: DRAFTING"), event_result("", is_error=True, subtype="error_max_turns", terminal_reason="max_turns")], 1, "LIMIT_REACHED"),
    ([event_assistant("STATUS: INDEXED"), event_result("STATUS: FAILED NO_SOURCE")], 0, "FAILED NO_SOURCE"),
    ([event_result("STATUS: DONE", is_error=True, terminal_reason="api_error", api_error_status=429)], 1, "FAILED BUDGET"),
])
def test_a_session_that_did_not_end_well_has_still_spent_what_it_spent(world, events: list, agent_exit: int, words: str) -> None:
    w = with_usage(world())
    w.stream(events, agent_exit)
    _, lines = w.run()
    assert lines[-1] == f"STATUS: {words}" and [p for _, p in w.backend.usage_uploads] == [b'{"cost_usd":0.0123}']


def test_a_session_whose_agent_never_ended_has_no_figure_to_tell(world) -> None:
    w = with_usage(world())
    w.stream([event_assistant("STATUS: DONE")], 0)            # no final event: a crash
    _, lines = w.run()
    assert lines[-1] == "STATUS: FAILED INTERNAL" and w.backend.usage_uploads == []


@pytest.mark.parametrize("figure", [None, float("inf"), float("nan"), -0.5, True, "0.5", 1e12, [1], {"a": 1}])
def test_a_figure_that_is_not_a_plain_number_is_not_told(world, figure: object) -> None:
    w = with_usage(world())
    w.stream([event_assistant("STATUS: DRAFTING"), event_result("STATUS: DONE", total_cost_usd=figure)])
    rc, lines = w.run()
    assert rc == 0 and lines[-1] == "STATUS: DONE" and w.backend.usage_uploads == []


@pytest.mark.parametrize("figure,expected", [(0, b'{"cost_usd":0.0}'), (1, b'{"cost_usd":1.0}'), (0.123456, b'{"cost_usd":0.1235}'),
                                             (12.5, b'{"cost_usd":12.5}'), (999999999999.0, b'{"cost_usd":999999999999.0}')])
def test_the_figure_is_a_number_to_four_places(world, figure: float, expected: bytes) -> None:
    w = with_usage(world())
    w.stream([event_assistant("STATUS: DRAFTING"), event_result("STATUS: DONE", total_cost_usd=figure)])
    w.run()
    assert [p for _, p in w.backend.usage_uploads] == [expected]


@pytest.mark.parametrize("change", [
    {"DOCUGEN_USAGE_URL": "ftp://127.0.0.1/usage"},                                  # not https, not loopback http
    {"DOCUGEN_USAGE_URL": "https://example.com/usage"},                              # not an amazonaws.com name
    {"DOCUGEN_USAGE_FIELDS": "not json"},
    {"DOCUGEN_USAGE_FIELDS": "[]"},
    {"DOCUGEN_USAGE_FIELDS": "{}"},
    {"DOCUGEN_USAGE_FIELDS": json.dumps({"file": "x"})},                             # a field called file is the file
])
def test_malformed_usage_variables_upload_nothing_and_do_not_stop_the_session(world, change: dict) -> None:
    plain_rc, plain_lines = world(sub="plain").run()
    w = with_usage(world(), **change)
    rc, lines = w.run()
    assert (rc, lines) == (plain_rc, plain_lines) and w.backend.usage_uploads == [] and w.backend.order == ["docs"]


@pytest.mark.parametrize("only", ["DOCUGEN_USAGE_URL", "DOCUGEN_USAGE_FIELDS"])
def test_half_a_usage_target_uploads_nothing(world, only: str) -> None:
    w = with_usage(world())
    for name in ("DOCUGEN_USAGE_URL", "DOCUGEN_USAGE_FIELDS"):
        if name != only:
            del w.environ[name]
    rc, _ = w.run()
    assert rc == 0 and w.backend.usage_uploads == [] and w.backend.order == ["docs"]


@pytest.mark.parametrize("status", [403, 412, 500, 302])
def test_a_refused_spend_upload_changes_nothing(world, status: int) -> None:
    plain_rc, plain_lines = world(sub="plain").run()
    w = with_usage(world(Backend(tar_bytes(SOURCE_FILES), usage_status=status)))
    rc, lines = w.run()
    assert (rc, lines) == (plain_rc, plain_lines) and len(w.backend.uploads) == 1 and w.backend.usage_uploads == []


@pytest.mark.parametrize("where", ["record", "post"])
def test_an_exception_while_telling_the_spend_changes_nothing_and_prints_nothing(world, monkeypatch: pytest.MonkeyPatch,
                                                                              capsys: pytest.CaptureFixture[str], where: str) -> None:
    plain_rc, plain_lines = world(sub="plain").run()
    capsys.readouterr()
    w = with_usage(world())

    def explode(*_a: object, **_k: object) -> None:
        raise RuntimeError(f"/workspace/src/secret-customer-file.py {USAGE_SECRET}")

    real_post = transfer.post_bytes

    def post(url: str, fields: dict, payload: bytes, filename: str) -> int:
        if filename == "usage.json":
            explode()
        return real_post(url, fields, payload, filename)      # the output still goes through the same function

    if where == "record":
        monkeypatch.setattr(supervise_module, "cost_record", explode)
    else:
        monkeypatch.setattr(transfer, "post_bytes", post)
    rc, lines = w.run()
    captured = capsys.readouterr()
    assert (rc, lines) == (plain_rc, plain_lines) and captured.err == "" and captured.out == ""
    assert USAGE_SECRET not in "".join(lines) and len(w.backend.uploads) == 1 and w.backend.usage_uploads == []


def test_the_agent_never_holds_the_spend_target(world) -> None:
    w = with_usage(world())
    w.run()
    names = json.loads(w.record.read_text())["env"]
    assert not [n for n in names if n.startswith("DOCUGEN_")] and w.record.read_text().count(USAGE_SECRET) == 0


def test_the_config_reads_the_spend_target_and_forgives_its_absence_and_its_faults() -> None:
    base = {"DOCUGEN_SESSION_ID": SESSION_ID, "DOCUGEN_SOURCE_URL": "https://b.s3.eu-west-1.amazonaws.com/s?sig=1",
            "DOCUGEN_OUT_URL": "https://b.s3.eu-west-1.amazonaws.com/", "DOCUGEN_OUT_FIELDS": '{"key": "k"}',
            "DOCUGEN_SOURCE_KIND": "zip", "DOCUGEN_SOURCE_ARCHIVE_SHA256": "ab" * 32}
    assert SessionConfig.from_environ(base).usage_url is None and SessionConfig.from_environ(base).usage_fields is None
    good = {**base, "DOCUGEN_USAGE_URL": "https://b.s3.eu-west-1.amazonaws.com/", "DOCUGEN_USAGE_FIELDS": '{"key": "u"}'}
    config = SessionConfig.from_environ(good)
    assert config.usage_url == "https://b.s3.eu-west-1.amazonaws.com/" and config.usage_fields == {"key": "u"}
    assert config.out_fields == {"key": "k"}                                     # the output's own target is untouched
    for broken in ({"DOCUGEN_USAGE_FIELDS": "nope"}, {"DOCUGEN_USAGE_URL": "http://evil.example/"}):
        config = SessionConfig.from_environ({**good, **broken})
        assert (config.usage_url, config.usage_fields) == (None, None) and config.session_id == SESSION_ID


# ── how the agent ended ─────────────────────────────────────────────────────────────────────────────

def test_a_session_that_hit_its_limit_still_packs_what_it_has(world) -> None:
    w = world()
    w.stream([event_assistant("STATUS: DRAFTING"), event_result("", is_error=True, subtype="error_max_turns", terminal_reason="max_turns")], 1)
    rc, lines = w.run()
    assert rc == 3 and lines[-1] == "STATUS: LIMIT_REACHED"
    assert any(ln.startswith("OUTPUT: ") for ln in lines) and len(w.backend.uploads) == 1


@pytest.mark.parametrize("events,agent_exit,expected", [
    ([event_assistant("STATUS: INDEXED"), event_result("I could not find it.\nSTATUS: FAILED NO_SOURCE")], 0, "STATUS: FAILED NO_SOURCE"),
    ([event_assistant("STATUS: DONE")], 0, "STATUS: FAILED INTERNAL"),                                    # no result: a crash
    ([event_result("STATUS: DONE", is_error=True, terminal_reason="api_error", api_error_status=429)], 1, "STATUS: FAILED BUDGET"),
])
def test_a_session_that_did_not_end_well_uploads_nothing(world, events: list, agent_exit: int, expected: str) -> None:
    w = world()
    w.stream(events, agent_exit)
    rc, lines = w.run()
    assert rc == 1 and lines[-1] == expected
    assert w.backend.uploads == [] and "STATUS: PACKAGING" not in lines
    assert not (w.out / MANIFEST_PATH).exists()


# ── the pre-step ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,expected", [(404, "NO_SOURCE"), (403, "NO_SOURCE"), (500, "INTERNAL"), (400, "INTERNAL")])
def test_a_source_that_cannot_be_fetched_never_starts_the_agent(world, status: int, expected: str) -> None:
    w = world(Backend(b"", source_status=status))
    rc, lines = w.run()
    assert rc == 1 and lines == [f"STATUS: FAILED {expected}"]
    assert not w.record.exists() and w.backend.uploads == []


def test_a_source_nobody_serves(world) -> None:
    w = world()
    w.environ["DOCUGEN_SOURCE_URL"] = "http://127.0.0.1:9/source"
    rc, lines = w.run()
    assert (rc, lines) == (1, ["STATUS: FAILED INTERNAL"]) and not w.record.exists()


def test_a_source_over_the_cap_is_too_large(world, monkeypatch: pytest.MonkeyPatch) -> None:
    w = world()
    monkeypatch.setattr(session, "MAX_SOURCE_BYTES", 100)
    rc, lines = w.run()
    assert (rc, lines) == (1, ["STATUS: FAILED TOO_LARGE"]) and not w.record.exists()


@pytest.mark.parametrize("archive,expected", [
    (tar_bytes({"../escape.txt": "x"}), "INTERNAL"),          # a path that leaves the directory
    (tar_bytes({"/etc/passwd": "x"}), "INTERNAL"),
    (tar_bytes({}), "INTERNAL"),                               # a tar with no members is not a recognisable archive
    (b"this is not an archive at all", "INTERNAL"),
])
def test_a_source_that_is_not_acceptable_never_starts_the_agent(world, archive: bytes, expected: str) -> None:
    w = world(Backend(archive))
    rc, lines = w.run()
    assert (rc, lines) == (1, [f"STATUS: FAILED {expected}"])
    assert not w.record.exists() and w.backend.uploads == []
    assert not (w.work / "source.tar").exists()
    assert not (w.tmp / "escape.txt").exists()


def test_an_archive_with_no_files_in_it_has_nothing_to_document(world) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("app")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    w = world(Backend(buf.getvalue()))
    rc, lines = w.run()
    assert (rc, lines) == (1, ["STATUS: FAILED NO_SOURCE"]) and not w.record.exists()


def test_a_source_with_only_skipped_files_has_nothing_to_document(world) -> None:
    w = world(Backend(tar_bytes({".env": "TOKEN=x\n", "node_modules/x/index.js": "x\n", "id_rsa": "key\n"})))
    rc, lines = w.run()
    assert (rc, lines) == (1, ["STATUS: FAILED NO_SOURCE"]) and not w.record.exists()


def test_extraction_limits_are_the_ingest_limits(world, monkeypatch: pytest.MonkeyPatch) -> None:
    w = world(Backend(tar_bytes({f"f{i}.py": "x\n" for i in range(5)})))
    monkeypatch.setattr(session, "Limits", lambda: RealLimits(max_files=3))
    rc, lines = w.run()
    assert (rc, lines) == (1, ["STATUS: FAILED TOO_LARGE"])


# ── the post-step ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("write,symlinks,why", [
    ({"notes.md": "# outside docs\n"}, {}, "a file outside docs/"),
    ({"docs/run.sh": "#!/bin/sh\n"}, {}, "an extension that is not delivered"),
    ({"docs/.docugen/other.json": "{}"}, {}, "a hidden directory"),
    ({"docs/Ünïcode.md": "# x\n"}, {}, "characters outside the allowed set"),
    ({}, {"docs/link.md": "/etc/passwd"}, "a symbolic link"),
    ({"docs/a.md": "# a\n", "docs/A.md": "# A\n"}, {}, "paths that differ only by case"),
    ({"docs/empty.txt": "x"}, {}, "no document at all"),
])
def test_output_that_is_not_an_acceptable_tree_is_not_uploaded(world, write: dict, symlinks: dict, why: str) -> None:
    w = world()
    w.environ["FAKE_WRITE"] = json.dumps(write)
    w.environ["FAKE_SYMLINKS"] = json.dumps(symlinks)
    rc, lines = w.run()
    assert rc == 1 and lines[-1] == "STATUS: FAILED VALIDATION", why
    assert "STATUS: PACKAGING" in lines and w.backend.uploads == []
    assert list(w.work.iterdir()) == []


def test_a_session_that_wrote_nothing_has_nothing_to_deliver(world) -> None:
    w = world()
    w.environ["FAKE_WRITE"] = "{}"
    rc, lines = w.run()
    assert rc == 1 and lines[-1] == "STATUS: FAILED VALIDATION" and w.backend.uploads == []


def test_output_over_the_cap_is_too_large_and_is_not_uploaded(world, monkeypatch: pytest.MonkeyPatch) -> None:
    w = world()
    monkeypatch.setattr(session, "MAX_OUTPUT_BYTES", 2000)
    rc, lines = w.run()
    assert rc == 1 and lines[-1] == "STATUS: FAILED TOO_LARGE" and w.backend.uploads == []
    assert list(w.work.iterdir()) == []


@pytest.mark.parametrize("status", [403, 412, 500, 302])
def test_an_upload_that_is_refused_fails_the_session(world, status: int) -> None:
    w = world(Backend(tar_bytes(SOURCE_FILES), upload_status=status))
    rc, lines = w.run()
    assert rc == 1 and lines[-1] == "STATUS: FAILED INTERNAL"
    assert list(w.work.iterdir()) == []   # the archive is removed even when the upload failed


def test_a_manifest_that_does_not_match_the_trees_is_never_uploaded(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """The builder is trusted, and its output is checked anyway, against the documents and the source on disk."""
    real = session.build_manifest

    def wrong(*args: Any, **kwargs: Any) -> Any:
        manifest, unresolved = real(*args, **kwargs)
        manifest["documents"][0]["sha256"] = "0" * 64
        return manifest, unresolved

    w = world()
    monkeypatch.setattr(session, "build_manifest", wrong)
    rc, lines = w.run()
    assert rc == 1 and lines[-1] == "STATUS: FAILED INTERNAL" and w.backend.uploads == []
    assert list(w.work.iterdir()) == []


def test_an_agent_that_leaves_a_forged_manifest_gets_it_replaced(world) -> None:
    w = world()
    w.environ["FAKE_WRITE"] = json.dumps({**DOCS, MANIFEST_PATH: '{"schema": 1, "forged": true}'})
    rc, _ = w.run()
    assert rc == 0
    (_, payload), = w.backend.uploads
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        names = tar.getnames()
        assert names.count(MANIFEST_PATH) == 1                            # not the agent's and ours side by side
        assert b"forged" not in tar.extractfile(MANIFEST_PATH).read()  # type: ignore[union-attr]


# ── started wrongly ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("change", [
    {"DOCUGEN_SESSION_ID": ""}, {"DOCUGEN_SESSION_ID": "not-a-uuid"}, {"DOCUGEN_SESSION_ID": SESSION_ID.upper()},
    {"DOCUGEN_SOURCE_URL": ""}, {"DOCUGEN_SOURCE_URL": "https://example.com/x"}, {"DOCUGEN_OUT_URL": "http://example.com/x"},
    {"DOCUGEN_OUT_FIELDS": "not json"}, {"DOCUGEN_OUT_FIELDS": "{}"},
    {"DOCUGEN_SOURCE_KIND": ""}, {"DOCUGEN_SOURCE_KIND": "svn"}, {"DOCUGEN_SOURCE_COMMIT": "abc"},
])
def test_a_session_started_wrongly_does_nothing_and_says_so_without_a_value(world, capsys: pytest.CaptureFixture[str], change: dict) -> None:
    w = world()
    w.environ.update(change)
    rc, lines = w.run()
    assert rc == 2 and lines == []
    err = capsys.readouterr().err
    assert err.startswith("docugen-box: ") and err.count("\n") == 1
    assert SECRET not in err and COMMIT not in err and "example.com" not in err
    assert w.backend.gets == 0 and not w.record.exists()


@pytest.mark.parametrize("argv", [[], [sys.executable, str(FAKE)], [sys.executable, str(FAKE), "--version"]])
def test_a_session_without_a_prompt_fetches_nothing(world, capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    w = world()
    rc, lines = w.run(argv=argv)
    assert rc == 2 and lines == [] and w.backend.gets == 0
    assert capsys.readouterr().err == "docugen-box: the agent must be started headless (-p PROMPT)\n"


def test_an_agent_that_cannot_start_ends_the_session_wrongly_started(world, capsys: pytest.CaptureFixture[str]) -> None:
    w = world()
    rc, lines = w.run(argv=["/nonexistent/claude", "-p", "go"])
    assert (rc, lines) == (2, []) and capsys.readouterr().err == "docugen-box: cannot start the agent\n"


def test_the_session_id_is_a_uuid_and_the_config_keeps_nothing_else() -> None:
    config = SessionConfig.from_environ({
        "DOCUGEN_SESSION_ID": SESSION_ID, "DOCUGEN_SOURCE_URL": "https://b.s3.eu-west-1.amazonaws.com/s?sig=1",
        "DOCUGEN_OUT_URL": "https://b.s3.eu-west-1.amazonaws.com/", "DOCUGEN_OUT_FIELDS": '{"key": "k"}',
        "DOCUGEN_SOURCE_KIND": "zip", "DOCUGEN_SOURCE_ARCHIVE_SHA256": "ab" * 32})
    assert config.session_id == SESSION_ID and config.out_fields == {"key": "k"} and config.source.kind == "zip"
    with pytest.raises(ConfigError):
        SessionConfig.from_environ({})


# ── nothing unexpected is ever printed ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("where", ["fetch", "extract", "build_manifest", "package", "upload"])
def test_an_unexpected_exception_is_a_failed_session_and_prints_nothing_of_itself(world, monkeypatch: pytest.MonkeyPatch,
                                                                                  capsys: pytest.CaptureFixture[str], where: str) -> None:
    w = world()
    boom = RuntimeError(f"/workspace/src/secret-customer-file.py {SECRET}")

    def explode(*_a: object, **_k: object) -> None:
        raise boom

    target = {"fetch": (transfer, "fetch"), "extract": (session, "safe_extract"), "build_manifest": (session, "build_manifest"),
              "package": (session, "package"), "upload": (transfer, "upload")}[where]
    monkeypatch.setattr(*target, explode)
    rc, lines = w.run()
    assert rc == 1 and lines[-1] == "STATUS: FAILED INTERNAL"
    captured = capsys.readouterr()
    assert captured.err == "" and SECRET not in "".join(lines) and "secret-customer-file" not in "".join(lines)


@pytest.mark.skipif(os.geteuid() == 0, reason="as root the agent is switched to another user: see the root-only test below")
def test_the_command_line_entry_point(world, tmp_path: Path) -> None:
    w = world()
    env = {**supervisor_env(w.environ), "DOCUGEN_SRC_DIR": str(w.src), "DOCUGEN_OUT_DIR": str(w.out),
           "DOCUGEN_WORK_DIR": str(w.work), "DOCUGEN_TOOLS_FILE": str(tmp_path / "none.json")}
    done = subprocess.run([sys.executable, "-P", "-m", "docugen.box", "--session", "--", sys.executable, str(FAKE), "-p", "go"],
                          env=env, capture_output=True, text=True, cwd=ROOT, timeout=120)
    lines = done.stdout.splitlines()
    assert done.returncode == 0 and done.stderr == ""
    assert lines[-1] == "STATUS: DONE" and "STATUS: PACKAGING" in lines and len(w.backend.uploads) == 1


def test_the_command_line_entry_point_never_prints_a_traceback(world, tmp_path: Path) -> None:
    w = world()
    env = {**supervisor_env(w.environ), "DOCUGEN_SRC_DIR": "/nonexistent/src", "DOCUGEN_OUT_DIR": str(w.out),
           "DOCUGEN_WORK_DIR": "/nonexistent/work"}
    done = subprocess.run([sys.executable, "-P", "-m", "docugen.box", "--session", "--", sys.executable, str(FAKE), "-p", "go"],
                          env=env, capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert done.stdout == "STATUS: FAILED INTERNAL\n" and done.stderr == "" and done.returncode == 1


def test_load_tools(tmp_path: Path) -> None:
    path = tmp_path / "tools.json"
    for content, expected in [('{"a": "1", "b": "2"}', {"a": "1", "b": "2"}), ('{"a": 1}', {}), ("[]", {}), ("not json", {}), ("", {}),
                              (json.dumps({f"k{i}": "v" for i in range(51)}), {}), ('{"a": "' + "x" * 65 + '"}', {})]:
        path.write_text(content)
        assert load_tools(str(path)) == expected
    assert load_tools(str(tmp_path / "missing.json")) == {}


def test_the_output_line_is_numbers_only() -> None:
    assert OutputSummary(3, 1024, 2).line() == "OUTPUT: documents=3 bytes=1024 unresolved_sources=2"
    assert StepFailed("VALIDATION").code == "VALIDATION"
    with pytest.raises(AssertionError):
        StepFailed("SOMETHING_ELSE")


# ── who the agent is ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("environ,euid,expected", [
    ({}, 1000, None), ({}, 1, None), ({"DOCUGEN_AGENT_UID": "0"}, 1000, None),           # not root: nobody to switch to
    ({}, 0, session.AgentIdentity(1001, 1001)),
    ({"DOCUGEN_AGENT_UID": "2000", "DOCUGEN_AGENT_GID": "3000"}, 0, session.AgentIdentity(2000, 3000)),
    ({"DOCUGEN_AGENT_UID": "65535", "DOCUGEN_AGENT_GID": "1"}, 0, session.AgentIdentity(65535, 1)),
    ({"DOCUGEN_AGENT_HOME": "/opt/box-test/home"}, 0, session.AgentIdentity(1001, 1001, "/opt/box-test/home")),
    ({"DOCUGEN_AGENT_HOME": "/"}, 0, session.AgentIdentity(1001, 1001, "/")),
])
def test_the_agent_is_switched_to_its_own_user_only_when_this_process_is_root(environ: dict, euid: int, expected: object) -> None:
    assert session.agent_identity(environ, euid) == expected


@pytest.mark.parametrize("bad", ["0", "00", "-1", "+5", "abc", "", " 5", "5 ", "65536", "1e3", "0x10", "\uff11\uff12\uff13", "١٢٣"])
@pytest.mark.parametrize("name", ["DOCUGEN_AGENT_UID", "DOCUGEN_AGENT_GID"])
def test_the_agent_never_runs_as_root_and_never_as_a_malformed_id(name: str, bad: str) -> None:
    with pytest.raises(ConfigError) as exc:
        session.agent_identity({name: bad}, 0)
    assert name in str(exc.value) and (bad == "" or bad not in str(exc.value))   # the message never repeats the value


@pytest.mark.parametrize("bad", ["", "home/claude", "~/claude", "/home/../root", "/home/c laude", "/home/claude\n", "/h\x00ome",
                                 "/home/cläude", "/" + "a" * 300, "$HOME", "/home/$USER"])
def test_the_agent_home_is_a_plain_absolute_path(bad: str) -> None:
    with pytest.raises(ConfigError) as exc:
        session.agent_identity({"DOCUGEN_AGENT_HOME": bad}, 0)
    assert str(exc.value) == "DOCUGEN_AGENT_HOME must be a plain absolute path"


def test_a_session_run_as_root_gives_the_agent_the_configured_home(world, monkeypatch: pytest.MonkeyPatch) -> None:
    w = world()
    seen: dict[str, Any] = {}
    monkeypatch.setattr(session.os, "geteuid", lambda: 0)
    monkeypatch.setattr(session, "prepare_scratch", lambda path: None)
    monkeypatch.setattr(session, "pre_step", lambda config, src, scratch: None)

    def execute(argv: list[str], *, out: Any, env: dict[str, str], user: Any) -> tuple[Any, None]:
        seen.update(env)
        return session.Verdict("FAILED", "INTERNAL", 1), None

    monkeypatch.setattr(session.supervise, "execute", execute)
    w.environ.update(DOCUGEN_AGENT_HOME="/opt/somewhere/home", HOME="/root")
    session.run_session([sys.executable, str(FAKE), "-p", "go"], w.environ, src_dir=str(w.src), out_dir=str(w.out),
                        work_dir=str(w.work), out=io.StringIO())
    assert seen["HOME"] == "/opt/somewhere/home" and seen["USER"] == "claude"


def test_a_root_session_switches_the_agent_and_keeps_its_own_files_apart(world, monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path) -> None:
    """The decisions, without needing to be root: who the agent is, what it is given, where this process keeps its own files."""
    w = world()
    calls: dict[str, Any] = {}
    monkeypatch.setattr(session.os, "geteuid", lambda: 0)
    monkeypatch.setattr(session, "prepare_scratch", lambda path: calls.setdefault("scratch", path))
    monkeypatch.setattr(session, "pre_step", lambda config, src, scratch: calls.setdefault("pre_step_scratch", scratch))
    monkeypatch.setattr(session, "make_read_only", lambda path: calls.setdefault("read_only", path))

    def execute(argv: list[str], *, out: Any, env: dict[str, str], user: Any) -> tuple[Any, None]:
        calls.update(env=env, user=user)
        return session.Verdict("FAILED", "INTERNAL", 1), None

    monkeypatch.setattr(session.supervise, "execute", execute)
    w.environ.update(HOME="/root", USER="root")
    out = io.StringIO()
    rc = session.run_session([sys.executable, str(FAKE), "-p", "go"], w.environ, src_dir=str(w.src), out_dir=str(w.out),
                             work_dir=str(w.work), out=out)               # drop_privileges left at its default
    assert rc == 1
    assert calls["user"] == (1001, 1001)
    assert calls["scratch"] == calls["pre_step_scratch"] == session.DEFAULT_SCRATCH != str(w.work)   # not where the agent writes
    assert (calls["env"]["HOME"], calls["env"]["USER"], calls["env"]["LOGNAME"]) == ("/home/claude", "claude", "claude")
    assert not [k for k in calls["env"] if k.startswith("DOCUGEN_")] and calls["env"]["ANTHROPIC_API_KEY"] == "sk-not-real"
    assert "read_only" not in calls                                     # root-owned already: nothing to chmod


def test_a_root_session_for_a_misconfigured_agent_id_does_not_start(world, monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    w = world()
    monkeypatch.setattr(session.os, "geteuid", lambda: 0)
    w.environ["DOCUGEN_AGENT_UID"] = "0"
    out = io.StringIO()
    rc = session.run_session([sys.executable, str(FAKE), "-p", "go"], w.environ, src_dir=str(w.src), out_dir=str(w.out),
                             work_dir=str(w.work), out=out)
    assert rc == 2 and out.getvalue() == "" and w.backend.gets == 0
    assert capsys.readouterr().err == "docugen-box: DOCUGEN_AGENT_UID must be a user or group id from 1 to 65535: the agent never runs as root\n"


def test_the_agent_is_started_as_the_user_with_no_supplementary_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def popen(*args: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        raise PermissionError

    monkeypatch.setattr(supervise_module.subprocess, "Popen", popen)
    with pytest.raises(supervise_module.UsageError) as exc:
        supervise_module.execute(["claude", "-p", "go"], out=io.StringIO(), env={}, user=(1001, 1002))
    assert (seen["user"], seen["group"], seen["extra_groups"]) == (1001, 1002, [])
    assert str(exc.value) == "cannot start the agent: permission denied (switching to the agent user?)"
    seen.clear()
    with pytest.raises(supervise_module.UsageError):
        supervise_module.execute(["claude", "-p", "go"], out=io.StringIO(), env={})
    assert "user" not in seen and "group" not in seen and "extra_groups" not in seen


def test_a_refused_switch_is_reported_as_started_wrongly_with_a_fixed_message(world, monkeypatch: pytest.MonkeyPatch,
                                                                              capsys: pytest.CaptureFixture[str]) -> None:
    w = world()

    def popen(*_a: Any, **_k: Any) -> None:
        raise PermissionError("/secret/path/of/the/customer")

    monkeypatch.setattr(supervise_module.subprocess, "Popen", popen)
    rc, lines = w.run()
    assert (rc, lines) == (2, [])
    assert capsys.readouterr().err == "docugen-box: cannot start the agent: permission denied (switching to the agent user?)\n"


# ── what this process keeps for itself ──────────────────────────────────────────────────────────────

def test_the_scratch_directory_is_created_private_when_missing(world, tmp_path: Path) -> None:
    w = world()
    scratch = tmp_path / "new" / "scratch"
    rc, _ = w.run(scratch=str(scratch))
    assert rc == 0 and (scratch.stat().st_mode & 0o777) == 0o700 and list(scratch.iterdir()) == []
    assert list(w.work.iterdir()) == []      # the agent's own work directory was not used for it


@pytest.mark.parametrize("make", ["symlink", "loose-mode", "a-file"])
def test_a_scratch_directory_that_is_not_private_ends_the_session_before_anything_is_fetched(world, tmp_path: Path, make: str) -> None:
    w = world()
    target = tmp_path / "scratch"
    if make == "symlink":
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        target.symlink_to(real)
    elif make == "loose-mode":
        target.mkdir()
        target.chmod(0o755)
    else:
        target.write_text("not a directory")
    rc, lines = w.run(scratch=str(target))
    assert (rc, lines) == (1, ["STATUS: FAILED INTERNAL"]) and w.backend.gets == 0 and not w.record.exists()


def test_the_supervisor_follows_no_link_the_agent_leaves_where_it_would_write(world, tmp_path: Path) -> None:
    """A root process that wrote the manifest in the agent's tree would follow these. This one writes nothing there."""
    w = world()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    w.environ["FAKE_SYMLINKS"] = json.dumps({"docs/.docugen/manifest.json": str(victim), "docs/.docugen/manifest.json.tmp": str(victim)})
    rc, lines = w.run()
    assert rc == 1 and lines[-1] == "STATUS: FAILED VALIDATION" and w.backend.uploads == []
    assert victim.read_text() == "precious"


def test_not_being_root_makes_the_source_read_only_by_mode(world) -> None:
    w = world()
    rc, _ = w.run()
    assert rc == 0
    for path in [w.src, *w.src.rglob("*")]:
        assert not path.lstat().st_mode & 0o222, path
    assert (w.src / "app" / "api.py").read_text() == SOURCE_FILES["app/api.py"]   # still readable


# ── the real thing: root, and an agent that is somebody else ────────────────────────────────────────

@pytest.mark.skipif(os.geteuid() != 0, reason="starting the agent as another user needs root")
def test_as_root_the_agent_really_runs_as_another_user_and_cannot_reach_what_is_not_its_own() -> None:
    import shutil
    import tempfile
    uid, gid = 31337, 31338
    base = Path(tempfile.mkdtemp(prefix="docugen-root-"))
    try:
        base.chmod(0o755)
        for name in ("src", "scratch"):
            (base / name).mkdir(mode=0o700 if name == "scratch" else 0o755)
        for name in ("out", "work", "rec"):
            (base / name).mkdir(mode=0o755)
            os.chown(base / name, uid, gid)
        (base / "work").chmod(0o700)
        fake = base / "fake_agent.py"
        shutil.copy(FAKE, fake)
        fake.chmod(0o644)
        stream = stream_file(base, [event_assistant("STATUS: DRAFTING"), event_result("STATUS: DONE")])
        stream.chmod(0o644)

        with serve(Backend(tar_bytes(SOURCE_FILES))) as (url, hits):
            environ = {
                "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root", "PYTHONPATH": str(ROOT), "ANTHROPIC_API_KEY": "sk-not-real",
                "DOCUGEN_SESSION_ID": SESSION_ID, "DOCUGEN_SOURCE_URL": f"{url}/source?sig={SECRET}", "DOCUGEN_OUT_URL": f"{url}/upload",
                "DOCUGEN_OUT_FIELDS": json.dumps({"key": "k", "policy": SECRET}), "DOCUGEN_SOURCE_KIND": "github",
                "DOCUGEN_SOURCE_COMMIT": COMMIT, "DOCUGEN_AGENT_UID": str(uid), "DOCUGEN_AGENT_GID": str(gid),
                "DOCUGEN_SRC_DIR": str(base / "src"), "DOCUGEN_OUT_DIR": str(base / "out"), "DOCUGEN_WORK_DIR": str(base / "work"),
                "DOCUGEN_SCRATCH_DIR": str(base / "scratch"), "DOCUGEN_TOOLS_FILE": str(base / "none.json"),
                "FAKE_STREAM": str(stream), "FAKE_OUT_DIR": str(base / "out"), "FAKE_WRITE": json.dumps(DOCS),
                "FAKE_RECORD": str(base / "rec" / "agent.json"),
                "FAKE_PROBE": json.dumps({"src": str(base / "src"), "scratch": str(base / "scratch")}),
            }
            done = subprocess.run([sys.executable, "-P", "-m", "docugen.box", "--session", "--", "/usr/bin/python3", str(fake), "-p", "go"],
                                  env=environ, capture_output=True, text=True, cwd=str(base / "out"), timeout=120)
            uploads = hits.requests

        lines = done.stdout.splitlines()
        assert done.returncode == 0 and done.stderr == "", (done.stdout, done.stderr)
        assert lines[0] == "STATUS: DRAFTING" and lines[1] == "STATUS: PACKAGING" and lines[-1] == "STATUS: DONE"
        assert [r[0] for r in uploads] == ["GET", "POST"]

        record = json.loads((base / "rec" / "agent.json").read_text())
        assert (record["uid"], record["gid"], record["groups"]) == (uid, gid, [])
        assert record["home"] == "/home/claude" and not [n for n in record["env"] if n.startswith("DOCUGEN_")]
        assert record["probe"] == {"parent_environ": "denied", "write_src": "denied", "list_scratch": "denied"}

        # ownership says the same: the source is root's, the output is the agent's, and nothing in it is the supervisor's
        assert (base / "src" / "app" / "api.py").stat().st_uid == 0
        owners = {p.stat().st_uid for p in (base / "out").rglob("*")}
        assert owners == {uid}, owners
        assert list((base / "scratch").iterdir()) == []
    finally:
        shutil.rmtree(base, ignore_errors=True)
