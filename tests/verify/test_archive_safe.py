"""Hostile and clean archives for the ingest extractor. Archives are built in code: no binary blobs."""

from __future__ import annotations

import io
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from docugen.verify.archive_safe import Limits, UnsafeArchive, check_archive, safe_extract


def make_zip(path: Path, members: dict[str, bytes | str], *, mutate=None) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    if mutate:
        path.write_bytes(mutate(path.read_bytes()))
    return path


def make_tar(path: Path, entries: list[tuple[tarfile.TarInfo, bytes | None]], mode: str = "w:gz") -> Path:
    with tarfile.open(path, mode) as tf:  # type: ignore[call-overload]
        for info, data in entries:
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def tinfo(name: str, data: bytes = b"", type_: bytes = tarfile.REGTYPE, linkname: str = "") -> tuple[tarfile.TarInfo, bytes | None]:
    info = tarfile.TarInfo(name)
    info.type = type_
    info.size = len(data) if type_ == tarfile.REGTYPE else 0
    info.linkname = linkname
    info.mode = 0o644
    return info, (data if type_ == tarfile.REGTYPE else None)


@pytest.fixture
def dest(tmp_path: Path) -> str:
    d = tmp_path / "dest"
    d.mkdir()
    return str(d)


def rule_of(archive: Path, dest: str, limits: Limits | None = None) -> str:
    with pytest.raises(UnsafeArchive) as exc:
        safe_extract(str(archive), dest, limits)
    return exc.value.rule


# ── clean archives ───────────────────────────────────────────────────────────────────────────────

def test_clean_zip_extracts_without_exec_bits(tmp_path: Path, dest: str) -> None:
    z = make_zip(tmp_path / "a.zip", {"src/app.py": "print('x')\n", "README.md": "# hi\n", "src/": ""})
    res = safe_extract(str(z), dest)
    assert res.files == 2 and res.skipped == []
    assert (Path(dest) / "src" / "app.py").read_text() == "print('x')\n"
    assert not (os.stat(Path(dest) / "src" / "app.py").st_mode & stat.S_IXUSR)


def test_clean_targz_with_dot_slash_prefix(tmp_path: Path, dest: str) -> None:
    t = make_tar(tmp_path / "a.tar.gz", [tinfo("./", type_=tarfile.DIRTYPE), tinfo("./src/a.py", b"x = 1\n")])
    res = safe_extract(str(t), dest)
    assert res.files == 1 and (Path(dest) / "src" / "a.py").exists()


def test_plain_tar_extracts(tmp_path: Path, dest: str) -> None:
    t = make_tar(tmp_path / "a.tar", [tinfo("a.txt", b"hello\n")], mode="w")
    assert safe_extract(str(t), dest).files == 1


def test_skipped_files_are_recorded_not_extracted(tmp_path: Path, dest: str) -> None:
    z = make_zip(tmp_path / "a.zip", {
        "keep.py": "x = 1\n", ".git/config": "[core]\n", ".env": "K=v\n", ".env.example": "K=\n",
        "keys/id_rsa": "-", "keys/server.pem": "-", "node_modules/pkg/index.js": "1\n",
        "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00", "deep/.venv/lib.py": "1\n",
    })
    res = safe_extract(str(z), dest)
    names = sorted(p.relative_to(dest).as_posix() for p in Path(dest).rglob("*") if p.is_file())
    assert names == [".env.example", "keep.py"]
    reasons = dict(res.skipped)
    assert reasons[".env"] == "secret-file" and reasons["keys/id_rsa"] == "secret-file"
    assert reasons["logo.png"] == "binary" and reasons[".git/config"].startswith("directory:")


# ── hostile zips ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,rule", [
    ("../evil.txt", "path-traversal"),
    ("a/../../evil.txt", "path-traversal"),
    ("/etc/cron.d/x", "absolute-path"),
    ("..\\evil.txt", "backslash"),
    ("a\\b.txt", "backslash"),
    ("C:/Windows/x.txt", "absolute-path"),
    ("bad\x01name.txt", "control-character"),
    # a NUL in a name never reaches us: zipfile truncates the name at the NUL itself
    ("x" * 300 + ".txt", "name-too-long"),
])
def test_hostile_member_names(tmp_path: Path, dest: str, name: str, rule: str) -> None:
    z = make_zip(tmp_path / "a.zip", {"ok.txt": "1\n", name: "boom\n"})
    assert rule_of(z, dest) == rule
    assert not (tmp_path / "evil.txt").exists()


def test_zip_symlink_member(tmp_path: Path, dest: str) -> None:
    p = tmp_path / "a.zip"
    with zipfile.ZipFile(p, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "/etc/passwd")
    assert rule_of(p, dest) == "special-file"


def test_zip_encrypted_member(tmp_path: Path, dest: str) -> None:
    def set_encrypted(data: bytes) -> bytes:
        b = bytearray(data)
        b[6] |= 1  # local header general-purpose flag
        c = bytes(b).rfind(b"PK\x01\x02")
        b[c + 8] |= 1  # central directory flag
        return bytes(b)
    z = make_zip(tmp_path / "a.zip", {"a.txt": "secret\n"}, mutate=set_encrypted)
    assert rule_of(z, dest) == "encrypted"


def test_zip_duplicate_names_ignoring_case(tmp_path: Path, dest: str) -> None:
    z = make_zip(tmp_path / "a.zip", {"Readme.md": "1\n", "README.MD": "2\n"})
    assert rule_of(z, dest) == "duplicate-path"


