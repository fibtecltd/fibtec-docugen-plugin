# fibtec-docugen-plugin

Software Documentation as a Service (*Documentation that stays true to the code*): the parts of Fibtec DocuGen
that run next to the customer's source and decide what may enter and leave a documentation session. The service
itself (ingest, delivery, orchestration, infrastructure) is a separate, private repository.

Like the other Fibtec plugin repositories, this one is public and is fetched by the images that use it at a pinned
commit, with no credentials.

## What is in it

| Path | What it is |
|---|---|
| `plugins/docugen-box/` | the Claude Code plugin the agent runs under inside the DocuGen box (a sealed container, one per documentation session; source read-only, no inbound connections, outbound HTTPS only): the session contract, the verify skill and `docugen-src` |
| `docugen/verify/` | the validators, standard library only: they decide what may enter DocuGen (uploaded archives, secrets) and what may leave it (Markdown, SVG, diagram sources, PDFs, the output tree and its provenance manifest). The same code runs in the box, in the delivery step and in CI |
| `docugen/box/` | what runs around the agent in the box: the status vocabulary, the supervisor that lets nothing but status lines out of a session, the transfer of source in and output out, the provenance manifest, and the printing of PDFs |
| `tests/` | the tests of all three, with hostile inputs built in code rather than committed |

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
| `manifest` | the shape of the output (`docs/**` only, fixed extensions, no symlinks or executable bits) and its provenance manifest (commit or archive hash, tools, document and source hashes). PDFs are accepted only when asked for (`allow_pdf`), directly under `docs/pdf/` |
| `pdf_check` | a fail-closed scan of a PDF's bytes: no JavaScript, launch or submit actions, attachments, forms, media, encryption or object streams (names are decoded first, so `/J#61vaScript` is found), every stream's `/Length` exact with no `endstream` or `endobj` inside it, links only to http or https addresses, size and page caps, and a time that grows with the size of the file (a file built so that its links must be read again and again is refused as `pdf-complexity`) |

```bash
python3 -P -m docugen.verify markdown docs/            # exit 0 clean, 1 findings, 2 usage; --json for machines
python3 -P -m docugen.verify archive upload.zip
python3 -P -m docugen.verify tree out/ --source src/   # output allowlist + manifest + source hashes
python3 -P -m docugen.verify tree out/ --source src/ --allow-pdf   # the same, for a session that asked for PDFs
python3 -P -m docugen.verify pdf docs/pdf/              # exit 0 clean, 1 findings
```

`-P` (or `-I` once installed) keeps the working directory off `sys.path`, so a file named `json.py` in the directory
being checked cannot shadow the standard library.

### The supervisor: `docugen/box`

The box image starts the agent as `python3 -P -m docugen.box -- claude ARGS... -p PROMPT`. The supervisor runs
Claude Code with `--output-format stream-json --verbose` (appended last, so the caller cannot switch it off),
reads the event stream and prints, on standard output, only:

- `STATUS: <word>` for the progress words the agent reports (`STARTED`, `INDEXED`, `DRAFTING`, `VALIDATING`,
  `REPAIRING`, `PACKAGING`, `INJECTION_SUSPECTED`), built from the vocabulary and never echoed from the agent's
  text, repeated words once, at most 100 lines;
- one `USAGE: turns=N cost_usd=X duration_ms=N` line of numbers;
- one terminal line: `STATUS: DONE`, `STATUS: FAILED <code>` or `STATUS: LIMIT_REACHED`.

Everything else (the model's prose, tool calls and results, the agent's standard error) is read and dropped, so no
customer content reaches the container's logs. Only the agent's own `assistant` text is read for words: a source
file that contains `STATUS: DONE` reaches the agent as a tool result and says nothing.

The terminal line is not the agent's claim. It comes from Claude Code's final `result` event and exit code: an
agent that prints `STATUS: DONE` and then runs out of turns or budget is `LIMIT_REACHED`; a gateway answer 429 is
`FAILED BUDGET`; a missing result, a crash or a kill is `FAILED INTERNAL`. The agent's `DONE` or
`FAILED <code>` counts only when the session ended cleanly and it is the last line of its final message. It
is still a claim about the documents, not a proof: the deliver step re-checks every output.

