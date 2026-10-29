"""Repo profile and nudge policy: loading, merging, caching and glob matching.

The merged configuration is cached as JSON under .harness-evo/cache/, keyed by
the mtimes of its source files, so the hook hot path never imports PyYAML.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .paths import Layout, skill_file

LEVELS = ("info", "advise", "gate", "deny")
CONFIG_CACHE_VERSION = 5
# The agent may not edit the harness that watches it. Built in, so a profile edit cannot remove it.
HARNESS_CONFIG = (".harness-evo/**", ".claude/settings.json", ".claude/settings.local.json",
                  "~/.claude/settings.json", ".git/hooks/**", "~/.config/harness-evo/**",
                  "~/.local/share/harness-evo/**")
_SECRET_EXEMPT = (".example", ".sample", ".template", ".dist", ".defaults")


# ---------------------------------------------------------------- globbing

@lru_cache(maxsize=2048)
def _glob_re(pattern: str) -> re.Pattern:
    """gitignore-flavoured glob: `**` spans directories, `*` does not, `{a,b}` alternates.
    A pattern without a slash matches the basename at any depth."""
    if "/" not in pattern.rstrip("/"):
        pattern = "**/" + pattern
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "{":
            end = pattern.find("}", i)
            if end == -1:
                out.append(re.escape(c))
            else:
                alts = pattern[i + 1:end].split(",")
                out.append("(?:" + "|".join(re.escape(a) for a in alts) + ")")
                i = end + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(path: str, patterns) -> bool:
    if path.startswith("./"):
        path = path[2:]
    return any(_glob_re(p).match(path) for p in patterns or ())


def glob_prefix(pattern: str) -> str:
    """The literal directory part of a glob: 'tests/billing/**' -> 'tests/billing'."""
    parts = []
    for part in pattern.split("/"):
        if any(ch in part for ch in "*?{["):
            break
        parts.append(part)
    return "/".join(parts)


# ---------------------------------------------------------------- merging

def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    from ._yaml import yaml  # imported lazily: only on a cache miss
    data = yaml.safe_load(path.read_text()) or {}
    return data if isinstance(data, dict) else {}


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _stamp(paths) -> list:
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append([str(p), st.st_mtime_ns, st.st_size])
        except OSError:
            out.append([str(p), 0, 0])
    return out


# ---------------------------------------------------------------- profile

@dataclass
class TestGroup:
    name: str
    source: list
    tests: list
    command: str | None = None
    depends_on: list = field(default_factory=list)

    @property
    def test_dirs(self) -> list[str]:
        return [glob_prefix(t) or t for t in self.tests]


class Profile:
    def __init__(self, data: dict, protected_dirs=()):
        self.data = data
        self.protected_dirs = list(protected_dirs)  # absolute harness-evo dirs outside the repo (D7)
        g = data.get
        self.code_globs = g("code_globs") or []
        self.test_globs = g("test_globs") or []
        self.risky_paths = g("risky_paths") or []
        self.protected_paths = g("protected_paths") or []
        self.secret_paths = g("secret_paths") or []
        self.generated_globs = g("generated_globs") or []
        self.harness_paths = g("harness_paths") or []
        self.test_commands = [re.compile(p) for p in g("test_commands") or []]
        self.test_map = [TestGroup(name=t.get("name", f"group{i}"), source=t.get("source") or [],
                                   tests=t.get("tests") or [], command=t.get("command"),
                                   depends_on=t.get("depends_on") or [])
                         for i, t in enumerate(g("test_map") or [])]
        self.lint_commands = g("lint_commands") or []
        self.typecheck_commands = g("typecheck_commands") or []
        self.thresholds = g("thresholds") or {}
        self.task_type_thresholds = g("task_type_thresholds") or {}
        destructive = g("destructive") or {}
        self.destructive_allow = destructive.get("allow") or []
        self.destructive_extra = [re.compile(p, re.I) for p in destructive.get("extra_patterns") or []]
        claims = g("claims") or {}
        self.claim_test_patterns = [re.compile(p, re.I) for p in claims.get("test_patterns") or []]
        self.claim_patterns = [re.compile(p, re.I) for p in claims.get("patterns") or []]
        privacy = g("privacy") or {}
        self.prompt_mode = privacy.get("prompt_mode", "text")
        self.retention_days = privacy.get("retention_days", 30)
        self.pii_patterns = privacy.get("pii_patterns") or []
        self.queue_eval_on_end = bool(g("queue_eval_on_end"))

    # path classes
    def is_code(self, path: str) -> bool:
        return glob_match(path, self.code_globs)

    def is_test(self, path: str) -> bool:
        return glob_match(path, self.test_globs)

    def is_risky(self, path: str) -> bool:
        return glob_match(path, self.risky_paths)

    def is_protected(self, path: str) -> bool:
        return glob_match(path, self.protected_paths)

    def is_secret(self, path: str) -> bool:
        return glob_match(path, self.secret_paths) and not path.endswith(_SECRET_EXEMPT)

    def is_harness_config(self, path: str) -> bool:
        if glob_match(path, HARNESS_CONFIG):
            return True
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            return False
        p = os.path.realpath(p)
        return any(p == d or p.startswith(d + os.sep) for d in self.protected_dirs)

    def is_generated(self, path: str) -> bool:
        return glob_match(path, self.generated_globs)

    def is_harness(self, path: str) -> bool:
        return glob_match(path, self.harness_paths)

    def is_test_command(self, command: str) -> bool:
        return any(p.search(command) for p in self.test_commands)

    # test map
    def groups_for(self, path: str) -> list[TestGroup]:
        return [t for t in self.test_map if glob_match(path, t.source) or glob_match(path, t.tests)]

    def group(self, name: str) -> TestGroup | None:
        return next((t for t in self.test_map if t.name == name), None)

    def impacted_groups(self, paths) -> list[TestGroup]:
        """Groups for the changed paths plus the groups that depend on them."""
        hit = {g.name for p in paths for g in self.groups_for(p)}
        changed = True
        while changed:
            changed = False
            for t in self.test_map:
                if t.name not in hit and any(d in hit for d in t.depends_on):
                    hit.add(t.name)
                    changed = True
        return [t for t in self.test_map if t.name in hit]

    def test_hint(self, paths=None) -> str:
        groups = self.impacted_groups(paths) if paths else self.test_map
        dirs = [d for g in groups for d in g.test_dirs]
        return ", ".join(dirs[:3]) if dirs else "the tests that cover it"

    def command_hint(self, paths=None) -> str:
        groups = self.impacted_groups(paths) if paths else self.test_map
        cmds = [g.command for g in groups if g.command]
        if cmds:
            return " and ".join(f"`{c}`" for c in cmds[:2])
        return self.test_hint(paths)

    def threshold(self, detector: str, key: str, task_type: str | None = None, default=None):
        if task_type:
            v = ((self.task_type_thresholds.get(task_type) or {}).get(detector) or {}).get(key)
            if v is not None:
                return v
        v = (self.thresholds.get(detector) or {}).get(key)
        return default if v is None else v


# ---------------------------------------------------------------- policy

class Policy:
    def __init__(self, data: dict, state: dict | None = None, mutes: dict | None = None):
        self.data = data
        self.state = state or {}
        self.mutes = mutes or {}
        caps = data.get("caps") or {}
        self.per_turn = int(caps.get("per_turn", 1))
        self.per_session = int(caps.get("per_session", 5))
        self.adherence_window = int(data.get("adherence_window", 3))
        self.nudge_ab = bool((data.get("experiment") or {}).get("nudge_ab"))
        wording = data.get("wording") or {}
        self.prefix = wording.get("prefix", "Harness note:")
        self.contract = wording.get("contract") or []
        self.templates = wording.get("templates") or {}

    def detector(self, det: str) -> dict:
        return (self.data.get("detectors") or {}).get(det) or {}

    def level(self, det: str) -> str:
        overlay = (self.state.get("levels") or {}).get(det)
        return overlay or self.detector(det).get("level", "info")

    def target_level(self, det: str) -> str:
        return self.detector(det).get("target_level", self.detector(det).get("level", "info"))

    def is_muted(self, det: str, session: str | None = None) -> bool:
        if det in (self.mutes.get("repo") or []):
            return True
        return bool(session) and det in ((self.mutes.get("sessions") or {}).get(session) or [])

    def render(self, template: str, params: dict) -> str:
        text = self.templates.get(template)
        if text is None:
            text = "{detail}"
        clean = {k: _clean(v) for k, v in params.items()}
        try:
            body = text.format(**clean)
        except (KeyError, IndexError, ValueError):
            body = text
        return f"{self.prefix} {body}"


def _clean(v) -> str:
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v)
    s = " ".join(str(v).split())
    return s if len(s) <= 120 else s[:117] + "..."


# ---------------------------------------------------------------- loading

def load_at(repo: Path | str, ref: str) -> "Config":
    """The repo's harness config as committed at `ref`, e.g. a PR's base, so a PR cannot loosen its own gates."""
    import shutil
    import subprocess
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="harness-evo-cfg-"))
    try:
        (tmp / ".harness-evo").mkdir()
        for name in ("profile.yaml", "nudge-policy.yaml", "policy-state.json"):
            r = subprocess.run(["git", "show", f"{ref}:.harness-evo/{name}"], cwd=repo, capture_output=True, text=True)
            if r.returncode == 0:
                (tmp / ".harness-evo" / name).write_text(r.stdout)
        return load(tmp, mode="repo")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@dataclass
class Config:
    profile: Profile
    policy: Policy
    digest: str = ""   # identifies the harness configuration in traces and baselines


def preset_layer(strictness: str, policy: dict) -> dict:
    """observe: everything logged only. standard: the shipped levels. strict: every detector at its target."""
    detectors = policy.get("detectors") or {}
    if strictness == "observe":
        return {"detectors": {d: {"level": "info"} for d in detectors}}
    if strictness == "strict":
        return {"detectors": {d: {"level": v.get("target_level", v.get("level", "info"))} for d, v in detectors.items()}}
    return {}


def combine_privacy(profile: dict, user) -> dict:
    """Privacy settings combine to the stricter value: hashing wins, the shorter retention wins, and PII
    patterns are combined. Only values the user actually set take part."""
    privacy = dict(profile.get("privacy") or {})
    if user.explicit("prompt_mode") and user.effective["prompt_mode"] == "hash":
        privacy["prompt_mode"] = "hash"
    if user.explicit("retention_days"):
        privacy["retention_days"] = min(int(privacy.get("retention_days", 30)), user.effective["retention_days"])
    if user.explicit("pii_patterns"):
        privacy["pii_patterns"] = list(dict.fromkeys(list(privacy.get("pii_patterns") or [])
                                                     + list(user.effective["pii_patterns"])))
    return {**profile, "privacy": privacy}


def load(root: Path | str, mode: str | None = None) -> Config:
    from . import settings as user_settings
    user = user_settings.load()
    layout = Layout(root, mode)
    sources = [skill_file("config", "repo-profile.template.yaml"), layout.profile,
               skill_file("config", "nudge-policy.yaml"), layout.policy, layout.policy_state, layout.mutes]
    # JSON-stable values only (no tuples), or the cached stamp never compares equal and every call misses
    stamp = [CONFIG_CACHE_VERSION, _stamp(sources), user.effective, dict(sorted(user.sources.items()))]
    cache = layout.cache / "config.json"
    data = None
    try:
        cached = json.loads(cache.read_text())
        if cached.get("stamp") == stamp:
            data = cached
    except (OSError, ValueError):
        pass
    if data is None:
        data = {
            "stamp": stamp,
            "profile": combine_privacy(deep_merge(_load_yaml(sources[0]), _load_yaml(sources[1])), user),
            "policy": _policy(_load_yaml(sources[2]), _load_yaml(sources[3]), user),
            "policy_state": _load_json(sources[4]),
            "mutes": _load_json(sources[5]),
        }
        import hashlib
        identity = json.dumps([data["profile"], data["policy"], data["policy_state"].get("levels")], sort_keys=True)
        data["digest"] = hashlib.sha256(identity.encode()).hexdigest()[:12]
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, cache)
        except OSError:
            pass
    protected = [os.path.realpath(str(d)) for d in {user_settings.config_dir(), user_settings.data_root(), layout.base}
                 if layout.mode == "user" or d != layout.base]
    return Config(Profile(data["profile"], protected), Policy(data["policy"], data.get("policy_state"), data.get("mutes")),
                  data.get("digest", ""))


def _policy(defaults: dict, repo: dict, user) -> dict:
    """defaults < strictness preset < the user's policy settings < the repo's nudge-policy.yaml."""
    merged = deep_merge(defaults, preset_layer(user.effective["strictness"], defaults))
    if user.explicit("nudge_ab"):
        merged = deep_merge(merged, {"experiment": {"nudge_ab": user.effective["nudge_ab"]}})
    return deep_merge(merged, repo)
