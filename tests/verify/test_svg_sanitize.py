from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from docugen.verify import svg_sanitize
from docugen.verify.svg_sanitize import InvalidSvg, check_svg, sanitize

NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'

CLEAN = f"""<svg {NS} viewBox="0 0 100 50" width="100" height="50">
  <title>t</title>
  <defs><marker id="arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 z"/></marker>
    <style>.node rect {{ fill: #ececff; stroke: #9370db; }} .edge {{ marker-end: url(#arrow); font-family: "trebuchet ms", verdana; }}</style></defs>
  <g class="node" transform="translate(10,10)"><rect x="0" y="0" width="40" height="20" rx="3"/><text x="5" y="14" font-size="12">A</text></g>
  <use xlink:href="#arrow"/><use href="#arrow"/>
  <path class="edge" d="M10,10 L90,40" marker-end="url(#arrow)" style="stroke:#333"/>
</svg>""".encode()


def parse(data: bytes) -> ET.Element:
    return ET.fromstring(data)


def test_clean_svg_needs_no_change() -> None:
    assert check_svg(CLEAN) == []
    out = sanitize(CLEAN)
    assert out.removed == []
    root = parse(out.data)
    assert len(list(root.iter())) == len(list(parse(CLEAN).iter()))


def wrap(inner: str, attrs: str = "") -> bytes:
    return f"<svg {NS} {attrs}>{inner}</svg>".encode()


HOSTILE = [
    ("element-not-allowed", wrap("<script>alert(1)</script>")),
    ("element-not-allowed", wrap('<script xlink:href="http://evil.example/x.js"/>')),
    ("element-not-allowed", wrap('<foreignObject width="10" height="10"><div xmlns="http://www.w3.org/1999/xhtml">hi</div></foreignObject>')),
    ("element-not-allowed", wrap('<image href="http://evil.example/p.png" width="10" height="10"/>')),
    ("element-not-allowed", wrap('<iframe src="http://evil.example"/>')),
    ("element-not-allowed", wrap('<animate attributeName="href" to="javascript:alert(1)"/>')),
    ("element-not-allowed", wrap('<set attributeName="onmouseover" to="alert(1)"/>')),
    ("element-namespace", wrap('<h:div xmlns:h="http://www.w3.org/1999/xhtml">x</h:div>')),
    ("event-handler", wrap('<rect onload="alert(1)" width="1" height="1"/>')),
    ("event-handler", wrap('<g onclick="alert(1)"/>')),
    ("event-handler", wrap("<path d='M0,0' OnMouseOver='alert(1)'/>")),
    ("event-handler", wrap("<svg onload='alert(1)'/>")),
    ("href-not-fragment", wrap('<use href="http://evil.example/x.svg#a"/>')),
    ("href-not-fragment", wrap('<use xlink:href="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="/>')),
    ("href-not-fragment", wrap('<use xlink:href="javascript:alert(1)"/>')),
    ("style-unsafe", wrap('<rect style="fill:url(http://evil.example/x)" width="1" height="1"/>')),
    ("style-unsafe", wrap('<rect style="behavior:url(x.htc)" width="1" height="1"/>')),
    ("style-unsafe", wrap("<rect style=\"width:expression(alert(1))\" width='1' height='1'/>")),
    ("style-unsafe", wrap("<style>@import url(http://evil.example/x.css);</style>")),
    ("style-unsafe", wrap("<style>rect { background: url('http://evil.example/p.png') }</style>")),
    ("style-unsafe", wrap("<style>a { -moz-binding: url(x) }</style>")),
    ("url-not-fragment", wrap('<rect fill="url(http://evil.example/g)" width="1" height="1"/>')),
    ("attribute-not-allowed", wrap('<rect formaction="http://evil.example" width="1" height="1"/>')),
    ("attribute-namespace", wrap('<rect xlink:show="new" width="1" height="1"/>')),
]


@pytest.mark.parametrize("rule,data", HOSTILE, ids=[f"{r}-{i}" for i, (r, _) in enumerate(HOSTILE)])
def test_hostile_svg_is_cleaned_and_flagged(rule: str, data: bytes) -> None:
    result = sanitize(data)
    assert rule in {f.rule for f in result.removed}, [str(f) for f in result.removed]
    assert rule in {f.rule for f in check_svg(data)}
    out = result.data.lower()
    for needle in (b"<script", b"onload", b"onclick", b"onmouse", b"evil.example", b"javascript:", b"foreignobject", b"@import"):
        assert needle not in out, needle
    parse(result.data)  # the cleaned output is still well-formed XML


def test_a_is_unwrapped_and_its_link_dropped() -> None:
    data = wrap('<a xlink:href="https://evil.example/x"><rect width="5" height="5"/></a>')
    res = sanitize(data)
    assert "link-removed" in {f.rule for f in res.removed}
    assert b"evil.example" not in res.data
    root = parse(res.data)
    assert [c.tag.rsplit("}", 1)[-1] for c in root] == ["rect"]


