"""Allowlist sanitiser for the SVG diagrams DocuGen delivers.

Mermaid and PlantUML produce the SVG inside the box, after a model has read hostile text, so the
deliver step re-checks every SVG with this module and never trusts the box's own report.

Approach: parse, then rebuild only what is allowed.

- The document must be well-formed XML with no DOCTYPE and no entity declarations (no billion-laughs,
  no external entities), no more than ``MAX_ELEMENTS`` elements, and no more than ``MAX_BYTES`` bytes.
- Elements are an allowlist. ``script``, ``foreignObject``, ``iframe``, ``object``, ``embed``, ``image``,
  ``animate*`` and ``set`` are not on it, and are removed with their subtree. ``a`` is unwrapped: its
  children stay, the link goes.
- Attributes are an allowlist. Event handlers (``on*``) are never on it. ``href`` and ``xlink:href`` are
  kept only when they point to a fragment (``#id``) of the same document.
- ``style`` attributes and ``<style>`` elements pass only when the CSS has no ``@import``, ``expression(``,
  ``javascript:``, ``behavior`` or ``-moz-binding``, and every ``url(...)`` points to a fragment.

``sanitize`` returns the cleaned bytes and what it removed. ``check`` is the strict form for the deliver
step: any removal is a failure, because a clean renderer output needs none.

Mermaid draws labels in ``foreignObject`` unless ``htmlLabels`` is off, so the box's Mermaid config must
turn ``htmlLabels`` off for this check to pass; the integration test belongs with the pipeline.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .common import Finding

MAX_BYTES = 5 * 1024 * 1024
MAX_ELEMENTS = 200_000
MAX_DEPTH = 256

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"

ALLOWED_ELEMENTS = frozenset({
    "svg", "g", "defs", "title", "desc", "style", "symbol", "use", "path", "rect", "circle", "ellipse",
    "line", "polyline", "polygon", "text", "tspan", "textPath", "marker", "linearGradient",
    "radialGradient", "stop", "clipPath", "pattern", "filter", "feGaussianBlur", "feOffset", "feBlend",
    "feMerge", "feMergeNode", "feFlood", "feComposite", "feColorMatrix", "feDropShadow", "metadata",
})
UNWRAP_ELEMENTS = frozenset({"a"})

ALLOWED_ATTRIBUTES = frozenset({
    "id", "class", "style", "d", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width",
    "height", "points", "transform", "viewBox", "preserveAspectRatio", "version", "fill", "fill-opacity",
    "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-linecap", "stroke-linejoin",
    "stroke-dasharray", "stroke-dashoffset", "stroke-miterlimit", "opacity", "font-family", "font-size",
    "font-style", "font-weight", "text-anchor", "text-decoration", "dominant-baseline", "alignment-baseline",
    "letter-spacing", "dx", "dy", "rotate", "textLength", "lengthAdjust", "marker-start", "marker-mid",
    "marker-end", "markerWidth", "markerHeight", "markerUnits", "refX", "refY", "orient", "clip-path",
    "clipPathUnits", "gradientUnits", "gradientTransform", "offset", "stop-color", "stop-opacity",
    "patternUnits", "patternTransform", "visibility", "display", "filter", "stdDeviation", "in", "in2",
    "result", "mode", "operator", "values", "type", "flood-color", "flood-opacity", "dominantBaseline",
    "shape-rendering", "text-rendering", "color", "role", "clip-rule", "name", "xmlns", "xmlns:xlink",
    "baseProfile", "contentStyleType", "zoomAndPan", "preserveAspectRatio",
})
# aria-* and data-* attributes carry text for assistive technology and for the renderer's own bookkeeping
# (Mermaid: data-id, data-points, data-edge; PlantUML: data-entity-1, data-qualified-name). They are inert; the
# value checks below (no javascript:, no url() outside the document) still apply to them.
_INERT_ATTRIBUTE = re.compile(r"^(?:aria|data)-[A-Za-z0-9_.:-]+$")
_URL_REF = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)
_BAD_CSS = re.compile(r"@import|expression\s*\(|javascript:|vbscript:|behavior\s*:|-moz-binding|\\", re.IGNORECASE)
_FRAGMENT = re.compile(r"^#[A-Za-z_][\w.:-]*$")
_DOCTYPE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)", re.IGNORECASE)


@dataclass
class Sanitized:
    data: bytes
    removed: list[Finding] = field(default_factory=list)


class InvalidSvg(Exception):
    """The input is not usable SVG at all (not XML, DOCTYPE, too big)."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"[{rule}] {message}")
        self.rule = rule
        self.message = message

    def to_finding(self, path: str = "") -> Finding:
        return Finding(self.rule, self.message, path)


