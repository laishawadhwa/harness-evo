"""summary.py: transcript token usage, summaries on disk, and the session-end summary cost."""
from __future__ import annotations

import json

import pytest

from harness_evo import summary
from unit_helpers import ClaudeCode


def entry(model, i, o, cr=0, cw=0, kind="assistant"):
    return {"type": kind, "message": {"role": "assistant", "model": model,
                                      "usage": {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,
                                                "cache_creation_input_tokens": cw}}}


def write_transcript(path, lines):
    path.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n")
    return str(path)


def test_transcript_usage_sums_tokens_by_model(tmp_path):
    path = write_transcript(tmp_path / "t.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        entry("claude-opus-5-5", 1000, 200, cr=10_000, cw=500),
        entry("claude-opus-5-5", 500, 100),
        entry("claude-haiku-5-5", 2000, 50),
        {"type": "assistant", "message": {"usage": {"input_tokens": 7, "output_tokens": None}}},   # no model
        "not json at all",
        "[1, 2, 3]",
        {"type": "summary", "message": "text"},
        {"message": {"usage": "bad"}},
    ])
    u = summary.transcript_usage(path)
    assert u["input_tokens"] == 3507 and u["output_tokens"] == 350
    assert u["cache_read_input_tokens"] == 10_000 and u["cache_creation_input_tokens"] == 500
    assert u["by_model"]["claude-opus-5-5"] == {"input_tokens": 1500, "output_tokens": 300,
                                               "cache_read_input_tokens": 10_000, "cache_creation_input_tokens": 500}
    assert u["by_model"]["unknown"]["input_tokens"] == 7
    opus = (1500 * 4 + 300 * 20 + 10_000 * 0.2 + 500 * 5) / 1e6
    haiku = (2000 * 0.1 + 50 * 0.5) / 1e6
    assert u["cost_usd"] == pytest.approx(opus + haiku)


def test_transcript_usage_missing_or_absent(tmp_path):
    assert summary.transcript_usage(str(tmp_path / "missing.jsonl")) is None
    assert summary.transcript_usage(None) is None
    assert summary.transcript_usage("") is None


def test_transcript_usage_skips_oversized_files(tmp_path, monkeypatch):
    path = write_transcript(tmp_path / "t.jsonl", [entry("claude-opus-5-5", 1, 1)])
    monkeypatch.setattr(summary, "TRANSCRIPT_LIMIT", 10)
    assert summary.transcript_usage(path) is None


def test_transcript_with_only_unpriced_models_has_no_cost(tmp_path):
    u = summary.transcript_usage(write_transcript(tmp_path / "t.jsonl", [entry("other-model", 5, 5)]))
    assert u["input_tokens"] == 5 and u["cost_usd"] is None


def test_load_summaries_skips_unreadable_files(tmp_path):
    (tmp_path / "a.summary.json").write_text(json.dumps({"session": "a"}))
    (tmp_path / "b.summary.json").write_text("{broken")
    (tmp_path / "c.json").write_text(json.dumps({"session": "c"}))
    assert summary.load_summaries(tmp_path) == [{"session": "a"}]
    assert summary.load_summaries(tmp_path / "missing") == []


def test_session_end_summary_records_transcript_cost(project):
    transcript = project / ".claude-transcript.jsonl"
    write_transcript(transcript, [entry("claude-opus-5-5", 1_000_000, 0)])
    agent = ClaudeCode(project, session="s-cost", transcript=transcript)
    agent.start()
    agent.prompt("look around")
    agent.read("docs/notes.md")
    agent.end()
    s = json.loads((project / ".harness-evo" / "sessions" / "s-cost.summary.json").read_text())
    assert s["cost"]["cost_usd"] == pytest.approx(4.0)
    assert s["cost"]["input_tokens"] == 1_000_000
    assert s["counts"]["files_read"] == 1 and s["verification"]["code_changed"] is False
    assert s["hook_latency"]["n"] >= 3 and s["hook_latency"]["p95_ms"] is not None


def test_summarize_empty_trace():
    from harness_evo.config import Profile
    s = summary.summarize([], Profile({}))
    assert s["started"] is None and s["harness"] is None and s["counts"]["tool_calls"] == 0
    assert s["hook_latency"] == {"n": 0, "p50_ms": None, "p95_ms": None, "max_ms": None, "measured": "in-process"}
    assert s["cost"] == {"input_tokens": None, "output_tokens": None, "cost_usd": None}
