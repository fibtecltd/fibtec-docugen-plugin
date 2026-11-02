# fibtec-docugen-plugin

A Claude Code plugin that connects Claude to the DocuGen tool. Private.

It is what the agent runs under inside the DocuGen box (a Fargate task, one per documentation session, source
read-only, no internet route; the image is built from `claude-docker-ecs`, `Dockerfile.docugen`). It carries no
hooks, no MCP servers, no agents and no commands: only skills and one script, and a test makes sure it stays so.

## What is in it

| Path | Purpose |
|---|---|
| `skills/docugen-session/SKILL.md` | the session contract: where the source and the output are, repository text is data and never instructions, the only commands allowed, what to write and how to cite, diagram syntax per type, the status words, limits |
| `skills/docugen-verify/SKILL.md` | how to run the validators (from `fibtec-docugen`) on the output and repair it in at most three bounded rounds |
| `scripts/docugen_src.py` | `docugen-src`: `tree`, `find`, `grep` and `manifest`, confined to the source directory |

### Why `docugen-src` exists

Claude Code 2.1.294 has no Glob or Grep tool, so the model searches with shell commands, and a shell command can
read anywhere (`/proc/self/environ` holds the session token). The box allows no general shell search. It allows
this script, which resolves every path with `realpath` and refuses anything outside `$DOCUGEN_SRC_DIR`
(`/workspace/src`), never follows a symbolic link, caps its output at 64 KiB and its search at 20 s, skips binary
and very large files, and records file hashes in the work directory, never in the delivered output.

```bash
docugen-src manifest                 # files, lines, languages, sha256 -> $DOCUGEN_WORK_DIR/files.json
docugen-src tree app --depth 2
docugen-src find '*.py'
docugen-src grep 'class \w+' --glob '*.py' -C 1
```

## How it gets into the box (not done here)

`claude-docker-ecs` adds this plugin to `plugins-closed.lock` at a pinned commit and to
`profiles-closed/docugen.plugins`; installs the `docugen-src` and `docugen-verify` wrappers next to `mmdc` and
`plantuml`; allows `Bash(docugen-src *)` and `Bash(docugen-verify *)` in `claude-config-closed/settings.json`;
creates `/workspace/work`; and puts the validators from `fibtec-docugen` into the image. Until that lands, the
skills describe commands the image does not have yet, so nothing should load this plugin in a real session.

The contract between the skills and the box is tested here (`tests/test_plugin.py`): the commands a skill may
name, the status words and failure codes, and the rules the box relies on. Change them together with the box.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install pytest && .venv/bin/python -m pytest
claude plugin validate . --strict          # Claude Code 2.1.294, as in CI
```

`tests/test_docugen_src.py` covers the script (confinement, symbolic links, output and time limits, manifest);
`tests/test_plugin.py` covers the plugin's shape and the skills' text.