def test_zip_file_and_directory_share_a_name(tmp_path: Path, dest: str) -> None:
    z = make_zip(tmp_path / "a.zip", {"a": "file\n", "a/b.txt": "x\n"})
    assert rule_of(z, dest) == "path-conflict"


def test_zip_too_many_files(tmp_path: Path, dest: str) -> None:
    z = make_zip(tmp_path / "a.zip", {f"f{i}.txt": "x\n" for i in range(10)})
    assert rule_of(z, dest, Limits(max_files=5)) == "too-many-files"


def test_zip_member_over_the_cap(tmp_path: Path, dest: str) -> None:
    z = make_zip(tmp_path / "a.zip", {"big.txt": "a" * 5000})
    assert rule_of(z, dest, Limits(max_member_bytes=1000)) == "member-too-large"


def test_zip_total_over_the_cap(tmp_path: Path, dest: str) -> None:
    z = make_zip(tmp_path / "a.zip", {f"f{i}.txt": "a" * 600 for i in range(5)})
    assert rule_of(z, dest, Limits(max_total_bytes=2000)) == "archive-too-large"


def test_zip_compression_bomb(tmp_path: Path, dest: str) -> None:
    z = make_zip(tmp_path / "a.zip", {"zeros.txt": "0" * (10 * 1024 * 1024)})
    assert z.stat().st_size < 100_000
    assert rule_of(z, dest) == "compression-bomb"


def test_zip_header_lies_about_the_size(tmp_path: Path, dest: str) -> None:
    def shrink(data: bytes) -> bytes:
        b = bytearray(data)
        # uncompressed size is at offset 22 of the local header and 24 of the central header
        b[22:26] = (3).to_bytes(4, "little")
        c = bytes(b).rfind(b"PK\x01\x02")
        b[c + 24:c + 28] = (3).to_bytes(4, "little")
        return bytes(b)
    z = make_zip(tmp_path / "a.zip", {"a.txt": "a" * 200}, mutate=shrink)
    assert rule_of(z, dest) in ("size-mismatch", "bad-archive")


def test_zip_too_deep(tmp_path: Path, dest: str) -> None:
    z = make_zip(tmp_path / "a.zip", {"/".join(["d"] * 40) + "/f.txt": "x\n"})
    assert rule_of(z, dest) == "too-deep"


def test_unreadable_zip(tmp_path: Path, dest: str) -> None:
    p = tmp_path / "a.zip"
    p.write_bytes(b"PK\x03\x04" + b"\xff" * 50)
    assert rule_of(p, dest) == "bad-archive"


# ── hostile tars ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("entry,rule", [
    (lambda: tinfo("link", type_=tarfile.SYMTYPE, linkname="/etc/passwd"), "special-file"),
    (lambda: tinfo("hard", type_=tarfile.LNKTYPE, linkname="ok.txt"), "special-file"),
    (lambda: tinfo("dev", type_=tarfile.CHRTYPE), "special-file"),
    (lambda: tinfo("fifo", type_=tarfile.FIFOTYPE), "special-file"),
    (lambda: tinfo("../evil.txt", b"x"), "path-traversal"),
    (lambda: tinfo("/abs/evil.txt", b"x"), "absolute-path"),
])
def test_hostile_tar_members(tmp_path: Path, dest: str, entry, rule: str) -> None:
    t = make_tar(tmp_path / "a.tar.gz", [tinfo("ok.txt", b"1\n"), entry()])
    assert rule_of(t, dest) == rule
    assert not (tmp_path / "evil.txt").exists()


def test_tar_member_over_the_cap(tmp_path: Path, dest: str) -> None:
    t = make_tar(tmp_path / "a.tar.gz", [tinfo("big.txt", b"a" * 5000)])
    assert rule_of(t, dest, Limits(max_member_bytes=1000)) == "member-too-large"


def test_tar_gzip_bomb_is_caught_by_the_member_cap(tmp_path: Path, dest: str) -> None:
    t = make_tar(tmp_path / "bomb.tar.gz", [tinfo("zeros.txt", b"0" * (30 * 1024 * 1024))])
    assert t.stat().st_size < 200_000
    assert rule_of(t, dest) == "member-too-large"


def test_tar_duplicate_names(tmp_path: Path, dest: str) -> None:
    t = make_tar(tmp_path / "a.tar.gz", [tinfo("a.txt", b"1"), tinfo("A.TXT", b"2")])
    assert rule_of(t, dest) == "duplicate-path"


# ── the archive itself ───────────────────────────────────────────────────────────────────────────

def test_unknown_format(tmp_path: Path, dest: str) -> None:
    p = tmp_path / "a.rar"
    p.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 600)
    assert rule_of(p, dest) == "unknown-format"


def test_archive_over_the_size_cap(tmp_path: Path, dest: str) -> None:
    z = make_zip(tmp_path / "a.zip", {"a.txt": "x" * 100})
    assert rule_of(z, dest, Limits(max_archive_bytes=50)) == "archive-too-large"


def test_destination_must_be_empty(tmp_path: Path, dest: str) -> None:
    (Path(dest) / "old").write_text("x")
    z = make_zip(tmp_path / "a.zip", {"a.txt": "1\n"})
    assert rule_of(z, dest) == "bad-destination"


def test_check_archive_reports_a_finding_and_cleans_up(tmp_path: Path) -> None:
    z = make_zip(tmp_path / "a.zip", {"../x": "1"})
    findings = check_archive(str(z))
    assert [f.rule for f in findings] == ["path-traversal"]
    assert check_archive(str(make_zip(tmp_path / "b.zip", {"a.txt": "1\n"}))) == []