def _local(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


def _css_ok(css: str) -> bool:
    if _BAD_CSS.search(css):
        return False
    return all(_FRAGMENT.match(m.group(2).strip()) for m in _URL_REF.finditer(css))


def _attr_ok(name: str, value: str) -> tuple[bool, str]:
    ns, local = _local(name)
    if ns == XLINK_NS and local == "href" or (not ns and local == "href"):
        return (bool(_FRAGMENT.match(value.strip())), "href-not-fragment")
    if ns == XML_NS and local in ("space", "lang"):
        return True, ""
    if ns == "http://www.w3.org/2000/xmlns/":
        return True, ""
    if ns:
        return False, "attribute-namespace"
    if local.lower().startswith("on"):
        return False, "event-handler"
    if local not in ALLOWED_ATTRIBUTES and not _INERT_ATTRIBUTE.match(local):
        return False, "attribute-not-allowed"
    if local == "style" and not _css_ok(value):
        return False, "style-unsafe"
    if "url(" in value.lower() and local != "style" and not _css_ok(value):
        return False, "url-not-fragment"
    if "javascript:" in value.lower():
        return False, "javascript-url"
    return True, ""


def _clean(elem: ET.Element, removed: list[Finding], depth: int) -> None:
    if depth > MAX_DEPTH:
        raise InvalidSvg("too-deep", f"more than {MAX_DEPTH} levels")
    for name in list(elem.attrib):
        ok, why = _attr_ok(name, elem.attrib[name])
        if not ok:
            removed.append(Finding(why, f"attribute {name!r} removed from <{_local(elem.tag)[1]}>"))
            del elem.attrib[name]
    out: list[ET.Element] = []
    for child in list(elem):
        ns, name = _local(child.tag)
        if ns != SVG_NS:
            removed.append(Finding("element-namespace", f"element {{{ns}}}{name} removed with its content"))
            continue
        if name in UNWRAP_ELEMENTS:
            removed.append(Finding("link-removed", "<a> unwrapped; its link removed"))
            _clean(child, removed, depth + 1)
            out.extend(list(child))
            continue
        if name not in ALLOWED_ELEMENTS:
            removed.append(Finding("element-not-allowed", f"<{name}> removed with its content"))
            continue
        if name == "style" and not _css_ok("".join(child.itertext())):
            removed.append(Finding("style-unsafe", "<style> removed"))
            continue
        _clean(child, removed, depth + 1)
        out.append(child)
    elem[:] = out


def _parse(data: bytes) -> ET.Element:
    if len(data) > MAX_BYTES:
        raise InvalidSvg("too-large", f"SVG over {MAX_BYTES} bytes")
    if _DOCTYPE.search(data):
        raise InvalidSvg("doctype", "DOCTYPE or entity declaration")
    try:
        root = ET.fromstring(data)  # noqa: S314 - DOCTYPE and entities are rejected above
    except ET.ParseError as exc:
        raise InvalidSvg("not-xml", f"not well-formed XML: {exc}") from exc
    if sum(1 for _ in root.iter()) > MAX_ELEMENTS:
        raise InvalidSvg("too-many-elements", f"more than {MAX_ELEMENTS} elements")
    if _local(root.tag) != (SVG_NS, "svg"):
        raise InvalidSvg("not-svg", "the root element is not <svg> in the SVG namespace")
    return root


def sanitize(data: bytes) -> Sanitized:
    root = _parse(data)
    removed: list[Finding] = []
    _clean(root, removed, 0)
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    return Sanitized(ET.tostring(root, encoding="utf-8", xml_declaration=False), removed)


def check_svg(data: bytes, path: str = "") -> list[Finding]:
    """The strict form: no finding means the SVG needed no change."""
    try:
        result = sanitize(data)
    except InvalidSvg as exc:
        return [exc.to_finding(path)]
    return [Finding(f.rule, f.message, path) for f in result.removed]
