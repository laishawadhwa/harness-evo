"""User settings: parsing, precedence, persistence, and the config cache that depends on them."""
from __future__ import annotations

import json

import pytest

from harness_evo import config, settings


@pytest.mark.parametrize("key,raw,value,ok", [
    ("strictness", "STRICT", "strict", True), ("strictness", "loud", "standard", False),
    ("prompt_mode", "hash", "hash", True), ("trace_location", "user", "user", True),
    ("retention_days", "7", 7, True), ("retention_days", "0", 30, False), ("retention_days", "x", 30, False),
    ("nudge_ab", "yes", True, True), ("nudge_ab", False, False, True), ("nudge_ab", "maybe", False, False),
    ("pii_patterns", '["a\\\\d+", "b"]', ["a\\d+", "b"], True), ("pii_patterns", "x\ny\n", ["x", "y"], True),
    ("pii_patterns", "[unclosed", [], False),
])
def test_parse(key, raw, value, ok):
    v, err = settings.parse(key, raw)
    assert v == value and (err is None) is ok


def test_precedence_config_file_then_persisted_then_plugin_then_env(tmp_path, monkeypatch):
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    monkeypatch.setenv("HARNESS_EVO_CONFIG_DIR", str(cdir))
    (cdir / "config.yaml").write_text("strictness: observe\nretention_days: 20\nprompt_mode: hash\n")
    (cdir / "plugin-options.json").write_text(json.dumps({"retention_days": 10}))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STRICTNESS", "strict")
    monkeypatch.setenv("HARNESS_EVO_PROMPT_MODE", "text")
    s = settings.load()
    assert s.effective["strictness"] == "strict" and s.sources["strictness"] == "plugin"
    assert s.effective["retention_days"] == 10 and s.sources["retention_days"] == "plugin-options.json"
    assert s.effective["prompt_mode"] == "text" and s.sources["prompt_mode"] == "env"
    assert s.sources["trace_location"] == "default" and not s.explicit("trace_location")


def test_persist_writes_only_live_plugin_options_and_only_on_change(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_EVO_CONFIG_DIR", str(tmp_path))
    assert settings.persist_plugin_options() is False  # nothing live
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_NUDGE_AB", "true")
    assert settings.persist_plugin_options() is True
    assert json.loads((tmp_path / "plugin-options.json").read_text()) == {"nudge_ab": True}
    assert settings.persist_plugin_options() is False  # unchanged


def test_repo_ids_are_stable_and_distinct(tmp_path):
    a, b = tmp_path / "a" / "repo", tmp_path / "b" / "repo"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert settings.repo_id(a) == settings.repo_id(a)
    assert settings.repo_id(a) != settings.repo_id(b) and settings.repo_id(a).startswith("repo-")


def test_config_cache_hits_with_explicit_user_settings(tmp_path, monkeypatch):
    """Regression: the cache stamp must survive a JSON round trip, or every hook call re-parses YAML."""
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STRICTNESS", "strict")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_PII_PATTERNS", '["x\\\\d+"]')
    root = tmp_path / "repo"
    (root / ".harness-evo").mkdir(parents=True)
    config.load(root)

    def boom(path):
        raise AssertionError(f"cache miss: re-parsed {path}")

    monkeypatch.setattr(config, "_load_yaml", boom)
    cfg = config.load(root)
    assert cfg.policy.level("D1") == "advise"  # the strict preset, served from the cache


def test_strict_preset_respects_target_levels():
    policy = {"detectors": {"D1": {"level": "info", "target_level": "advise"}, "D7": {"level": "deny"}}}
    assert config.preset_layer("strict", policy) == {"detectors": {"D1": {"level": "advise"}, "D7": {"level": "deny"}}}
    assert config.preset_layer("observe", policy) == {"detectors": {"D1": {"level": "info"}, "D7": {"level": "info"}}}
    assert config.preset_layer("standard", policy) == {}


def test_config_yaml_is_parsed_once_per_change(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_EVO_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text("strictness: observe\n")
    assert settings.load().effective["strictness"] == "observe"
    settings._MEMO.clear()
    import harness_evo._yaml as y

    def boom(*a, **k):
        raise AssertionError("re-parsed an unchanged config.yaml")

    monkeypatch.setattr(y.yaml, "safe_load", boom)
    assert settings.load().effective["strictness"] == "observe"