def test_processing_instructions_and_comments_are_dropped() -> None:
    data = f'<?plantuml-src ABC123?><svg {NS}><!-- note --><rect width="1" height="1"/></svg>'.encode()
    out = sanitize(data).data
    assert b"plantuml-src" not in out and b"note" not in out


@pytest.mark.parametrize("rule,data", [
    ("doctype", b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd"><svg xmlns="http://www.w3.org/2000/svg"/>'),
    ("doctype", b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg">&xxe;</svg>'),
    ("doctype", b'<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]><svg xmlns="http://www.w3.org/2000/svg">&lol2;</svg>'),
    ("not-xml", b"<svg xmlns='http://www.w3.org/2000/svg'><rect></svg>"),
    ("not-xml", b"not xml at all"),
    ("not-svg", b"<html xmlns='http://www.w3.org/1999/xhtml'><body>hi</body></html>"),
    ("not-svg", b"<svg><rect/></svg>"),  # no SVG namespace
])
def test_inputs_that_are_not_usable_svg(rule: str, data: bytes) -> None:
    with pytest.raises(InvalidSvg) as exc:
        sanitize(data)
    assert exc.value.rule == rule
    assert [f.rule for f in check_svg(data, "x.svg")] == [rule]


def test_size_element_and_depth_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svg_sanitize, "MAX_BYTES", 100)
    with pytest.raises(InvalidSvg) as exc:
        sanitize(CLEAN)
    assert exc.value.rule == "too-large"
    monkeypatch.setattr(svg_sanitize, "MAX_BYTES", 5 * 1024 * 1024)
    monkeypatch.setattr(svg_sanitize, "MAX_ELEMENTS", 3)
    with pytest.raises(InvalidSvg) as exc:
        sanitize(CLEAN)
    assert exc.value.rule == "too-many-elements"
    monkeypatch.setattr(svg_sanitize, "MAX_ELEMENTS", 200_000)
    monkeypatch.setattr(svg_sanitize, "MAX_DEPTH", 5)
    with pytest.raises(InvalidSvg) as exc:
        sanitize(wrap("<g>" * 10 + "</g>" * 10))
    assert exc.value.rule == "too-deep"


# ── what the real renderers emit ─────────────────────────────────────────────────────────────────

REAL = sorted((Path(__file__).resolve().parents[1] / "fixtures" / "renderer-output").glob("*.svg"))


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_real_renderer_output_needs_no_change(path: Path) -> None:
    """Mermaid and PlantUML output, as the box produces it, passes untouched (see the README next to it)."""
    assert [str(f) for f in check_svg(path.read_bytes(), path.name)] == []


def test_there_is_real_output_from_both_renderers() -> None:
    names = {p.name.split("-")[0] for p in REAL}
    assert names == {"mermaid", "plantuml"}


@pytest.mark.parametrize("attr", ["data-id", "data-points", "data-qualified-name", "aria-roledescription", "aria-label"])
def test_inert_data_and_aria_attributes_are_kept(attr: str) -> None:
    res = sanitize(wrap(f'<g {attr}="x1">' + '<rect width="1" height="1"/></g>'))
    assert res.removed == [] and attr.encode() in res.data


@pytest.mark.parametrize("attr,value,rule", [
    ("data-link", "javascript:alert(1)", "javascript-url"),
    ("aria-label", "javascript:alert(1)", "javascript-url"),
    ("data-bg", "url(http://evil.example/x)", "url-not-fragment"),
    ("data-x", "url(//evil.example/x)", "url-not-fragment"),
])
def test_data_and_aria_attributes_get_the_same_value_checks(attr: str, value: str, rule: str) -> None:
    res = sanitize(wrap(f'<g {attr}="{value}"><rect width="1" height="1"/></g>'))
    assert rule in {f.rule for f in res.removed} and b"evil.example" not in res.data and b"javascript:" not in res.data


def test_a_data_attribute_does_not_smuggle_an_event_handler_or_other_prefix() -> None:
    for name in ("onload", "datax", "ariax", "xdata-id"):
        res = sanitize(wrap(f'<g {name}="x"><rect width="1" height="1"/></g>'))
        assert res.removed, name


def test_fe_drop_shadow_is_allowed_but_not_feimage() -> None:
    ok = sanitize(wrap('<defs><filter id="f"><feDropShadow dx="1" dy="1" stdDeviation="1"/></filter></defs>'))
    assert ok.removed == []
    bad = sanitize(wrap('<defs><filter id="f"><feImage href="http://evil.example/x.png"/></filter></defs>'))
    assert "element-not-allowed" in {f.rule for f in bad.removed} and b"evil.example" not in bad.data
