"""Source policy and render check for Mermaid and PlantUML diagrams.

``check_mermaid`` and ``check_plantuml`` are pure: they look at the diagram source only and refuse the
constructs that read files, fetch URLs, read the environment, run script, or change the renderer's own
security settings. They run before a renderer ever sees the text, in the box and again in deliver.

``render_check`` then runs the real tool (``mmdc`` or ``plantuml``, as installed in the box image) and
reports a syntax error as a finding. It needs the tools; the policy checks do not.

Supported diagram types are an allowlist, because each renderer type is attack surface:
Mermaid flowchart, sequence, class, state and ER diagrams; PlantUML ``@startuml`` only (component,
deployment, activity, use case and the rest of the UML family all live inside it).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Sequence

from .common import Finding

MAX_BYTES = 200 * 1024
MAX_LINES = 5000

MERMAID_TYPES = ("flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram", "stateDiagram-v2",
                 "erDiagram")

_MERMAID_BAD = (
    ("mermaid-click", re.compile(r"^\s*click\s", re.IGNORECASE | re.MULTILINE),
     "'click' attaches links or callbacks"),
    ("mermaid-directive", re.compile(r"%%\{\s*(?:init|initialize|wrap)?[^}]*?(securityLevel|secure|themeCSS|"
                                     r"dompurify|callback|maxTextSize|maxEdges|fontFamily|logLevel)",
                                     re.IGNORECASE | re.DOTALL),
     "a directive that changes the renderer's security or CSS settings"),
    ("mermaid-html", re.compile(r"<\s*/?\s*(script|iframe|object|embed|img|a|style|link|svg|foreignObject|"
                                r"form|input|meta|base)\b", re.IGNORECASE),
     "HTML tag in a label"),
    ("mermaid-event", re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE), "event-handler attribute"),
    ("mermaid-url-scheme", re.compile(r"(?:javascript|vbscript|data)\s*:", re.IGNORECASE), "script or data URL"),
    ("mermaid-link-style", re.compile(r"\bhref\b", re.IGNORECASE), "href"),
)

_PLANTUML_BAD = (
    ("plantuml-include", re.compile(r"^\s*!(?:include|includeurl|includesub|include_many|include_once|import|"
                                    r"includedef)\b", re.IGNORECASE | re.MULTILINE),
     "!include and !import read files or URLs"),
    ("plantuml-theme", re.compile(r"^\s*!theme\b", re.IGNORECASE | re.MULTILINE),
     "!theme can load a theme from a URL or a file"),
    ("plantuml-function", re.compile(r"%(?:getenv|env|file_exists|dirpath|filename|load_json|loadjson|"
                                    r"get_json_attribute|call_user_func|invoke_procedure)\s*\(", re.IGNORECASE),
     "builtin that reads the environment or the filesystem"),
    ("plantuml-image", re.compile(r"<\s*img\s*[: ]", re.IGNORECASE), "<img> loads a file or URL"),
    ("plantuml-sprite-url", re.compile(r"sprite\s+\$?\w+\s+(?:jar|https?|file):", re.IGNORECASE),
     "sprite loaded from a jar, URL or file"),
    ("plantuml-link", re.compile(r"\[\[\s*(?:https?|ftp|file|javascript|data)\s*:", re.IGNORECASE),
     "hyperlink to an external or script URL"),
    ("plantuml-url-scheme", re.compile(r"(?:javascript|vbscript|file)\s*:", re.IGNORECASE),
     "script or file URL"),
    ("plantuml-pragma", re.compile(r"^\s*!pragma\b(?!\s+(?:teoz\s+true|layout\s+(?:smetana|elk)\b))",
                                   re.IGNORECASE | re.MULTILINE),
     "unreviewed !pragma"),
)
_PLANTUML_START = re.compile(r"^\s*@(\w+)", re.MULTILINE)


def _common(text: str, path: str) -> list[Finding]:
    out: list[Finding] = []
    if len(text.encode("utf-8", errors="replace")) > MAX_BYTES:
        out.append(Finding("too-large", f"diagram source over {MAX_BYTES} bytes", path))
    if text.count("\n") + 1 > MAX_LINES:
        out.append(Finding("too-many-lines", f"more than {MAX_LINES} lines", path))
    if "\x00" in text:
        out.append(Finding("nul", "NUL character", path))
    return out


def _scan(text: str, path: str, rules: Sequence[tuple[str, re.Pattern[str], str]]) -> list[Finding]:
    out: list[Finding] = []
    for rule, rx, why in rules:
        m = rx.search(text)
        if m:
            line = text.count("\n", 0, m.start()) + 1
            out.append(Finding(rule, why, path, line))
    return out


_FRONTMATTER = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_DIRECTIVE_BLOCK = re.compile(r"%%\{.*?\}%%", re.DOTALL)


def check_mermaid(text: str, path: str = "") -> list[Finding]:
    out = _common(text, path)
    body = text
    fm = _FRONTMATTER.match(body)
    if fm:
        if re.search(r"^\s*config\s*:", fm.group(1), re.MULTILINE):
            out.append(Finding("mermaid-directive", "front matter that sets renderer configuration", path, 1))
        body = body[fm.end():]
    body = _DIRECTIVE_BLOCK.sub("", body)
    first = ""
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("%%"):
            first = s
            break
    kind = re.split(r"[\s;]", first, maxsplit=1)[0] if first else ""
    if kind not in MERMAID_TYPES:
        out.append(Finding("mermaid-type", f"diagram type {kind[:30]!r} is not supported", path, 1))
    out.extend(_scan(text, path, _MERMAID_BAD))
    return out


def check_plantuml(text: str, path: str = "") -> list[Finding]:
    out = _common(text, path)
    starts = _PLANTUML_START.findall(text)
    if not starts or starts[0].lower() != "startuml":
        out.append(Finding("plantuml-type", "the source must start with @startuml", path, 1))
    if any(s.lower() not in ("startuml", "enduml") for s in starts):
        out.append(Finding("plantuml-type", "only @startuml ... @enduml is supported", path))
    if len([s for s in starts if s.lower() == "startuml"]) > 1:
        out.append(Finding("plantuml-multiple", "more than one @startuml block in a file", path))
    out.extend(_scan(text, path, _PLANTUML_BAD))
    return out


def check_source(text: str, path: str) -> list[Finding]:
    """Dispatch on the file extension: ``.mmd`` / ``.mermaid`` or ``.puml`` / ``.plantuml``."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".mmd", ".mermaid"):
        return check_mermaid(text, path)
    if ext in (".puml", ".plantuml", ".pu"):
        return check_plantuml(text, path)
    return [Finding("diagram-extension", f"not a diagram source extension: {ext or '(none)'}", path)]


def render_check(path: str, *, timeout: int = 120, mmdc: str = "mmdc", plantuml: str = "plantuml") -> list[Finding]:
    """Run the installed renderer on ``path``; a failure is a finding. Call after ``check_source``."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".mmd", ".mermaid"):
        tmp = tempfile.mkdtemp(prefix="docugen-render-")
        cmd = [mmdc, "-i", path, "-o", os.path.join(tmp, "out.svg")]
        rule = "mermaid-render"
    elif ext in (".puml", ".plantuml", ".pu"):
        tmp = ""
        cmd = [plantuml, "-checkonly", "-failfast2", path]
        rule = "plantuml-syntax"
    else:
        return [Finding("diagram-extension", f"not a diagram source extension: {ext or '(none)'}", path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False,
                              env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")})
    except FileNotFoundError:
        return [Finding("renderer-missing", f"{cmd[0]} is not installed", path)]
    except subprocess.TimeoutExpired:
        return [Finding(rule, f"renderer did not finish in {timeout}s", path)]
    finally:
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return [Finding(rule, (detail[-1] if detail else f"exit {proc.returncode}")[:200], path)]
    return []
