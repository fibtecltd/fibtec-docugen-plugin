"""The PDF check: what a printer such as Chromium writes passes, and anything that can act on its reader does not.

PDFs are built here from bytes, never committed: a hostile one is a few lines, and a committed active PDF trips scanners.
"""

from __future__ import annotations

import pytest

from docugen.verify import pdf_check
from docugen.verify.pdf_check import FORBIDDEN_NAMES, MAX_BYTES, MAX_FINDINGS, MAX_PAGES, MAX_URI_CHARS, check_pdf

HEADER = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
TRAILER = b"xref\n0 1\n0000000000 65535 f \ntrailer\n<< /Size 1 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"


def obj(number: int, body: bytes) -> bytes:
    return f"{number} 0 obj\n".encode() + body + b"\nendobj\n"


def stream(number: int, data: bytes, entries: bytes = b"", *, length: int | None = None) -> bytes:
    declared = len(data) if length is None else length
    return obj(number, b"<< /Length %d %b>>\nstream\n" % (declared, entries) + data + b"\nendstream")


def pdf(*objects: bytes, header: bytes = HEADER, trailer: bytes = TRAILER) -> bytes:
    return header + b"".join(objects) + trailer


def page_tree(extra: bytes = b"") -> list[bytes]:
    return [
        obj(1, b"<< /Type /Catalog /Pages 2 0 R %b>>" % extra),
        obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R >>"),
        stream(4, b"BT /F1 12 Tf 72 720 Td (Hello) Tj ET"),
    ]


def rules(findings) -> set[str]:
    return {f.rule for f in findings}


# ── what a printer writes passes ──────────────────────────────────────────────────────────────────

def test_a_plain_document_passes() -> None:
    assert check_pdf(pdf(*page_tree())) == []


def test_links_to_http_and_https_and_inside_the_document_pass() -> None:
    link_a = b"<< /Type /Annot /Subtype /Link /Rect [0 0 1 1] /A << /S /URI /URI (https://example.com/a?b=c) >> >>"
    link_b = b"<< /Type /Annot /Subtype /Link /Rect [0 0 1 1] /A << /S /URI /URI (http://example.com/) >> >>"
    link_c = b"<< /Type /Annot /Subtype /Link /Rect [0 0 1 1] /Dest [3 0 R /XYZ 0 800 0] >>"
    data = pdf(*page_tree(), obj(5, link_a), obj(6, link_b), obj(7, link_c))
    assert check_pdf(data) == []


def test_a_title_with_parentheses_and_a_slash_word_is_not_a_name() -> None:
    info = obj(5, b"<< /Title (Report \\(draft\\) 2026) /Producer (Skia/PDF m147) >>")
    assert check_pdf(pdf(*page_tree(), info)) == []


def test_the_data_of_a_stream_is_not_scanned() -> None:
    inert = stream(5, b"/JavaScript /OpenAction << /S /Launch >> /JS (x) 6 0 obj stream")
    assert check_pdf(pdf(*page_tree(), inert)) == []


def test_a_stream_may_end_its_data_with_any_line_ending() -> None:
    for ending in (b"\n", b"\r\n", b"\r", b""):
        body = b"abc"
        data = pdf(*page_tree(), b"5 0 obj\n<< /Length 3 >>\nstream\n" + body + ending + b"endstream\nendobj\n")
        assert check_pdf(data) == [], ending


# ── names that act ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(FORBIDDEN_NAMES))
def test_every_forbidden_name_is_found(name: str) -> None:
    findings = check_pdf(pdf(*page_tree(b"/Extra /%b " % name.encode())))
    assert rules(findings) == {"pdf-active-content"}
    assert all(f"/{name}" in f.message for f in findings)


@pytest.mark.parametrize("spelling", [b"/J#61vaScript", b"/#4aS", b"/Open#41ction", b"/#4Cau#6e#63h", b"/Embedded#46iles"])
def test_a_name_written_with_hex_escapes_is_found_as_what_it_means(spelling: bytes) -> None:
    assert rules(check_pdf(pdf(*page_tree(b"/Extra %b " % spelling)))) == {"pdf-active-content"}