| Exit code | Meaning |
|---|---|
| 0 | `STATUS: DONE` |
| 1 | `STATUS: FAILED <code>` (`NO_SOURCE`, `TOO_LARGE`, `VALIDATION`, `BUDGET`, `DENIED`, `INTERNAL`) |
| 2 | the box was started wrongly: no `-p`, or no agent to run |
| 3 | `STATUS: LIMIT_REACHED`: a turn or spend limit; the output may be partial and the deliver step decides |

`docugen/box/status.py` is the one definition of the words and codes; `skills/docugen-session/SKILL.md` lists
them for the model and a test requires the two to agree. `tests/fixtures/claude-stream/` holds real event
streams from Claude Code 2.1.294 (see the README there); the supervisor is tested against them and against
hostile streams built in code.

### Source in, output out: `transfer` and `provenance`

Two more pieces of trusted code that run in the box before and after the agent, never during it. `session` (below) calls
them.

`docugen.box.transfer` is the box's side of the presigned S3 URLs the orchestrator puts in the box's environment file
(a GET for the source, a POST for the output). A presigned URL is a bearer secret, so a URL is accepted only if it is
`https` to an `*.amazonaws.com` name (or plain `http` to a loopback address, which is how the tests and the image's smoke
test serve one); redirects are never followed and the environment's proxies are ignored; a download stops at its byte
cap however long the server keeps sending; a file that is already at the destination is never touched; and no error
message, and nothing that is returned, contains a URL or a form field value (the exception chain that would print one is
cut). The fields of a presigned POST come from the environment as a JSON object of text to text; the file is sent last, as
S3 requires.

`docugen.box.provenance` builds `docs/.docugen/manifest.json`, which nothing built before (the session skill tells the
model to leave it alone). Source hashes come from the source tree, which is root-owned and read-only for the agent; document
hashes come from what the agent wrote, after it has stopped; the source (a commit, or the SHA-256 of an uploaded archive)
and the tools come from the caller. The one thing the agent says is which files a document was derived from, in its
`## Sources` section; a path that is not a regular file inside the source tree (missing, a directory, a symbolic link, a
`..` or absolute path) is not recorded and is returned to the caller, so a manifest never claims a provenance the source
does not have. A diagram and its rendering take the sources of the Markdown documents that embed them. The result passes
`docugen.verify.manifest.check_manifest` against the real trees, which is how the tests check it.

### A whole session: `docugen/box/session.py`

`python3 -P -m docugen.box --session -- claude ARGS... -p PROMPT` runs one session from the orchestrator's environment
(`DOCUGEN_SESSION_ID`, `DOCUGEN_SOURCE_URL`, `DOCUGEN_OUT_URL`, `DOCUGEN_OUT_FIELDS` as a JSON object, and
`DOCUGEN_SOURCE_KIND` with `DOCUGEN_SOURCE_COMMIT` or `DOCUGEN_SOURCE_ARCHIVE_SHA256`):

1. **Pre-step.** Fetch the source (`transfer`), extract it safely into the source directory (`archive_safe`, the limits
   the ingest step applies), refuse a session with nothing to document.
2. **The agent**, through the supervisor, with every `DOCUGEN_*` variable removed from its environment: it never holds a
   presigned URL.
3. **Post-step**, only if the session ended `DONE` or `LIMIT_REACHED`: check the output tree (`docs/**`, fixed extensions,
   no links or executables, no PDF, at least one document), print PDFs if the session asked for them (below), build the
   provenance manifest and check it against the source, pack `docs/` and the manifest into one reproducible tar and upload
   it. A `LIMIT_REACHED` session still packs what it has; whether partial output is delivered is the deliver step's
   decision.

Between the agent and the terminal line it prints `STATUS: PACKAGING` and `OUTPUT: documents=N bytes=N unresolved_sources=N`
(with ` pdfs=N`, and ` pdf_error=WORD`, for a session that asked for PDFs; `documents` does not count them).

### PDFs: `docugen/box/pdf.py`

`DOCUGEN_PDF` (`none`, the default when it is absent, `each`, `combined` or `both`) is the orchestrator's choice and, like
every `DOCUGEN_*` variable, never reaches the agent, which cannot write a PDF either: its output is checked without PDFs
allowed. After the agent has stopped, trusted code does the printing:

