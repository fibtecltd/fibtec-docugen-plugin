"""``python3 -P -m docugen.box -- claude ARGS... -p PROMPT``: see ``supervise``."""

import sys

from .supervise import main

raise SystemExit(main(sys.argv[1:]))
