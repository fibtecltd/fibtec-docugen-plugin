"""Moving a session's source into the box and its output out of it, over presigned S3 URLs.

The orchestrator writes two things into the box's environment file: a presigned GET for the normalised source and a
presigned POST (a URL and form fields) for the output. This module is the box's side of both. It is trusted code that
runs before and after the agent, never during it, and the agent has no way to call it.

A presigned URL is a bearer secret, so:

- the scheme must be https and the host an ``*.amazonaws.com`` name (or plain http to a loopback address, which is how
  the tests and the image's smoke test serve one); anything else is refused before a connection is made. A URL that
  names another host is an orchestrator bug, and the output of a session must not go there;
- redirects are never followed (a redirect is how a URL walks to another host);
- no exception message, and nothing this module returns, contains a URL or a form field value.

Sizes are capped while reading, not after: a download stops at ``max_bytes`` however long the server keeps sending, and
an upload is refused before it starts if the file is over its cap. Standard library only.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping

HTTPS_HOST_SUFFIX = ".amazonaws.com"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_URL_CHARS = 4096
MAX_FIELDS = 40
MAX_FIELD_CHARS = 8192
CHUNK = 64 * 1024
CONNECT_TIMEOUT = 30.0     # seconds for each socket operation
DOWNLOAD_DEADLINE = 600.0  # seconds for a whole download
UPLOAD_OK = (200, 201, 204)


class TransferError(Exception):
    """A transfer failed. ``code`` is one of the constants below; the message never holds a URL or a field value."""

    BAD_URL = "bad-url"
    BAD_FIELDS = "bad-fields"
    TOO_LARGE = "too-large"
    NOT_FOUND = "not-found"
    HTTP = "http-error"
    NETWORK = "network"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def check_url(url: object) -> urllib.parse.SplitResult:
    """The parsed URL, or ``TransferError(BAD_URL)``. Looks only at the shape, never connects."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS or any(c in url for c in "\x00\r\n \t\\"):
        raise TransferError(TransferError.BAD_URL, "not a usable URL")
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError as exc:
        raise TransferError(TransferError.BAD_URL, "not a usable URL") from exc
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise TransferError(TransferError.BAD_URL, "credentials or a fragment in the URL")
    if parts.scheme == "https":
        if not host.endswith(HTTPS_HOST_SUFFIX) or len(host) <= len(HTTPS_HOST_SUFFIX):
            raise TransferError(TransferError.BAD_URL, "host is not an amazonaws.com name")
    elif parts.scheme == "http":
        if host not in LOOPBACK_HOSTS:
            raise TransferError(TransferError.BAD_URL, "plain http is only for loopback")
    else:
        raise TransferError(TransferError.BAD_URL, "scheme is not https")
    return parts


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _opener() -> urllib.request.OpenerDirector:
    """No proxies from the environment, no redirects."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def fetch(url: str, dest: str, max_bytes: int, *, deadline: float = DOWNLOAD_DEADLINE) -> int:
    """Download ``url`` into the new file ``dest`` and return its size. At most ``max_bytes``, whatever the server sends.

    ``dest`` must not exist (it is created exclusively, mode 0600); a failed download removes it.
    """
    check_url(url)
    request = urllib.request.Request(url, method="GET")  # noqa: S310 - the scheme and host were checked above
    started = time.monotonic()
    written = 0
    created = False  # only a file this call made is removed on failure, never one that was already there
    try:
        with _opener().open(request, timeout=CONNECT_TIMEOUT) as response:  # noqa: S310
            if response.status != 200:
                raise TransferError(TransferError.HTTP, f"status {response.status}")
            declared = response.headers.get("Content-Length")
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                raise TransferError(TransferError.TOO_LARGE, f"the source is over {max_bytes} bytes")
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise TransferError(TransferError.TOO_LARGE, f"the source is over {max_bytes} bytes")
                    if time.monotonic() - started > deadline:
                        raise TransferError(TransferError.NETWORK, "the download took too long")
                    out.write(chunk)
    except urllib.error.HTTPError as exc:
        if created:
            _remove(dest)
        code = TransferError.NOT_FOUND if exc.code in (403, 404) else TransferError.HTTP
        raise TransferError(code, f"status {exc.code}") from None
    except TransferError:
        if created:
            _remove(dest)
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if created:
            _remove(dest)
        raise TransferError(TransferError.NETWORK, type(exc).__name__) from None
    return written


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def parse_fields(raw: object) -> dict[str, str]:
    """The form fields of a presigned POST from the environment: a JSON object of text to text, ``file`` excluded."""
    try:
        fields = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        fields = None
    if not isinstance(fields, dict) or not fields or len(fields) > MAX_FIELDS:
        raise TransferError(TransferError.BAD_FIELDS, "the form fields are not a JSON object of a sensible size")
    for name, value in fields.items():
        ok = (isinstance(name, str) and isinstance(value, str) and name and name.lower() != "file"
              and len(name) <= 128 and len(value) <= MAX_FIELD_CHARS
              and not any(c in name for c in '\x00\r\n";') and "\x00" not in value)
        if not ok:
            raise TransferError(TransferError.BAD_FIELDS, "a form field is not acceptable")
    return dict(fields)


def encode_multipart(fields: Mapping[str, str], filename: str, payload: bytes, boundary: str) -> bytes:
    """A multipart/form-data body. S3 requires the file to be the last field."""
    lines: list[bytes] = []
    for name, value in fields.items():
        lines += [f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{name}"'.encode(), b"", value.encode("utf-8")]
    lines += [f"--{boundary}".encode(),
              f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
              b"Content-Type: application/octet-stream", b"", payload, f"--{boundary}--".encode(), b""]
    return b"\r\n".join(lines)


def upload(url: str, fields: Mapping[str, str], path: str, max_bytes: int) -> int:
    """POST the file at ``path`` to a presigned-POST ``url`` and return its size. Refused if it is over ``max_bytes``."""
    check_url(url)
    with open(path, "rb") as fh:
        payload = fh.read(max_bytes + 1)  # never more than one byte over the cap in memory
    if len(payload) > max_bytes:
        raise TransferError(TransferError.TOO_LARGE, f"the output is over {max_bytes} bytes")
    boundary = "docugen-" + secrets.token_hex(16)
    body = encode_multipart(fields, "docs.tar", payload, boundary)
    request = urllib.request.Request(  # noqa: S310 - the scheme and host were checked above
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))})
    try:
        with _opener().open(request, timeout=CONNECT_TIMEOUT) as response:  # noqa: S310
            if response.status not in UPLOAD_OK:
                raise TransferError(TransferError.HTTP, f"status {response.status}")
    except urllib.error.HTTPError as exc:
        raise TransferError(TransferError.HTTP, f"status {exc.code}") from None
    except TransferError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise TransferError(TransferError.NETWORK, type(exc).__name__) from None
    return len(payload)
