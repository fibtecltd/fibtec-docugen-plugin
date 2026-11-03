"""``python3 -P -m docugen.box -- claude ARGS... -p PROMPT`` (see ``supervise``) or, for a whole session,
``python3 -P -m docugen.box --session -- claude ARGS... -p PROMPT`` (see ``session``)."""

import sys

if sys.argv[1:2] == ["--session"]:
    from .session import main as session_main

    raise SystemExit(session_main(sys.argv[2:]))

from .supervise import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