def test_a_name_that_only_starts_like_a_forbidden_one_is_not_found() -> None:
    assert check_pdf(pdf(*page_tree(b"/JSON /Javascript /OpenActions /AAA /Launcher "))) == []


def test_a_forbidden_name_in_a_stream_object_s_dictionary_is_found() -> None:
    hostile = stream(5, b"data", b"/Type /EmbeddedFile ")
    assert rules(check_pdf(pdf(*page_tree(), hostile))) == {"pdf-active-content"}


def test_object_streams_and_encryption_are_refused_because_they_hide_names() -> None:
    assert rules(check_pdf(pdf(*page_tree(), stream(5, b"x", b"/Type /ObjStm /N 1 /First 4 ")))) == {"pdf-active-content"}
    assert rules(check_pdf(pdf(*page_tree(), obj(5, b"<< /Filter /Standard /V 4 >>"), trailer=TRAILER.replace(b"/Root", b"/Encrypt 5 0 R /Root")))) \
        == {"pdf-active-content"}


# ── streams that lie ──────────────────────────────────────────────────────────────────────────────

HIDDEN = b"endstream\nendobj\n6 0 obj\n<< /S /JavaScript /JS (app.alert(1)) >>\nendobj\n7 0 obj\n<< /Length 0 >>\nstream\n"


def test_a_stream_that_ends_early_exposes_what_follows_it() -> None:
    data = pdf(*page_tree(), b"5 0 obj\n<< /Length 1 >>\nstream\nx\n" + HIDDEN + b"\nendstream\nendobj\n")
    assert rules(check_pdf(data)) == {"pdf-active-content"}


def test_a_length_that_spans_a_hidden_object_behind_a_false_end_is_refused() -> None:
    """A viewer that ends a stream at the first ``endstream`` reads the hidden object as structure; a scan that obeys /Length
    would skip it. The scan must not skip what a lenient viewer reads."""
    body = b"x\n" + HIDDEN
    data = pdf(*page_tree(), b"5 0 obj\n<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream\nendobj\n")
    assert rules(check_pdf(data)) == {"pdf-stream-length"}


def test_a_false_object_end_inside_the_data_is_refused_too() -> None:
    body = b"x\nendobj\n6 0 obj\n<< /S /Launch >>\nendobj\n"
    data = pdf(*page_tree(), b"5 0 obj\n<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream\nendobj\n")
    assert rules(check_pdf(data)) == {"pdf-stream-length"}


def test_a_stream_whose_length_is_long_is_refused_too() -> None:
    assert rules(check_pdf(pdf(*page_tree(), stream(5, b"abc", length=999)))) == {"pdf-stream-length"}


@pytest.mark.parametrize("entries", [b"/Length 12 0 R ", b""])
def test_a_stream_with_an_indirect_or_missing_length_is_refused(entries: bytes) -> None:
    data = pdf(*page_tree(), b"5 0 obj\n<< " + entries + b">>\nstream\nabc\nendstream\nendobj\n")
    assert rules(check_pdf(data)) == {"pdf-stream-length"}


def test_the_length_of_another_stream_is_not_borrowed() -> None:
    data = pdf(*page_tree(), b"5 0 obj\n<< /Filter /FlateDecode >>\nstream\nabc\nendstream\nendobj\n")
    assert rules(check_pdf(data)) == {"pdf-stream-length"}


# ── links ─────────────────────────────────────────────────────────────────────────────────────────

def link(target: bytes) -> bytes:
    return obj(5, b"<< /Type /Annot /Subtype /Link /A << /S /URI /URI %b >> >>" % target)


