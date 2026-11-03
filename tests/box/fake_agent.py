"""A stand-in for ``claude`` in the supervisor tests. It replays a recorded event stream and misbehaves on request.

Configured through environment variables so the supervisor's own command line stays the real one:

FAKE_STREAM   file whose bytes are written to standard output
FAKE_EXIT     the exit code (default 0)
FAKE_STDERR   text written to standard error
FAKE_RECORD   file that receives a JSON record of argv, environment names, whether stdin is empty
FAKE_SLEEP    seconds to sleep after the stream (a session that does not finish)
FAKE_BACKGROUND  start a background process that keeps standard output open for this many seconds
"""

import json
import os
import subprocess
import sys
import time


def main() -> int:
    record = os.environ.get("FAKE_RECORD")
    if record:
        with open(record, "w") as fh:
            json.dump({"argv": sys.argv[1:], "env": sorted(os.environ), "stdin": sys.stdin.read(),
                       "pgid_is_own": os.getpgrp() == os.getpid()}, fh)
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
