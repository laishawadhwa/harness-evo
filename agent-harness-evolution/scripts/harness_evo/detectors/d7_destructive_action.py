"""D7 destructive-action: destructive commands and writes to protected or secret paths (deny)."""
from __future__ import annotations

from ..trace import shell
from .base import Detector


def destructive_in(e: dict, p) -> list[str]:
    payload, d = e.get("payload") or {}, e.get("derived") or {}
    hits = []
    if payload.get("tool") == "Bash":
        if "destructive" in d:  # computed by the adapter from the full, untruncated command
            hits += d["destructive"]
        else:
            hits += shell.destructive_matches((payload.get("args") or {}).get("command") or "",
                                              p.destructive_allow, p.destructive_extra)
    for w in d.get("writes") or []:
        if p.is_harness_config(w["path"]):
            hits.append(f"Changing harness configuration ({w['path']})")
        elif p.is_protected(w["path"]):
            hits.append(f"Writing to protected path {w['path']}")
        elif p.is_secret(w["path"]):
            hits.append(f"Writing to secret file {w['path']}")
    for path in d.get("deletes") or []:
        if p.is_harness_config(path) or p.is_protected(path) or p.is_secret(path):
            hits.append(f"Deleting {path}")
    return hits


class DestructiveAction(Detector):
    id, name, on = "D7", "destructive-action", ("tool_call",)
    summary = "rm -rf, git reset --hard, force push, dropping tables, writes to protected paths or secrets."

    def check(self, e, s, p):
        hits = destructive_in(e, p)
        if not hits:
            return []
        first = hits[0]
        literal = first.startswith(("rm ", "git ", "find ", "pattern ", "harness-evo ", "mkfs", "dd "))
        what = f"`{first}`" if literal else first[0].upper() + first[1:]
        # Denies are never de-duplicated: every attempt is blocked.
        return [self.finding(e, hits[0], f"seq:{e['seq']}", "D7.blocked", {"what": what}, matches=hits)]

    def complied(self, nudge, after):
        for a in after:
            if a["type"] == "tool_call" and (a.get("payload") or {}).get("tool") == "Bash":
                derived = a.get("derived") or {}
                if "destructive" in derived:  # judged with the repo's allow-list at the time
                    hits = derived["destructive"]
                else:
                    hits = shell.destructive_matches(((a.get("payload") or {}).get("args") or {}).get("command") or "")
                if hits:
                    return False
        return True
