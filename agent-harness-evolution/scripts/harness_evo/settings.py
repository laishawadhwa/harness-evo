"""User-level settings: the user's choices, whatever the install method.

Sources, lowest precedence first:
  1. <config dir>/config.yaml        written by hand (pip installs)
  2. <config dir>/plugin-options.json the plugin options last seen by a hook. Claude Code passes plugin
                                      options to hooks only, so this is how the CLI run from the Bash tool
                                      sees them.
  3. CLAUDE_PLUGIN_OPTION_<KEY>      plugin options, set by Claude Code for hook processes
  4. HARNESS_EVO_<KEY>               environment overrides
The config dir is $HARNESS_EVO_CONFIG_DIR, else ${XDG_CONFIG_HOME:-~/.config}/harness-evo.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "strictness": "standard",
    "prompt_mode": "text",
    "retention_days": 30,
    "pii_patterns": [],
    "trace_location": "repo",
    "nudge_ab": False,
}
CHOICES = {"strictness": ("observe", "standard", "strict"), "prompt_mode": ("text", "hash"),
           "trace_location": ("repo", "user")}
_TRUE, _FALSE = {"1", "true", "yes", "on"}, {"0", "false", "no", "off", ""}


def config_dir() -> Path:
    if os.environ.get("HARNESS_EVO_CONFIG_DIR"):
        return Path(os.environ["HARNESS_EVO_CONFIG_DIR"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "harness-evo"


def data_root() -> Path:
    if os.environ.get("HARNESS_EVO_DATA_DIR"):
        return Path(os.environ["HARNESS_EVO_DATA_DIR"])
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "harness-evo"


def repo_id(root: Path | str) -> str:
    resolved = Path(root).resolve()
    return f"{resolved.name or 'root'}-{hashlib.sha256(str(resolved).encode()).hexdigest()[:12]}"


def repo_data_dir(root: Path | str) -> Path:
    return data_root() / "repos" / repo_id(root)


def parse(key: str, raw) -> tuple[object, str | None]:
    """(value, error). Invalid values fall back to the default and say why."""
    default = DEFAULTS[key]
    try:
        if key in CHOICES:
            v = str(raw).strip().lower()
            if v not in CHOICES[key]:
                return default, f"{key}: {raw!r} is not one of {', '.join(CHOICES[key])}; using {default!r}"
            return v, None
        if key == "retention_days":
            v = int(float(raw))
            if not 1 <= v <= 365:
                return default, f"retention_days: {raw!r} is outside 1-365; using {default}"
            return v, None
        if key == "nudge_ab":
            if isinstance(raw, bool):
                return raw, None
            v = str(raw).strip().lower()
            if v in _TRUE | _FALSE:
                return v in _TRUE, None
            return default, f"nudge_ab: {raw!r} is not a boolean; using {default}"
        if key == "pii_patterns":
            if isinstance(raw, list):
                items = raw
            else:
                text = str(raw).strip()
                items = json.loads(text) if text.startswith("[") else text.splitlines()
            import re
            out = []
            for p in items:
                p = str(p).strip()
                if p:
                    re.compile(p)
                    out.append(p)
            return out, None
    except (ValueError, TypeError) as exc:
        return default, f"{key}: {raw!r} is invalid ({exc}); using the default"
    except Exception as exc:  # re.error and friends
        return default, f"{key}: {raw!r} is invalid ({exc}); using the default"
    return default, None


@dataclass
class Settings:
    effective: dict
    sources: dict
    errors: list = field(default_factory=list)
    plugin: dict = field(default_factory=dict)      # live CLAUDE_PLUGIN_OPTION_* values, parsed

    def explicit(self, key: str) -> bool:
        return self.sources.get(key) != "default"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_yaml(path: Path) -> dict:
    """config.yaml, parsed once per change: the JSON copy keeps PyYAML off the hook hot path."""
    stamp = _stamp(path)
    if stamp is None:
        return {}
    cache = path.with_name(".config.cache.json")
    cached = _read_json(cache)
    if cached.get("stamp") == list(stamp):
        return cached.get("data") or {}
    try:
        from ._yaml import yaml
        data = yaml.safe_load(path.read_text()) or {}
        data = data if isinstance(data, dict) else {}
    except Exception:
        return {}
    try:
        tmp = cache.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"stamp": list(stamp), "data": data}))
        os.replace(tmp, cache)
    except OSError:
        pass
    return data


def _stamp(path: Path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


_MEMO: dict = {}


def load() -> Settings:
    cdir = config_dir()
    env_keys = [f"{p}{k.upper()}" for k in DEFAULTS for p in ("CLAUDE_PLUGIN_OPTION_", "HARNESS_EVO_")]
    key = (str(cdir), tuple(os.environ.get(k) for k in env_keys),
           _stamp(cdir / "config.yaml"), _stamp(cdir / "plugin-options.json"))
    if key in _MEMO:
        return _MEMO[key]
    effective, sources, errors, plugin = dict(DEFAULTS), {k: "default" for k in DEFAULTS}, [], {}
    layers = [("config.yaml", _read_yaml(cdir / "config.yaml")),
              ("plugin-options.json", _read_json(cdir / "plugin-options.json"))]
    live = {k: os.environ[f"CLAUDE_PLUGIN_OPTION_{k.upper()}"] for k in DEFAULTS
            if f"CLAUDE_PLUGIN_OPTION_{k.upper()}" in os.environ}
    env = {k: os.environ[f"HARNESS_EVO_{k.upper()}"] for k in DEFAULTS if f"HARNESS_EVO_{k.upper()}" in os.environ}
    layers += [("plugin", live), ("env", env)]
    for source, values in layers:
        for k, raw in values.items():
            if k not in DEFAULTS:
                continue
            value, err = parse(k, raw)
            if err:
                errors.append(f"{source}: {err}")
                continue
            effective[k], sources[k] = value, source
            if source == "plugin":
                plugin[k] = value
    s = Settings(effective, sources, errors, plugin)
    if len(_MEMO) > 64:
        _MEMO.clear()
    _MEMO[key] = s
    return s


def persist_plugin_options(s: Settings | None = None) -> bool:
    """Save the live plugin options for the CLI. Writes only when they changed."""
    s = s or load()
    if not s.plugin:
        return False
    path = config_dir() / "plugin-options.json"
    if _read_json(path) == s.plugin:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(s.plugin, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return True


def cmd_settings(args, out):
    s = load()
    report = {"effective": s.effective, "sources": s.sources, "errors": s.errors,
              "config_dir": str(config_dir()), "data_root": str(data_root())}
    if getattr(args, "json", False):
        out.write(json.dumps(report, indent=2) + "\n")
    else:
        for k, v in s.effective.items():
            out.write(f"{k:<15} {json.dumps(v):<22} ({s.sources[k]})\n")
        for e in s.errors:
            out.write(f"warning: {e}\n")
        out.write(f"config dir: {config_dir()}\ndata dir:   {data_root()}\n")
    return 0


HERMETIC_ENV_KEYS = tuple(f"HARNESS_EVO_{k.upper()}" for k in DEFAULTS)


def hermetic_env(config_dir_path: Path | str) -> dict:
    """Environment that ignores the user's own settings: eval trials must not depend on who runs them."""
    return {"HARNESS_EVO_CONFIG_DIR": str(config_dir_path), "HARNESS_EVO_TRACE_LOCATION": "repo"}


@contextlib.contextmanager
def hermetic(config_dir_path: Path | str):
    """Run in-process hooks (e.g. a scripted eval agent) with the user's settings switched off."""
    saved = dict(os.environ)
    try:
        for var in list(os.environ):
            if var.startswith("CLAUDE_PLUGIN_OPTION_") or var in HERMETIC_ENV_KEYS:
                del os.environ[var]
        os.environ.update(hermetic_env(config_dir_path))
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
