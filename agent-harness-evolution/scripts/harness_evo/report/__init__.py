"""Report rendering: the PR comment and the suite run report, from templates in reports/."""
from __future__ import annotations

import json
from pathlib import Path


def cmd(args, out):
    if args.kind == "pr":
        from .pr import render
        text = render(json.loads(Path(args.source).read_text()))
    else:
        from .run import render
        text = render(Path(args.source))
    if args.out:
        Path(args.out).write_text(text)
    else:
        out.write(text)
    return 0
