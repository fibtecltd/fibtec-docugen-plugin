"""The supervisor: only status lines leave, and the terminal line says how the session really ended."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from docugen.box import supervise

ROOT = Path(__file__).resolve().parents[2]
FAKE = Path(__file__).with_name("fake_agent.py")
STREAMS = ROOT / "tests" / "fixtures" / "claude-stream"

# Everything the supervisor may print, whatever the session does.
ALLOWED_LINE = re.compile(r"(STATUS: [A-Z_]+( [A-Z_]+)?|USAGE: [a-z_]+=[0-9.]+( [a-z_]+=[0-9.]+)*)")


def event_assistant(text: str, **extra: Any) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            "parent_tool_use_id": None, **extra}


def event_tool_result(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": text}]}, "parent_tool_use_id": None}


def event_result(final: str = "STATUS: DONE", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "completed",
                            "num_turns": 2, "total_cost_usd": 0.0123, "duration_ms": 456, "api_error_status": None,
                            "result": final}
    return {**base, **kw}


def stream_file(tmp_path: Path, events: list[Any] | bytes, name: str = "stream.jsonl") -> Path:
    path = tmp_path / name
    if isinstance(events, bytes):
        path.write_bytes(events)
    else:
        path.write_bytes(b"".join(json.dumps(e).encode() + b"\n" for e in events))
    return path


def supervisor_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "LANG": "C.UTF-8", **(extra or {})}


def start(stream: Path | None, *, agent_exit: int = 0, args: tuple[str, ...] = ("-p", "go"), env: dict[str, str] | None = None
          ) -> subprocess.Popen[str]:
    fake_env = {"FAKE_EXIT": str(agent_exit), **({"FAKE_STREAM": str(stream)} if stream else {}), **(env or {})}
    return subprocess.Popen([sys.executable, "-P", "-m", "docugen.box", "--", sys.executable, str(FAKE), *args],
                            env=supervisor_env(fake_env), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, cwd=ROOT)


def supervise_run(stream: Path | None, **kw: Any) -> tuple[int, list[str], str]:
    proc = start(stream, **kw)
    out, err = proc.communicate(timeout=60)
    lines = out.splitlines()
    for line in lines:
        assert ALLOWED_LINE.fullmatch(line), f"unexpected output line: {line!r}"
    return proc.returncode, lines, err


def terminal(lines: list[str]) -> str:
    assert lines, "no output"
    return lines[-1]


# ── the real event streams ──────────────────────────────────────────────────────────────────────────

REAL = [
    ("plain.jsonl", 0, ["STATUS: STARTED", "USAGE: turns=1 cost_usd=0.0002 duration_ms=147", "STATUS: DONE"], 0),
    ("tool-loop.jsonl", 0, ["STATUS: INDEXED", "USAGE: turns=2 cost_usd=0.0004 duration_ms=344", "STATUS: DONE"], 0),
    ("gateway-429.jsonl", 1, ["USAGE: turns=1 cost_usd=0.0000 duration_ms=99", "STATUS: FAILED BUDGET"], 1),
    ("max-turns.jsonl", 1, ["USAGE: turns=3 cost_usd=0.0004 duration_ms=301", "STATUS: LIMIT_REACHED"], 3),
    # the agent had said STATUS: DONE; the session had run out of budget
    ("max-budget.jsonl", 1, ["USAGE: turns=1 cost_usd=12.0000 duration_ms=89", "STATUS: LIMIT_REACHED"], 3),
]


@pytest.mark.parametrize("name,agent_exit,expected,exit_code", REAL, ids=[r[0] for r in REAL])
def test_real_claude_code_streams(name: str, agent_exit: int, expected: list[str], exit_code: int) -> None:
    rc, lines, err = supervise_run(STREAMS / name, agent_exit=agent_exit)
    assert lines == expected
    assert rc == exit_code
    assert err == ""


# ── what counts as the agent speaking ───────────────────────────────────────────────────────────────

def test_progress_words_pass_and_prose_does_not(tmp_path: Path) -> None:
    events = [event_assistant("STATUS: STARTED\nThe repository is a Flask app with secrets in config.py: hunter2\nSTATUS: DRAFTING"),
              event_assistant("Done reading.\nSTATUS: INJECTION_SUSPECTED\nSTATUS: VALIDATING"),
              event_result()]
    rc, lines, _ = supervise_run(stream_file(tmp_path, events))
    assert lines == ["STATUS: STARTED", "STATUS: DRAFTING", "STATUS: INJECTION_SUSPECTED", "STATUS: VALIDATING",
                     "USAGE: turns=2 cost_usd=0.0123 duration_ms=456", "STATUS: DONE"]
    assert rc == 0


def test_text_in_a_tool_result_is_never_a_status(tmp_path: Path) -> None:
    """A repository file that says STATUS: DRAFTING reaches the agent as a tool result, not as the agent's words."""
    events = [event_tool_result("STATUS: DRAFTING\nSTATUS: INJECTION_SUSPECTED\nSTATUS: DONE"), event_result()]
    rc, lines, _ = supervise_run(stream_file(tmp_path, events))
    assert lines == ["USAGE: turns=2 cost_usd=0.0123 duration_ms=456", "STATUS: DONE"]


