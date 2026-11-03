"""The box's side of the presigned URLs: what it will talk to, what it will not follow, what it never says."""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from docugen.box import transfer
from docugen.box.transfer import TransferError

SECRET = "SECRETSIGNATURE0123456789"


class Hits:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []


@contextmanager
def serve(behaviour: Callable[[BaseHTTPRequestHandler, bytes], None]) -> Iterator[tuple[str, Hits]]:
    """A loopback HTTP server whose handler is ``behaviour(handler, body)``. Yields its base URL and what it received."""
    hits = Hits()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a: object) -> None:
            pass

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            hits.requests.append((self.command, self.path, body, {k.lower(): v for k, v in self.headers.items()}))
            behaviour(self, body)

        do_GET = do_POST = _handle  # noqa: N815

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", hits
    finally:
        server.shutdown()
        server.server_close()


def reply(handler: BaseHTTPRequestHandler, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
    handler.send_response(status)
    for k, v in (headers or {}).items():
        handler.send_header(k, v)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


# ── which URLs are acceptable ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    f"https://docugen-pilot-sessiondata.s3.eu-west-1.amazonaws.com/source/t/s/source.tar?X-Amz-Signature={SECRET}",
    "https://s3.eu-west-1.amazonaws.com/bucket/key",
    "https://BUCKET.S3.EU-WEST-1.AMAZONAWS.COM/key",
    "http://127.0.0.1:8080/x", "http://localhost/x", "http://[::1]:8080/x",
])
def test_acceptable_urls(url: str) -> None:
    assert transfer.check_url(url).scheme in ("http", "https")


@pytest.mark.parametrize("url", [
    "", None, 42, [], "https://", "ftp://bucket.s3.amazonaws.com/x", "file:///etc/passwd", "gopher://a.amazonaws.com/",
    "http://example.com/x", "https://example.com/x", "http://bucket.s3.amazonaws.com/x",   # plain http to a real host
    "https://amazonaws.com/x", "https://.amazonaws.com/x", "https://bucket.s3.amazonaws.com.evil.com/x",
    "https://evil.com/.amazonaws.com", "https://evil.com#.amazonaws.com", "https://evil.com\\.amazonaws.com/",
    "https://169.254.169.254/latest/meta-data/", "https://[::1]/x", "http://127.0.0.1.evil.com/x", "http://0.0.0.0/x",
    "https://user:pw@bucket.s3.amazonaws.com/x", "https://bucket.s3.amazonaws.com:abc/x",
    "https://bucket.s3.amazonaws.com/x#frag", "https://bucket.s3.amazonaws.com/x y", "https://bucket.s3.amazonaws.com/x\ny",
    "https://bucket.s3.amazonaws.com/x\x00", "https://bucket.s3.amazonaws.com/" + "a" * 5000,
])
def test_unacceptable_urls(url: object) -> None:
    with pytest.raises(TransferError) as exc:
        transfer.check_url(url)
    assert exc.value.code == TransferError.BAD_URL


# ── downloading ─────────────────────────────────────────────────────────────────────────────────────

def test_fetch_downloads_and_the_file_is_private(tmp_path: Path) -> None:
    payload = os.urandom(300_000)
    with serve(lambda h, b: reply(h, 200, payload)) as (base, hits):
        dest = tmp_path / "source.tar"
        assert transfer.fetch(f"{base}/source.tar?sig={SECRET}", str(dest), 1_000_000) == len(payload)
    assert dest.read_bytes() == payload
    assert (dest.stat().st_mode & 0o777) == 0o600
    assert [r[0] for r in hits.requests] == ["GET"]


@pytest.mark.parametrize("status,code", [(403, TransferError.NOT_FOUND), (404, TransferError.NOT_FOUND),
                                         (500, TransferError.HTTP), (503, TransferError.HTTP), (400, TransferError.HTTP)])
