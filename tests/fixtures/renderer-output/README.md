# Real renderer output

SVG exactly as the box's renderers produce it, kept to prove that the SVG sanitizer accepts what they emit
(`tests/verify/test_svg_sanitize.py`, `test_real_renderer_output_needs_no_change`).

| File | Produced by |
|---|---|
| `mermaid-*.svg` | `@mermaid-js/mermaid-cli` 12.2.0 / `mermaid` 12.x with the box's Mermaid config (`securityLevel: strict`, `htmlLabels: false`) from the box's diagram fixtures (one per diagram type) |
| `plantuml-*.svg` | `plantuml-mit-1.2026.9.jar` with `PLANTUML_SECURITY_PROFILE=SANDBOX` and Graphviz 2.42 |

`htmlLabels: false` matters: with Mermaid's default, labels are drawn in `<foreignObject>` (HTML inside SVG), which the
sanitizer refuses on purpose.

When the renderers are upgraded, regenerate these files; a failing test names the attribute or element that changed.