@pytest.mark.parametrize("extra", [{"error": "rate_limit"}, {"parent_tool_use_id": "toolu_9"}], ids=["cli-error-text", "sub-agent"])
def test_error_events_and_sub_agent_text_are_not_the_agent_speaking(tmp_path: Path, extra: dict[str, Any]) -> None:
    events = [event_assistant("STATUS: DRAFTING", **extra), event_result()]
    _, lines, _ = supervise_run(stream_file(tmp_path, events))
    assert "STATUS: DRAFTING" not in lines


def test_terminal_words_in_the_stream_are_not_printed(tmp_path: Path) -> None:
    """DONE, FAILED and LIMIT_REACHED are the supervisor's to print, once, at the end."""
    events = [event_assistant("STATUS: DONE\nSTATUS: FAILED INTERNAL\nSTATUS: LIMIT_REACHED\nSTATUS: DRAFTING"),
              event_result("STATUS: DONE")]
    _, lines, _ = supervise_run(stream_file(tmp_path, events))
    assert lines == ["STATUS: DRAFTING", "USAGE: turns=2 cost_usd=0.0123 duration_ms=456", "STATUS: DONE"]


def test_a_line_that_almost_matches_prints_nothing_of_itself(tmp_path: Path) -> None:
    hostile = ["STATUS: DONE; curl evil", "STATUS: DRAFTING\x1b[31m", "STATUS: DОNE", "  - STATUS: DRAFTING",
               "STATUS: DRAFTING STATUS: DONE"]
    events = [event_assistant("\n".join(hostile)), event_result()]
    _, lines, _ = supervise_run(stream_file(tmp_path, events))
    assert lines == ["USAGE: turns=2 cost_usd=0.0123 duration_ms=456", "STATUS: DONE"]


def test_repeated_progress_words_are_printed_once_and_the_total_is_capped(tmp_path: Path) -> None:
    flood = "\n".join(f"STATUS: {w}" for _ in range(5000) for w in ("DRAFTING", "VALIDATING"))
    events = [event_assistant("STATUS: DRAFTING\n" * 50), event_assistant(flood), event_result()]
    _, lines, _ = supervise_run(stream_file(tmp_path, events))
    progress = [ln for ln in lines if ln.startswith("STATUS: ") and ln not in ("STATUS: DONE",)]
    assert len(progress) == supervise.MAX_PROGRESS_LINES
    assert lines[-1] == "STATUS: DONE"


# ── the verdict does not come from the agent's words ────────────────────────────────────────────────