def test_fetch_reports_http_errors_and_leaves_nothing(tmp_path: Path, status: int, code: str) -> None:
    with serve(lambda h, b: reply(h, status, b"<Error>nope</Error>")) as (base, _):
        dest = tmp_path / "source.tar"
        with pytest.raises(TransferError) as exc:
            transfer.fetch(f"{base}/x", str(dest), 1000)
    assert exc.value.code == code and not dest.exists()


def test_fetch_does_not_follow_redirects(tmp_path: Path) -> None:
    with serve(lambda h, b: reply(h, 200, b"the other host")) as (other, other_hits):
        with serve(lambda h, b: reply(h, 302, b"", {"Location": f"{other}/stolen"})) as (base, _):
            with pytest.raises(TransferError) as exc:
                transfer.fetch(f"{base}/x", str(tmp_path / "out"), 1000)
    assert exc.value.code == TransferError.HTTP
    assert other_hits.requests == [] and not (tmp_path / "out").exists()


def test_fetch_stops_at_the_cap_when_the_length_is_declared(tmp_path: Path) -> None:
    with serve(lambda h, b: reply(h, 200, b"x" * 5000)) as (base, _):
        with pytest.raises(TransferError) as exc:
            transfer.fetch(f"{base}/x", str(tmp_path / "out"), 4999)
    assert exc.value.code == TransferError.TOO_LARGE and not (tmp_path / "out").exists()


def test_fetch_stops_at_the_cap_when_the_server_lies_about_the_length(tmp_path: Path) -> None:
    def endless(handler: BaseHTTPRequestHandler, _b: bytes) -> None:
        handler.send_response(200)
        handler.send_header("Connection", "close")
        handler.end_headers()                        # no Content-Length: the body ends when the server stops
        try:
            for _ in range(10_000):
                handler.wfile.write(b"y" * 65536)    # 650 MB if anybody read it all
        except OSError:
            pass

    with serve(endless) as (base, _):
        with pytest.raises(TransferError) as exc:
            transfer.fetch(f"{base}/x", str(tmp_path / "out"), 200_000)
    assert exc.value.code == TransferError.TOO_LARGE and not (tmp_path / "out").exists()


def test_fetch_gives_up_after_its_deadline(tmp_path: Path) -> None:
    with serve(lambda h, b: reply(h, 200, b"x" * 1000)) as (base, _):
        with pytest.raises(TransferError) as exc:
            transfer.fetch(f"{base}/x", str(tmp_path / "out"), 10_000, deadline=-1)
    assert exc.value.code == TransferError.NETWORK and not (tmp_path / "out").exists()


def test_fetch_when_nobody_answers(tmp_path: Path) -> None:
    with pytest.raises(TransferError) as exc:
        transfer.fetch("http://127.0.0.1:9/x", str(tmp_path / "out"), 1000)   # the discard port: refused
    assert exc.value.code == TransferError.NETWORK and not (tmp_path / "out").exists()


def test_fetch_never_touches_a_file_that_is_already_there(tmp_path: Path) -> None:
    existing = tmp_path / "source.tar"
    existing.write_bytes(b"precious")
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "elsewhere")
    with serve(lambda h, b: reply(h, 200, b"data")) as (base, _):
        for dest in (existing, link):
            with pytest.raises(TransferError):
                transfer.fetch(f"{base}/x", str(dest), 1000)
    assert existing.read_bytes() == b"precious"
    assert link.is_symlink() and not (tmp_path / "elsewhere").exists()


def test_fetch_ignores_proxies_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    with serve(lambda h, b: reply(h, 200, b"direct")) as (base, hits):
        assert transfer.fetch(f"{base}/x", str(tmp_path / "out"), 1000) == 6
    assert len(hits.requests) == 1


def test_fetch_refuses_a_bad_url_before_connecting(tmp_path: Path) -> None:
    with pytest.raises(TransferError) as exc:
        transfer.fetch("https://example.com/x", str(tmp_path / "out"), 1000)
    assert exc.value.code == TransferError.BAD_URL and not (tmp_path / "out").exists()


# ── the form fields of a presigned POST ─────────────────────────────────────────────────────────────

