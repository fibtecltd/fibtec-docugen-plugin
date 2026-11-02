---
name: docugen-verify
description: How to check DocuGen output with the validators and repair it in at most three bounded rounds before finishing a session. Use after writing or changing any file under docs/, and always before the final status line.
---

# Check your output, and repair it, three times at most

The validators are the same ones the delivery step runs again after you finish, and the delivery step never
trusts your report. A document that fails there is not delivered. Run them here first so you can fix what they
find.

## Run

```
docugen-verify markdown docs/
docugen-verify svg docs/
docugen-verify diagram docs/ --render
docugen-verify secrets docs/
```

Run all four from `/workspace/out`. Each prints one line per finding (`path:line: [rule] message`) and then
either `clean` or the number of findings. A finding never contains the secret it found.

| Check | What it refuses |
|---|---|
| `markdown` | raw HTML other than `<br>`, remote images, `javascript:`/`data:`/`file:` and protocol-relative links, links leaving the repository, hidden or bidirectional characters |
| `svg` | scripts, `foreignObject`, remote references, event handlers, DOCTYPE and entities |
| `diagram --render` | forbidden constructs in Mermaid and PlantUML sources, unsupported diagram types, and anything the renderer reports as a syntax error |
| `secrets` | token, key and password shapes |

## Repair loop

1. Run the four checks. If all are `clean`, you are done.
2. Fix each finding at its source: edit the Markdown or the `.mmd`/`.puml` file, then render again with
   `mmdc` or `plantuml` so the `.svg` matches. Never edit an `.svg` by hand.
3. Run the checks again. This is round 2. Fix and run again for round 3.
4. If anything is still reported after the third round, stop. Do not delete findings by weakening a check,
   renaming a file to dodge a rule, or hiding text in code. Finish with `STATUS: FAILED VALIDATION`.

A `secrets` finding means a secret-shaped string is in your output. Remove it, replace it by the name of the
variable or file where it lives, and do not paste the value into your reasoning or any message.

## Order of work

Write the Markdown first, then diagrams, then render, then check. Check once before you declare a document
finished, not after every line.
