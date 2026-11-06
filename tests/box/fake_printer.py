"""A stand-in for ``docugen-pdf`` in the box tests.

The real printer is a Node program in the box image (Chromium, a Markdown renderer). The supervisor gives it an empty
environment, so a stand-in cannot be steered with environment variables: ``make_printer`` writes a script with its
behaviour built in, and the script records what it was given in a JSON file whose path is built in too.

Behaviours:

ok          writes every PDF it was asked for
fail        exits 3 and writes nothing
sleep       sleeps for a minute (a printer that hangs)
partial     writes all but the last PDF it was asked for
hostile     writes an ordinary PDF for all but the last, and one that runs JavaScript for the last
symlink     leaves a symbolic link where the last PDF should be
extra       writes every PDF it was asked for, and files it was not asked for
notpdf      writes text that is not a PDF
big         writes a PDF of about 2 MiB
"""

from __future__ import annotations

import base64
import stat
import sys
from pathlib import Path

from tests.verify.test_pdf_check import page_tree, pdf

GOOD = pdf(*page_tree())
HOSTILE = pdf(*page_tree(b"/OpenAction << /S /JavaScript /JS (app.alert(1)) >> "))
BIG = pdf(*page_tree(), b"%" + b"p" * (2 * 1024 * 1024) + b"\n")

SCRIPT = '''#!{python}
import base64, json, os, sys, time

args = sys.argv[1:]


def values(flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]


out = values("--out")[0]
names = values("--combined") + [value.split("=", 1)[1] for value in values("--single")]
with open({record!r}, "w") as fh:
    json.dump({{"args": args, "env": sorted(os.environ), "cwd": os.getcwd(), "pid": os.getpid(), "pgid_is_own": os.getpgrp() == os.getpid(),
               "in_listing": sorted(os.path.relpath(os.path.join(d, f), values("--in")[0]) for d, _, fs in os.walk(values("--in")[0]) for f in fs)}}, fh)
good = base64.b64decode({good!r})
hostile = base64.b64decode({hostile!r})
big = base64.b64decode({big!r})
behaviour = {behaviour!r}
if behaviour == "fail":
    sys.exit(3)
if behaviour == "sleep":
    time.sleep(60)
for position, name in enumerate(names):
    path = os.path.join(out, name)
    last = position == len(names) - 1
    if behaviour == "partial" and last:
        continue
    if behaviour == "symlink" and last:
        os.symlink("/etc/passwd", path)
        continue
    if behaviour == "notpdf":
        data = b"<html>not a pdf</html>"
    elif behaviour == "hostile" and last:
        data = hostile
    elif behaviour == "big":
        data = big
    else:
        data = good
    with open(path, "wb") as fh:
        fh.write(data)
if behaviour == "extra":
    for extra in ("notes.txt", "evil.pdf"):
        with open(os.path.join(out, extra), "wb") as fh:
            fh.write(good)
sys.exit(0)
'''


def make_printer(directory: Path, behaviour: str = "ok") -> tuple[str, Path]:
    """Write the stand-in into ``directory``; return its path and the path of the JSON record it will leave."""
    record = directory / f"printer-{behaviour}.json"
    path = directory / f"docugen-pdf-{behaviour}"
    path.write_text(SCRIPT.format(python=sys.executable, record=str(record), behaviour=behaviour,
                                  good=base64.b64encode(GOOD).decode(), hostile=base64.b64encode(HOSTILE).decode(),
                                  big=base64.b64encode(BIG).decode()))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path), record
