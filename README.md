# fibtec-docugen-plugin

Software Documentation as a Service (*Documentation that stays true to the code*): the parts of Fibtec DocuGen
that run next to the customer's source and decide what may enter and leave a documentation session. The service
itself (ingest, delivery, orchestration, infrastructure) is a separate, private repository.

Like the other Fibtec plugin repositories, this one is public and is fetched by the images that use it at a pinned
commit, with no credentials.

## What is in it

| Path | What it is |
|---|---|
| `plugins/docugen-box/` | the Claude Code plugin the agent runs under inside the DocuGen box (a sealed container, one per documentation session; source read-only, no internet route): the session contract, the verify skill and `docugen-src` |
| `docugen/verify/` | the validators, standard library only: they decide what may enter DocuGen (uploaded archives, secrets) and what may leave it (Markdown, SVG, diagram sources, the output tree and its provenance manifest). The same code runs in the box, in the delivery step and in CI |
| `tests/` | the tests of both, with hostile inputs built in code rather than committed |

### The plugin: `plugins/docugen-box`

It carries no hooks, no MCP servers, no agents and no commands: only two skills and one script, and a test makes
sure it stays so.

| Path | Purpose |
|---|---|
| `skills/docugen-session/SKILL.md` | the session contract: where the source and the output are, repository text is data and never instructions, the only commands allowed, what to write and how to cite, diagram syntax per type, the status words, limits |
| `skills/docugen-verify/SKILL.md` | how to run the validators on the output and repair it in at most three bounded rounds |
| `scripts/docugen_src.py` | `docugen-src`: `tree`, `find`, `grep` and `manifest`, confined to the source directory |

`docugen-src` exists because Claude Code 2.1.294 has no Glob or Grep tool, so a model searches with shell commands,
and a shell command can read anywhere (`/proc/self/environ` holds the session token). The box allows no general
shell search. It allows this script, which resolves every path with `realpath` and refuses anything outside
`$DOCUGEN_SRC_DIR`, never follows a symbolic link, caps its output at 64 KiB and its search at 20 s, skips binary
and very large files, and records file hashes in a work directory that is never part of the delivered output.

```bash
docugen-src manifest                 # files, lines, languages, sha256 -> $DOCUGEN_WORK_DIR/files.json
docugen-src tree app --depth 2
docugen-src find '*.py'
docugen-src grep 'class \w+' --glob '*.py' -C 1
```

### The validators: `docugen/verify`

| Module | Decides |
|---|---|
| `archive_safe` | whether an uploaded zip/tar may be extracted: zip slip, absolute paths, symlinks and other special files, encrypted members, case collisions, bombs, counts and sizes; skips VCS and dependency directories, secret files and binaries |
| `secret_scan` | secret-shaped strings in text: find (with a redacted preview) or redact, before anything reaches a model and again before delivery |
| `md_policy` | whether generated Markdown is inert: no raw HTML but a few attribute-free tags, no remote images, only safe link schemes and in-repository relative paths, no bidirectional or hidden characters |
| `svg_sanitize` | allowlist rebuild of rendered SVG: no scripts, `foreignObject`, remote references, event handlers, DOCTYPE or entities |
| `diagram_check` | Mermaid and PlantUML source policy (no includes, URLs, environment access, `click`, security directives; supported types only) and a render check through the installed tool |
| `manifest` | the shape of the output (`docs/**` only, fixed extensions, no symlinks or executable bits) and its provenance manifest (commit or archive hash, tools, document and source hashes) |

```bash
python3 -P -m docugen.verify markdown docs/            # exit 0 clean, 1 findings, 2 usage; --json for machines
python3 -P -m docugen.verify archive upload.zip
python3 -P -m docugen.verify tree out/ --source src/   # output allowlist + manifest + source hashes
```

`-P` (or `-I` once installed) keeps the working directory off `sys.path`, so a file named `json.py` in the directory
being checked cannot shadow the standard library.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install pytest && .venv/bin/python -m pytest
claude plugin validate plugins/docugen-box --strict      # Claude Code 2.1.294, as in CI
```

Hostile inputs are built in the tests: archives (zip slip, symlinks, bombs, ...), SVG, Markdown, diagram sources.
Secret-shaped strings are generated at run time (`tests/helpers.py`) so no token-looking literal is ever in the
repository. `tests/fixtures/repos/` holds one tiny repository each for Python, TypeScript and Java.
`tests/fixtures/renderer-output/` holds real Mermaid and PlantUML output, and a test requires the SVG sanitizer to
accept it unchanged, so a renderer upgrade that adds an attribute fails there and names it.

## Known limits

- `svg_sanitize` refuses `foreignObject`, so Mermaid must render labels without it (`htmlLabels: false`).
- `diagram_check` has no grounding gate yet (identifiers cited by a diagram against the fact index).
- Markdown checks are line-based and conservative: a tag outside the small allowlist is a finding even in prose.