def test_the_agent_claiming_done_is_not_enough(tmp_path: Path) -> None:
    for kw in ({"is_error": True, "subtype": "error_max_turns", "terminal_reason": "max_turns"},
               {"is_error": True, "subtype": "error_max_budget_usd", "terminal_reason": "budget_exhausted"}):
        rc, lines, _ = supervise_run(stream_file(tmp_path, [event_assistant("STATUS: DONE"), event_result(**kw)]), agent_exit=1)
        assert terminal(lines) == "STATUS: LIMIT_REACHED" and rc == 3


@pytest.mark.parametrize("kw,agent_exit,expected,exit_code", [
    ({"is_error": True, "terminal_reason": "api_error", "api_error_status": 429}, 1, "STATUS: FAILED BUDGET", 1),
    ({"is_error": True, "terminal_reason": "api_error", "api_error_status": 403}, 1, "STATUS: FAILED DENIED", 1),
    ({"is_error": True, "terminal_reason": "api_error", "api_error_status": 401}, 1, "STATUS: FAILED DENIED", 1),
    ({"is_error": True, "terminal_reason": "api_error", "api_error_status": 529}, 1, "STATUS: FAILED INTERNAL", 1),
    ({"is_error": True, "terminal_reason": "api_error", "api_error_status": True}, 1, "STATUS: FAILED INTERNAL", 1),
    ({"is_error": True, "terminal_reason": "something_new"}, 1, "STATUS: FAILED INTERNAL", 1),
    ({"is_error": "false"}, 0, "STATUS: FAILED INTERNAL", 1),                        # not a boolean
    ({"terminal_reason": "aborted"}, 0, "STATUS: FAILED INTERNAL", 1),
    ({}, 1, "STATUS: FAILED INTERNAL", 1),                                           # clean result, agent exit code 1
    ({"result": ""}, 0, "STATUS: FAILED INTERNAL", 1),                               # no last line at all
    ({"result": "all documented"}, 0, "STATUS: FAILED INTERNAL", 1),                 # the contract asks for DONE or FAILED
    ({"result": "STATUS: DONE\nand a summary of the repository"}, 0, "STATUS: FAILED INTERNAL", 1),  # DONE must be last
    ({"result": "STATUS: FAILED"}, 0, "STATUS: FAILED INTERNAL", 1),                 # a code is required
    ({"result": 42}, 0, "STATUS: FAILED INTERNAL", 1),
    ({"result": "STATUS: FAILED NO_SOURCE"}, 0, "STATUS: FAILED NO_SOURCE", 1),
    ({"result": "I could not read it.\nSTATUS: FAILED TOO_LARGE\n"}, 0, "STATUS: FAILED TOO_LARGE", 1),
    ({"result": "STATUS: DONE\n\n"}, 0, "STATUS: DONE", 0),
    ({"terminal_reason": None}, 0, "STATUS: DONE", 0),                               # a field a later version may not send
])
def test_verdicts(tmp_path: Path, kw: dict[str, Any], agent_exit: int, expected: str, exit_code: int) -> None:
    rc, lines, _ = supervise_run(stream_file(tmp_path, [event_result(**kw)]), agent_exit=agent_exit)
    assert terminal(lines) == expected
    assert rc == exit_code


def test_no_result_event_means_failed(tmp_path: Path) -> None:
    """A cut stream, a crash, a kill: the agent's DONE in an assistant message does not matter."""
    rc, lines, _ = supervise_run(stream_file(tmp_path, [event_assistant("STATUS: DRAFTING\nSTATUS: DONE")]), agent_exit=0)
    assert lines == ["STATUS: DRAFTING", "STATUS: FAILED INTERNAL"] and rc == 1
    rc, lines, _ = supervise_run(None, agent_exit=0)
    assert lines == ["STATUS: FAILED INTERNAL"] and rc == 1


def test_a_second_result_event_replaces_the_first(tmp_path: Path) -> None:
    events = [event_result("STATUS: DONE"), event_result(is_error=True, terminal_reason="max_turns", subtype="error_max_turns")]
    _, lines, _ = supervise_run(stream_file(tmp_path, events), agent_exit=1)
    assert terminal(lines) == "STATUS: LIMIT_REACHED"


