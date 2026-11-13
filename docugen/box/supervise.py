"""Run Claude Code for one documentation session and let only status lines out of the box.

``python3 -P -m docugen.box -- claude ARGS... -p PROMPT`` starts the agent with ``--output-format stream-json
--verbose`` (appended last, so the caller cannot switch it off), reads its event stream and prints, on
standard output, nothing but:

- ``STATUS: <word>`` for the progress words the agent reports (``docugen.box.status.PROGRESS``), exactly as
  built from the vocabulary, never echoed from the agent's text, at most ``MAX_PROGRESS_LINES`` of them;
- one ``USAGE: turns=N cost_usd=X duration_ms=N`` line of numbers, when the agent's final event has them;
- one terminal line, ``STATUS: DONE``, ``STATUS: FAILED <code>`` or ``STATUS: LIMIT_REACHED``.

Everything else the session produces (the model's prose, tool calls, tool results, the agent's standard error)
is read and dropped, so no customer content reaches the container's logs.

Where a status comes from. The agent's own words are only the ``text`` blocks of its ``assistant`` events: file
contents arrive in ``user`` events as tool results and are never scanned, so a repository file that contains
``STATUS: DONE`` says nothing. Terminal words are not taken from the stream. The terminal line comes from the
final ``result`` event, which Claude Code writes, and from the agent's exit code (``decide``): an agent that
prints ``STATUS: DONE`` and then runs out of budget is ``LIMIT_REACHED``. The agent's claim only counts when
the session ended cleanly.

Exit codes: 0 done, 1 failed, 2 the box was started wrongly (no ``-p``, no agent), 3 a turn or spend limit was
reached (output may be partial; the deliver step decides what to do with it).

Measured on Claude Code 2.1.294 (``tests/fixtures/claude-stream/``). Standard library only.
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import IO, Any

from . import status

MAX_LINE = 8 * 1024 * 1024      # a longer event line is skipped; a final result that long counts as missing
MAX_PROGRESS_LINES = 100        # a hostile agent cannot flood the logs
STOP_GRACE = 10.0               # seconds between SIGTERM and SIGKILL when the box is told to stop
DRAIN_GRACE = 5.0               # seconds to let the reader finish after the agent has exited

EXIT_DONE, EXIT_FAILED, EXIT_USAGE, EXIT_LIMIT = 0, 1, 2, 3
FORCED_ARGS = ["--output-format", "stream-json", "--verbose"]


@dataclass(frozen=True)
class Verdict:
    word: str
    code: str | None
    exit_code: int


def _failed(code: str) -> Verdict:
    return Verdict("FAILED", code, EXIT_FAILED)


def decide(returncode: int, result: dict[str, Any] | None) -> Verdict:
    """How the session really ended, from Claude Code's final ``result`` event and the agent's exit code."""
    if result is None or not isinstance(result.get("is_error"), bool):
        return _failed("INTERNAL")  # killed, crashed, or an event this code does not understand
    reason, subtype = result.get("terminal_reason"), result.get("subtype")
    if result["is_error"]:
        if reason in ("max_turns", "budget_exhausted") or subtype in ("error_max_turns", "error_max_budget_usd"):
            return Verdict("LIMIT_REACHED", None, EXIT_LIMIT)
        http = result.get("api_error_status")
        if http == 429:                      # the gateway's hard stop: the session's budget is spent
            return _failed("BUDGET")
        if http in (401, 403):
            return _failed("DENIED")
        return _failed("INTERNAL")
    if returncode != 0 or reason not in (None, "completed"):
        return _failed("INTERNAL")
    final = result.get("result")
    if isinstance(final, str):
        lines = [ln for ln in final.split("\n") if ln.strip(" \t\r")]
        claimed = status.parse(lines[-1]) if lines else None
        if claimed == ("DONE", None):
            return Verdict("DONE", None, EXIT_DONE)
        if claimed is not None and claimed[0] == "FAILED":
            return _failed(claimed[1] or "INTERNAL")
    return _failed("INTERNAL")  # the contract asks for DONE or FAILED <code> as the last line


def _number(value: Any) -> int | float | None:
    """``value`` if it is a plain, finite number from 0 up to (not including) 1e12, else None: the only kind of figure that leaves the box."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not 0 <= value < 1e12:
        return None
    return value


def usage_line(result: dict[str, Any]) -> str | None:
    parts = []
    for key, label, fmt in (("num_turns", "turns", "%d"), ("total_cost_usd", "cost_usd", "%.4f"),
                            ("duration_ms", "duration_ms", "%d")):
        value = _number(result.get(key))
        if value is None:
            continue
        parts.append(f"{label}=" + fmt % value)
    return "USAGE: " + " ".join(parts) if parts else None


def cost_record(result: dict[str, Any]) -> bytes | None:
    """What the box tells the service about a session's spend: ``{"cost_usd":X}`` (four places) from the agent's final event, or None when
    the event has no acceptable figure (the rule of ``usage_line``). A number, never text."""
    value = _number(result.get("total_cost_usd"))
    if value is None:
        return None
    return json.dumps({"cost_usd": round(float(value), 4)}, separators=(",", ":")).encode("ascii")


