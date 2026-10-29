"""Which changed files a test run covers, through the repo profile's test-to-module map.

D3, D4 and D5 count only relevant runs, so `pytest tests/test_trivial.py` cannot
satisfy the verification gate for a change in src/billing/.
"""
from __future__ import annotations

import posixpath
import re

from ..config import Profile, glob_match, glob_prefix


def _target_hits(target: str, globs) -> bool:
    if glob_match(target, globs):
        return True
    for g in globs:
        prefix = glob_prefix(g)
        if not prefix:
            continue
        # `pytest tests/billing` for tests/billing/**, or `pytest tests` (a parent directory)
        if target == prefix or prefix.startswith(target.rstrip("/") + "/") or target in {".", ""}:
            return True
    return False


def _stem(path: str) -> str:
    base = posixpath.basename(path)
    return base.split(".", 1)[0]


def covers(targets: list[str], filters: list[str], paths, profile: Profile) -> list[str]:
    paths = list(paths)
    if not filters and (not targets or any(t in {".", ""} for t in targets)):
        return paths  # the whole suite, or everything under the repo root
    out = []
    for path in paths:
        groups = profile.groups_for(path)
        hit = False
        for g in groups:
            if any(_target_hits(t, g.tests) for t in targets):
                hit = True
            elif any(re.search(rf"\b{re.escape(g.name)}\b", f, re.I) for f in filters):
                hit = True
        if not hit and profile.is_test(path):
            hit = any(path == t or path.startswith(t.rstrip("/") + "/") or t in {".", ""} for t in targets)
        if not hit and not groups:
            stem, parent = _stem(path), posixpath.basename(posixpath.dirname(path))
            # a directory target named like the file's parent (`pytest tests/util` for src/util/x.py)
            hit = any(stem and stem in t or (parent and "." not in t.rsplit("/", 1)[-1] and parent in t.split("/"))
                      for t in targets) or any(stem and stem in f for f in filters)
        if hit:
            out.append(path)
    return out