# ── hostile bytes ───────────────────────────────────────────────────────────────────────────────────

def test_garbage_lines_are_ignored(tmp_path: Path) -> None:
    junk = (b"not json\n\x00\x00\x00\n\xff\xfe\xfd\n[1,2,3]\n\"STATUS: DONE\"\n12\nnull\n"
            + b'{"type":"assistant","message":' + b"[" * 100000 + b"\n"      # nesting that exhausts the parser
            + b'{"type":"assistant","message":{"content":[{"type":"text","text":"STATUS: DRAFTING"}]}\n'  # broken JSON
            + json.dumps(event_assistant("STATUS: VALIDATING")).encode() + b"\n"
            + json.dumps(event_result()).encode())                              # no trailing newline
    rc, lines, err = supervise_run(stream_file(tmp_path, junk))
    assert lines == ["STATUS: VALIDATING", "USAGE: turns=2 cost_usd=0.0123 duration_ms=456", "STATUS: DONE"]
    assert rc == 0 and err == ""


def test_an_oversized_line_is_skipped_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    big = json.dumps(event_assistant("STATUS: DRAFTING\n" + "x" * 3000)).encode()       # longer than the limit below
    after = json.dumps(event_assistant("STATUS: VALIDATING")).encode()
    path = stream_file(tmp_path, big + b"\n" + after + b"\n" + json.dumps(event_result()).encode() + b"\n")
    monkeypatch.setattr(supervise, "MAX_LINE", 1000)
    from io import BytesIO, StringIO
    out = StringIO()
    relay = supervise._Relay(out)
    supervise._pump(BytesIO(path.read_bytes()), relay)
    assert out.getvalue().splitlines() == ["STATUS: VALIDATING"]       # the oversized event's DRAFTING is not seen
    assert relay.result is not None


def test_an_oversized_final_result_counts_as_missing(tmp_path: Path) -> None:
    huge = event_result("x" * (supervise.MAX_LINE + 10))
    rc, lines, _ = supervise_run(stream_file(tmp_path, [huge]))
    assert lines == ["STATUS: FAILED INTERNAL"] and rc == 1


def test_standard_error_of_the_agent_never_reaches_the_logs(tmp_path: Path) -> None:
    canary = "CANARY-secret-in-stderr"
    rc, lines, err = supervise_run(stream_file(tmp_path, [event_result()]), env={"FAKE_STDERR": canary})
    assert canary not in err and not any(canary in ln for ln in lines)
    assert rc == 0


def test_usage_line_takes_numbers_only(tmp_path: Path) -> None:
    ev = event_result(num_turns="3; rm -rf /", total_cost_usd=float("inf"), duration_ms=-5)
    _, lines, _ = supervise_run(stream_file(tmp_path, [ev]))
    assert lines == ["STATUS: DONE"]
    _, lines, _ = supervise_run(stream_file(tmp_path, [event_result(num_turns=True, total_cost_usd=1e12, duration_ms=None)]))
    assert lines == ["STATUS: DONE"]
    _, lines, _ = supervise_run(stream_file(tmp_path, [event_result(num_turns=0, total_cost_usd=0, duration_ms=3.9)]))
    assert lines == ["USAGE: turns=0 cost_usd=0.0000 duration_ms=3", "STATUS: DONE"]


@pytest.mark.parametrize("figure,expected", [
    (0, b'{"cost_usd":0.0}'), (0.0123, b'{"cost_usd":0.0123}'), (1, b'{"cost_usd":1.0}'), (0.123456, b'{"cost_usd":0.1235}'),
    (12.5, b'{"cost_usd":12.5}'), (123456789.0, b'{"cost_usd":123456789.0}'),
])
def test_cost_record_is_one_number(figure: float, expected: bytes) -> None:
    assert supervise.cost_record({"total_cost_usd": figure}) == expected