class _Relay:
    """Reads events; keeps the final ``result`` and prints the agent's progress words."""

    def __init__(self, out: IO[str]) -> None:
        self.out = out
        self.lock = threading.Lock()
        self.result: dict[str, Any] | None = None
        self.printed = 0
        self.last: str | None = None
        self.closed = False

    def emit(self, text: str) -> None:
        with self.lock:
            if not self.closed:
                self.write(text)

    def write(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def handle(self, raw: bytes) -> None:
        try:
            event = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "result":
            with self.lock:
                if not self.closed:
                    self.result = event
        elif kind == "assistant":
            self._assistant(event)

    def _assistant(self, event: dict[str, Any]) -> None:
        # An error event is Claude Code's own text about a failed request; a sub-agent's text is not the
        # session agent's. Neither is read for statuses.
        if "error" in event or event.get("parent_tool_use_id") is not None:
            return
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)):
                continue
            for line in block["text"].split("\n"):
                parsed = status.parse(line)
                if parsed is not None and parsed[0] in status.PROGRESS:
                    self._progress(parsed[0])

    def _progress(self, word: str) -> None:
        if word == self.last or self.printed >= MAX_PROGRESS_LINES:
            return
        self.last = word
        self.printed += 1
        self.emit(status.format_status(word))

    def close(self) -> None:
        with self.lock:
            self.closed = True


def _pump(stream: IO[bytes], relay: _Relay) -> None:
    """Feed the relay one event per line; a line over ``MAX_LINE`` bytes is skipped as a whole."""
    skipping = False
    while True:
        chunk = stream.readline(MAX_LINE + 1)
        if not chunk:
            return
        complete = chunk.endswith(b"\n")
        if skipping:
            skipping = not complete
            continue
        if not complete and len(chunk) > MAX_LINE:
            skipping = True
            continue
        try:
            relay.handle(chunk[:-1] if complete else chunk)
        except Exception:  # noqa: BLE001 - whatever an event holds, nothing of it may be printed or crash the box
            continue


def _killpg(proc: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)  # the agent leads its own process group (start_new_session)
    except (ProcessLookupError, PermissionError):
        pass


class UsageError(Exception):
    """The box was started wrongly (no ``-p``, no agent to run). The message is fixed text, never session content."""


def execute(argv: list[str], *, out: IO[str], env: dict[str, str] | None = None,
            user: tuple[int, int] | None = None) -> tuple[Verdict, dict[str, Any] | None]:
    """Run the agent, print its progress words to ``out`` as they arrive, and return how the session really ended and the
    agent's final ``result`` event. The usage and terminal lines are not printed: ``finish`` prints them, after whatever
    the caller still has to do (the session flow packages the output in between).

    With ``user=(uid, gid)`` the agent is started as that user and with no supplementary groups (this needs root). It
    cannot then read this process's memory or environment, and files this process owns stay out of its reach."""
    if not argv or not any(a in ("-p", "--print") for a in argv[1:]):
        raise UsageError("the agent must be started headless (-p PROMPT)")
    child_env = {k: v for k, v in (os.environ if env is None else env).items() if k != "PYTHONPATH"}
    identity: dict[str, Any] = {} if user is None else {"user": user[0], "group": user[1], "extra_groups": []}
    try:
        proc = subprocess.Popen([*argv, *FORCED_ARGS], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=child_env, start_new_session=True, **identity)
    except PermissionError:
        # The first thing a deployment learns about whether the agent can be switched to its own user.
        raise UsageError("cannot start the agent: permission denied (switching to the agent user?)") from None
    except OSError:
        raise UsageError("cannot start the agent") from None
    assert proc.stdout is not None  # noqa: S101 - stdout=PIPE

    def on_stop(_signum: int, _frame: Any) -> None:
        _killpg(proc, signal.SIGTERM)
        timer = threading.Timer(STOP_GRACE, _killpg, args=(proc, signal.SIGKILL))
        timer.daemon = True
        timer.start()

    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():  # signal handlers can only be set there
        previous = {s: signal.signal(s, on_stop) for s in (signal.SIGTERM, signal.SIGINT)}
    relay = _Relay(out)
    reader = threading.Thread(target=_pump, args=(proc.stdout, relay), daemon=True)
    reader.start()
    try:
        returncode = proc.wait()
        _killpg(proc, signal.SIGKILL)  # anything the agent left running; it also closes a pipe they held open
        reader.join(DRAIN_GRACE)
    finally:
        relay.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        proc.stdout.close()

    # The relay is closed: a reader that is still alive can neither print nor change the result any more.
    return decide(returncode, relay.result), relay.result


def finish(out: IO[str], verdict: Verdict, result: dict[str, Any] | None) -> int:
    """Print the usage line (numbers from the agent's final event) and the terminal line, and return the exit code."""
    if result is not None and (line := usage_line(result)):
        out.write(line + "\n")
    out.write(status.format_status(verdict.word, verdict.code) + "\n")
    out.flush()
    return verdict.exit_code


def run(argv: list[str], *, out: IO[str] | None = None, env: dict[str, str] | None = None) -> int:
    out = out or sys.stdout
    try:
        verdict, result = execute(argv, out=out, env=env)
    except UsageError as exc:
        sys.stderr.write(f"docugen-box: {exc}\n")
        return EXIT_USAGE
    return finish(out, verdict, result)


def main(argv: list[str]) -> int:
    if argv[:1] == ["--"]:
        argv = argv[1:]
    return run(argv)
