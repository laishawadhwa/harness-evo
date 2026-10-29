"""Every test gets its own harness-evo user config and data dirs, so a developer's real settings
(~/.config/harness-evo, plugin options) can never change a result or receive test data.

The session-wide defaults are set at import time, before any fixture of any scope runs; the
autouse fixture then gives each test fresh directories of its own."""
import os
import tempfile

import pytest

_SETTINGS_ENV = ("HARNESS_EVO_STRICTNESS", "HARNESS_EVO_PROMPT_MODE", "HARNESS_EVO_RETENTION_DAYS",
                 "HARNESS_EVO_PII_PATTERNS", "HARNESS_EVO_TRACE_LOCATION", "HARNESS_EVO_NUDGE_AB",
                 "HARNESS_EVO_MODEL_PROVIDER")
for _var in list(os.environ):
    if _var.startswith("CLAUDE_PLUGIN_OPTION_") or _var in _SETTINGS_ENV:
        del os.environ[_var]
_SESSION_BASE = tempfile.mkdtemp(prefix="harness-evo-tests-")
os.environ["HARNESS_EVO_CONFIG_DIR"] = os.path.join(_SESSION_BASE, "config")
os.environ["HARNESS_EVO_DATA_DIR"] = os.path.join(_SESSION_BASE, "data")


@pytest.fixture(autouse=True)
def _isolated_harness_evo_user(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("harness-evo-user")
    monkeypatch.setenv("HARNESS_EVO_CONFIG_DIR", str(base / "config"))
    monkeypatch.setenv("HARNESS_EVO_DATA_DIR", str(base / "data"))
