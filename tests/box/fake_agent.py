"""A stand-in for ``claude`` in the supervisor tests. It replays a recorded event stream and misbehaves on request.

Configured through environment variables so the supervisor's own command line stays the real one:

FAKE_STREAM   file whose bytes are written to standard output
FAKE_EXIT     the exit code (default 0)
FAKE_STDERR   text written to standard error
FAKE_RECORD   file that receives a JSON record of argv, environment names, whether stdin is empty
FAKE_SLEEP    seconds to sleep after the stream (a session that does not finish)
FAKE_BACKGROUND  start a background process that keeps standard output open for this many seconds
FAKE_OUT_DIR  where FAKE_WRITE and FAKE_SYMLINKS put their files (the real agent works in the output directory)
FAKE_WRITE    JSON object of relative path to text: files the session writes
FAKE_SYMLINKS JSON object of relative path to target: symbolic links the session leaves behind
FAKE_PROBE    JSON object {"src": dir, "scratch": dir}: what the agent tries to touch, recorded in FAKE_RECORD under "probe"
"""

import json
import os
import subprocess
import sys
import time


def attempt(call) -> str:  # noqa: ANN001
    try:
        call()
    except PermissionError:
        return "denied"
    except OSError as exc:
        return f"error:{exc.errno}"
    return "allowed"


def probe() -> dict:
    """What this process can reach: its parent's environment, a write into the source, the supervisor's scratch directory."""
    targets = json.loads(os.environ.get("FAKE_PROBE", "{}"))
    out = {"parent_environ": attempt(lambda: open(f"/proc/{os.getppid()}/environ", "rb").read(1))}
    if "src" in targets:
        out["write_src"] = attempt(lambda: open(os.path.join(targets["src"], "planted.txt"), "w").close())
    if "scratch" in targets:
        out["list_scratch"] = attempt(lambda: os.listdir(targets["scratch"]))
    return out


def main() -> int:
    record = os.environ.get("FAKE_RECORD")
    if record:
        with open(record, "w") as fh:
            json.dump({"argv": sys.argv[1:], "env": sorted(os.environ), "stdin": sys.stdin.read(),
                       "pgid_is_own": os.getpgrp() == os.getpid(), "uid": os.getuid(), "gid": os.getgid(),
                       "groups": os.getgroups(), "home": os.environ.get("HOME"), "probe": probe()}, fh)
    root = os.environ.get("FAKE_OUT_DIR", "")
    for rel, text in json.loads(os.environ.get("FAKE_WRITE", "{}")).items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
    for rel, target in json.loads(os.environ.get("FAKE_SYMLINKS", "{}")).items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
    if os.environ.get("FAKE_STDERR"):
        sys.stderr.write(os.environ["FAKE_STDERR"])
        sys.stderr.flush()
    if os.environ.get("FAKE_BACKGROUND"):
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%s)" % os.environ["FAKE_BACKGROUND"]],
                         stdin=subprocess.DEVNULL)
    stream = os.environ.get("FAKE_STREAM")
    if stream:
        with open(stream, "rb") as fh:
            sys.stdout.buffer.write(fh.read())
            sys.stdout.buffer.flush()
    if os.environ.get("FAKE_SLEEP"):
        time.sleep(float(os.environ["FAKE_SLEEP"]))
    return int(os.environ.get("FAKE_EXIT", "0"))


if __name__ == "__main__":
    raise SystemExit(main())