@pytest.mark.parametrize("target", [
    b"(javascript:alert\\(1\\))", b"(file:///etc/passwd)", b"(//evil.example/x)", b"(data:text/html,x)", b"(mailto:a@b.example)",
    b"(https://exam ple.com/)", b"(https://example.com/\\000x)", b"(https://example.com/\\012x)", b"(https://example.com/\\0x)", b"()", b"(ftp://example.com/)", b"(https:/example.com)",
    b"<6a6176617363726970743a616c65727428312929>",                    # javascript:alert(1) in hexadecimal
    b"<66696c653a2f2f2f6574632f706173737764>",                        # file:///etc/passwd
    b"(https://example.com/" + b"a" * MAX_URI_CHARS + b")",
    b"(https://example.com/never closed",
])
def test_a_link_that_is_not_http_or_https_is_found(target: bytes) -> None:
    assert rules(check_pdf(pdf(*page_tree(), link(target)))) == {"pdf-uri"}


@pytest.mark.parametrize("target", [b"(HTTPS://EXAMPLE.COM/)", b"(https://example.com/\\(x\\))", b"(https://example.com/a\\\\b)",
                                    b"(https://example.com/\\101)", b"(https://example.com/a\\\nb)",
                                    b"<68747470733a2f2f6578616d706c652e636f6d2f>"])
def test_a_link_that_is_http_or_https_passes_however_it_is_written(target: bytes) -> None:
    assert check_pdf(pdf(*page_tree(), link(target))) == []


# ── the shape of the file ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("data, rule", [
    (b"<html>not a pdf</html>\n%%EOF\n", "pdf-header"),
    (b"%PDF-3.0\n" + b"%%EOF\n", "pdf-header"),
    (b"", "pdf-header"),
])
def test_a_file_that_does_not_start_like_a_pdf_is_refused(data: bytes, rule: str) -> None:
    assert rule in rules(check_pdf(data))


def test_a_file_without_an_end_marker_is_refused() -> None:
    assert rules(check_pdf(pdf(*page_tree(), trailer=b""))) == {"pdf-trailer"}


def test_the_end_marker_must_be_at_the_end() -> None:
    assert rules(check_pdf(pdf(*page_tree(), trailer=b"%%EOF\n" + b"\n" * 2000))) == {"pdf-trailer"}


def test_a_file_over_the_size_cap_is_refused_without_being_read() -> None:
    findings = check_pdf(b"%PDF-1.7\n" + b"0" * MAX_BYTES + b"\n%%EOF\n")
    assert [f.rule for f in findings] == ["pdf-too-large"]


def test_the_page_cap() -> None:
    def many(n: int) -> bytes:
        return pdf(obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"), obj(2, b"<< /Type /Pages /Kids [] /Count 0 >>"),
                   *[obj(10 + i, b"<< /Type /Page /Parent 2 0 R >>") for i in range(n)])
    assert check_pdf(many(MAX_PAGES)) == []
    assert rules(check_pdf(many(MAX_PAGES + 1))) == {"pdf-pages"}


def test_a_page_tree_node_is_not_a_page() -> None:
    nodes = [obj(10 + i, b"<< /Type /Pages /Kids [] /Count 0 >>") for i in range(MAX_PAGES + 5)]
    assert check_pdf(pdf(*page_tree(), *nodes)) == []


def test_the_findings_are_capped_and_carry_the_path() -> None:
    hostile = pdf(*page_tree(b"/JS /JS /JS /JS " * 40))
    findings = check_pdf(hostile, "docs/pdf/x.pdf")
    assert len(findings) == MAX_FINDINGS and {f.path for f in findings} == {"docs/pdf/x.pdf"}


def test_a_finding_says_the_rule_and_not_what_the_file_holds() -> None:
    secret = b"sk-ant-api03-" + b"A1b2C3d4" * 6
    findings = check_pdf(pdf(*page_tree(), stream(5, secret), obj(6, b"<< /S /JavaScript /JS (" + secret + b") >>")))
    assert findings and all(secret.decode() not in str(f) for f in findings)


def test_the_limits_are_the_ones_the_tree_check_uses() -> None:
    from docugen.verify import manifest

    assert manifest.MAX_PDF_BYTES == pdf_check.MAX_BYTES == 10 * 1024 * 1024