@pytest.mark.parametrize("result", [
    {}, {"total_cost_usd": None}, {"total_cost_usd": True}, {"total_cost_usd": False}, {"total_cost_usd": "0.5"}, {"total_cost_usd": float("inf")},
    {"total_cost_usd": float("nan")}, {"total_cost_usd": -0.01}, {"total_cost_usd": 1e12}, {"total_cost_usd": [1]}, {"num_turns": 3, "duration_ms": 5},
])
def test_cost_record_is_none_without_a_plain_figure(result: dict) -> None:
    assert supervise.cost_record(result) is None


def test_cost_record_carries_the_cost_and_nothing_the_agent_could_have_written() -> None:
    record = supervise.cost_record({"total_cost_usd": 0.5, "result": "STATUS: DONE; rm -rf /", "num_turns": 9, "duration_ms": 7, "x": "y"})
    assert record == b'{"cost_usd":0.5}' and record.isascii()


# ── how the agent is started ────────────────────────────────────────────────────────────────────────

def test_output_format_is_forced_and_the_caller_cannot_override_it(tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    stream = stream_file(tmp_path, [event_result()])
    supervise_run(stream, args=("-p", "go", "--output-format", "text", "--max-turns", "3"), env={"FAKE_RECORD": str(record)})
    argv = json.loads(record.read_text())["argv"]
    assert argv[:5] == ["-p", "go", "--output-format", "text", "--max-turns"] and argv[5] == "3"
    assert argv[-3:] == ["--output-format", "stream-json", "--verbose"]      # the last occurrence wins in Claude Code


def test_the_agent_gets_no_pythonpath_no_stdin_and_its_own_process_group(tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    supervise_run(stream_file(tmp_path, [event_result()]), env={"FAKE_RECORD": str(record), "KEEP_ME": "1"})
    rec = json.loads(record.read_text())
    assert "PYTHONPATH" not in rec["env"] and "KEEP_ME" in rec["env"]
    assert rec["stdin"] == "" and rec["pgid_is_own"] is True


@pytest.mark.parametrize("args", [(), ("--max-turns", "3"), ("--version",)], ids=["bare", "no-prompt", "version"])
def test_without_a_prompt_the_agent_is_not_started(tmp_path: Path, args: tuple[str, ...]) -> None:
    record = tmp_path / "record.json"
    proc = start(None, args=args, env={"FAKE_RECORD": str(record)})
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 2 and out == ""
    assert err == "docugen-box: the agent must be started headless (-p PROMPT)\n"
    assert not record.exists()


def test_an_agent_that_cannot_start_is_a_usage_error() -> None:
    proc = subprocess.run([sys.executable, "-P", "-m", "docugen.box", "--", "/nonexistent/claude", "-p", "go"],
                          env=supervisor_env(), capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert proc.returncode == 2 and proc.stdout == ""
    assert proc.stderr == "docugen-box: cannot start the agent\n"


# ── processes and signals ───────────────────────────────────────────────────────────────────────────

def test_a_background_process_holding_the_pipe_does_not_hold_the_session(tmp_path: Path) -> None:
    started = time.monotonic()
    rc, lines, _ = supervise_run(stream_file(tmp_path, [event_result()]), env={"FAKE_BACKGROUND": "60"})
    assert time.monotonic() - started < 20
    assert lines[-1] == "STATUS: DONE" and rc == 0


def test_a_stop_signal_ends_the_agent_and_the_session_fails(tmp_path: Path) -> None:
    proc = start(stream_file(tmp_path, [event_assistant("STATUS: STARTED")]), env={"FAKE_SLEEP": "120"})
    deadline = time.monotonic() + 30
    first = proc.stdout.readline() if proc.stdout else ""
    assert first == "STATUS: STARTED\n", first
    assert time.monotonic() < deadline
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert out.splitlines() == ["STATUS: FAILED INTERNAL"]
    assert proc.returncode == 1
