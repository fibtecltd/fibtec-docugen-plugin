# Real Claude Code event streams

What `claude -p --output-format stream-json --verbose` printed in five sessions, as captured on **Claude Code
2.1.294** (the version the DocuGen box image pins) against a scripted stub gateway on localhost. Nothing real
was called, and the files hold no credentials. `tests/box/test_supervise.py` replays them through the
supervisor, so a Claude Code upgrade that renames a field the supervisor relies on fails there.

| File | Session | Claude Code exit code | The fields the supervisor relies on |
|---|---|---|---|
| `plain.jsonl` | one answer: `STATUS: STARTED`, prose, `STATUS: DONE` | 0 | `assistant` text blocks; `result` with `is_error: false`, `terminal_reason: "completed"`, `result` (the final text) |
| `tool-loop.jsonl` | a Bash tool call whose result contains `STATUS: DONE` (a repository file saying so), then `STATUS: INDEXED`, `STATUS: DONE` | 0 | the file's text arrives in a `user` event as a `tool_result`, never in an `assistant` event |
| `gateway-429.jsonl` | the gateway answers 429 | 1 | the error is an `assistant` event with an `error` key; `result` has `is_error: true`, `terminal_reason: "api_error"`, `api_error_status: 429` and, oddly, `subtype: "success"` |
| `max-turns.jsonl` | `--max-turns 2` with an agent that never stops calling tools | 1 | `is_error: true`, `subtype: "error_max_turns"`, `terminal_reason: "max_turns"` |
| `max-budget.jsonl` | `--max-budget-usd 0.01` with a reported usage far above it; the agent had said `STATUS: DONE` | 1 | `is_error: true`, `subtype: "error_max_budget_usd"`, `terminal_reason: "budget_exhausted"` |

Standard error was empty in all five. The image's smoke test (`claude-docker-ecs`, `scripts/ci/smoke-docugen.sh`)
runs the same shapes through the real `claude` in the real image.