1. The agent's documents (`docs/**`, regular files only, read without following a link) are copied into a private tree that
   the printer can read and cannot change.
2. The printer, `docugen-pdf` (in the box image: Chromium and a Markdown renderer, see `claude-docker-ecs`), runs as the
   unprivileged user with an empty environment, a time limit, and exactly one place to write. It is told what to print:

   ```
   docugen-pdf --in DIR --out DIR --source-id TEXT --generated-at RFC3339 --generator-version TEXT
               --doc PATH [--doc PATH ...]   [--combined NAME.pdf]   [--single PATH=NAME.pdf ...]
   ```

   `--doc` lists the Markdown documents in reading order: `README.md`, `onboarding.md`, `architecture.md`,
   `api-reference.md`, then any other in path order. `--combined` is `documentation.pdf`; `--single` names the PDF of one
   document after its path (`guides/setup.md` is `guides-setup.pdf`). All land in `docs/pdf/`. `--generator-version` is the
   **release of the image** that is running (the name its tag has, `1.1.0`): it is on the cover of the combined PDF, in the
   documents' metadata, and is the `generator.version` of the manifest. The image writes it, when it is built, to
   `/opt/docugen/etc/release` (`DOCUGEN_RELEASE_FILE` moves it); a build that is not a release says `unreleased`; with no such
   file, or one that holds anything but one word of that shape, the version of this library is used.
3. Every file it wrote is read back and checked by `pdf_check`. If any is wrong, none is kept.
4. The manifest lists each PDF as kind `pdf`, with the source files of the documents it was printed from.

Printing is an extra, not the product: if it cannot be done (no printer in the image, it fails or hangs, a file is not
acceptable, the result would be too big for the upload) the documents are delivered anyway and the `OUTPUT` line says that
no PDF was made and why, in one word of `pdf.ERRORS`.
Failures use the codes of the status vocabulary: `NO_SOURCE` (the source is gone or empty), `TOO_LARGE` (a size cap),
`VALIDATION` (the output is not an acceptable tree), `INTERNAL` (anything else). **A failure before the agent starts means
the agent never runs; a failure after it is `FAILED` whatever the agent claimed.** Any unexpected exception is
`FAILED INTERNAL` and nothing of it is printed: a traceback can hold paths from the customer's source.

The steps are plain functions (`pre_step`, `post_step`), so the same code can run in separate containers later.

**Who runs it decides the privilege model.**

- **Root (what the image is built for).** The supervisor extracts the source (root-owned, so read-only for the agent by
  ownership, not by a rule it could change), keeps its own files in a root-only directory (`/run/docugen`, never under
  `/workspace`, which the agent can write), and starts the agent as an unprivileged user with no supplementary groups.
  That user cannot read the supervisor's environment (where the presigned URLs are) or its memory. The output directory
  belongs to the agent, so **the supervisor never writes in it**: a root process that wrote the manifest there would
  follow any link the agent had left in the way. It reads the tree (links, devices and hard links are refused before
  anything is opened; files are opened without following links) and puts the manifest into the archive from memory.
  The agent is never root: `DOCUGEN_AGENT_UID` and `DOCUGEN_AGENT_GID` (default 1001) must be from 1 to 65535, and
  `DOCUGEN_AGENT_HOME` (default `/home/claude`) a plain absolute path. If the switch is refused (a platform without
  `setuid`) the session ends with `docugen-box: cannot start the agent: permission denied (switching to the agent user?)`
  and exit 2, which is how a deployment finds out.
- **Not root (the fallback).** One user does everything. After the extraction the source is made read-only by mode bits and
  the scratch files live in the work directory. The separation is then only as strong as the agent's own permission
  rules and the deliver step, which re-hashes its own copy of the source.

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
- `pdf_check` reads only the plain structure a printer such as Chromium writes. It refuses object streams and anything else it
  cannot scan, so a PDF made by another tool, or by Chromium with different settings, may be refused although it is harmless.
- Markdown checks are line-based and conservative: a tag outside the small allowlist is a finding even in prose.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