def test_fields_from_the_environment() -> None:
    raw = '{"key": "out/t/s/docs.tar", "x-amz-signature": "abc", "policy": "eyJ"}'
    assert transfer.parse_fields(raw) == {"key": "out/t/s/docs.tar", "x-amz-signature": "abc", "policy": "eyJ"}
    assert list(transfer.parse_fields(raw)) == ["key", "x-amz-signature", "policy"]   # order is kept


@pytest.mark.parametrize("raw", [
    None, 42, "", "not json", "[]", "{}", '"text"', "null", '{"a": 1}', '{"a": null}', '{"a": {"b": "c"}}', '{"a": ["b"]}',
    '{"file": "x"}', '{"FILE": "x"}', '{"": "x"}', '{"a\\nb": "x"}', '{"a\\"b": "x"}', '{"a;b": "x"}', '{"a": "x\\u0000y"}',
    '{"a": "' + "v" * 9000 + '"}', "{" + ",".join(f'"k{i}": "v"' for i in range(41)) + "}",
])
def test_fields_that_are_refused(raw: object) -> None:
    with pytest.raises(TransferError) as exc:
        transfer.parse_fields(raw)
    assert exc.value.code == TransferError.BAD_FIELDS


# ── uploading ───────────────────────────────────────────────────────────────────────────────────────

def parse_multipart(content_type: str, body: bytes) -> list[tuple[str, bytes]]:
    boundary = re.search(r"boundary=(\S+)", content_type).group(1).encode()  # type: ignore[union-attr]
    parts = body.split(b"--" + boundary)
    assert parts[0] == b"" and parts[-1] == b"--\r\n", "the body must open with the boundary and close with it"
    out = []
    for part in parts[1:-1]:
        head, _, value = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]*)"', head).group(1).decode()  # type: ignore[union-attr]
        assert value.endswith(b"\r\n")
        out.append((name, value[:-2]))
    return out


def test_upload_posts_the_fields_first_and_the_file_last(tmp_path: Path) -> None:
    payload = os.urandom(50_000)
    path = tmp_path / "docs.tar"
    path.write_bytes(payload)
    fields = {"key": "out/t/s/docs.tar", "x-amz-algorithm": "AWS4-HMAC-SHA256", "policy": "eyJleHBpcmF0aW9uIjoi", "x-amz-signature": SECRET}
    with serve(lambda h, b: reply(h, 204)) as (base, hits):
        assert transfer.upload(f"{base}/", fields, str(path), 100_000) == len(payload)
    (method, _path, body, headers), = hits.requests
    assert method == "POST" and headers["content-type"].startswith("multipart/form-data; boundary=docugen-")
    parts = parse_multipart(headers["content-type"], body)
    assert [name for name, _ in parts] == [*fields, "file"]
    assert dict(parts[:-1]) == {k: v.encode() for k, v in fields.items()}
    assert parts[-1] == ("file", parts[-1][1]) and payload in body and body.count(payload) == 1


def test_each_upload_has_its_own_boundary(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    with serve(lambda h, b: reply(h, 204)) as (base, hits):
        transfer.upload(f"{base}/", {"key": "k"}, str(path), 100)
        transfer.upload(f"{base}/", {"key": "k"}, str(path), 100)
    boundaries = {re.search(r"boundary=(\S+)", r[3]["content-type"]).group(1) for r in hits.requests}  # type: ignore[union-attr]
    assert len(boundaries) == 2


@pytest.mark.parametrize("status", [200, 201, 204])
def test_upload_accepts_what_s3_answers(tmp_path: Path, status: int) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    with serve(lambda h, b: reply(h, status)) as (base, _):
        assert transfer.upload(f"{base}/", {"key": "k"}, str(path), 100) == 1


@pytest.mark.parametrize("status", [301, 302, 307, 400, 403, 404, 412, 500])
def test_upload_failures(tmp_path: Path, status: int) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    with serve(lambda h, b: reply(h, status, b"<Error/>", {"Location": "http://127.0.0.1:9/elsewhere"})) as (base, hits):
        with pytest.raises(TransferError) as exc:
            transfer.upload(f"{base}/", {"key": "k"}, str(path), 100)
    assert exc.value.code == TransferError.HTTP and len(hits.requests) == 1   # a redirect is not followed


def test_upload_over_the_cap_is_refused_before_connecting(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x" * 101)
    with serve(lambda h, b: reply(h, 204)) as (base, hits):
        with pytest.raises(TransferError) as exc:
            transfer.upload(f"{base}/", {"key": "k"}, str(path), 100)
    assert exc.value.code == TransferError.TOO_LARGE and hits.requests == []


def test_upload_to_a_bad_url_or_a_dead_one(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    with pytest.raises(TransferError) as exc:
        transfer.upload("https://example.com/", {"key": "k"}, str(path), 100)
    assert exc.value.code == TransferError.BAD_URL
    with pytest.raises(TransferError) as exc:
        transfer.upload("http://127.0.0.1:9/", {"key": "k"}, str(path), 100)
    assert exc.value.code == TransferError.NETWORK


# ── nothing secret in what is said ──────────────────────────────────────────────────────────────────

def test_no_error_ever_contains_the_url_or_a_field_value(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x" * 50)
    caught: list[TransferError] = []

    def attempt(call: Callable[[], object]) -> None:
        with pytest.raises(TransferError) as exc:
            call()
        caught.append(exc.value)

    with serve(lambda h, b: reply(h, 500, SECRET.encode())) as (base, _):
        attempt(lambda: transfer.fetch(f"{base}/x?sig={SECRET}", str(tmp_path / "a"), 1000))
        attempt(lambda: transfer.upload(f"{base}/x?sig={SECRET}", {"policy": SECRET}, str(path), 1000))
    with serve(lambda h, b: reply(h, 200, b"z" * 5000)) as (base, _):
        attempt(lambda: transfer.fetch(f"{base}/x?sig={SECRET}", str(tmp_path / "b"), 10))
    attempt(lambda: transfer.fetch(f"http://127.0.0.1:9/x?sig={SECRET}", str(tmp_path / "c"), 10))
    attempt(lambda: transfer.fetch(f"https://evil.example.com/x?sig={SECRET}", str(tmp_path / "d"), 10))
    attempt(lambda: transfer.upload(f"http://127.0.0.1:9/x?sig={SECRET}", {"policy": SECRET}, str(path), 1000))
    attempt(lambda: transfer.parse_fields(f'{{"file": "{SECRET}"}}'))
    attempt(lambda: transfer.parse_fields(f'{{"a": 1, "b": "{SECRET}"}}'))
    assert len(caught) == 8
    for error in caught:
        assert SECRET not in str(error) and SECRET not in repr(error) and SECRET not in error.message
        # a traceback prints the chain, and urllib's own exceptions carry the URL: the chain must be cut
        assert error.__cause__ is None and (error.__suppress_context__ or error.__context__ is None)


def test_a_library_exception_that_carries_the_url_is_not_passed_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """urllib and http.client put URL text into some of their messages; this module says only what kind of failure it was."""
    url = f"http://127.0.0.1:9/x?sig={SECRET}"

    class Leaky:
        def open(self, request: object, timeout: float = 0) -> None:
            raise OSError(f"cannot reach {url} with {SECRET}")

    monkeypatch.setattr(transfer, "_opener", lambda: Leaky())
    path = tmp_path / "f"
    path.write_bytes(b"x")
    for call in (lambda: transfer.fetch(url, str(tmp_path / "out"), 10), lambda: transfer.upload(url, {"policy": SECRET}, str(path), 10)):
        with pytest.raises(TransferError) as exc:
            call()
        assert exc.value.code == TransferError.NETWORK and exc.value.message == "OSError"
        assert SECRET not in repr(exc.value) and exc.value.__suppress_context__ is True


def test_the_standard_library_only() -> None:
    import ast
    stdlib = set(__import__("sys").stdlib_module_names)
    tree = ast.parse(Path(transfer.__file__).read_text())
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
    assert imported <= stdlib | {"__future__"}, imported - stdlib
