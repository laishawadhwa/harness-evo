"""graders/model.py: budgeted, cached, calibration-gated model graders, with a fake client (no network)."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from harness_evo.graders import model

GRADER = {"id": "over_engineering", "version": 1, "model": "claude-opus-5-5", "effort": "medium",
          "max_tokens": 4000, "question": "Is it over-engineered?", "rubric": "Flag new layers with one caller."}
INPUTS = {"task": "Fix rounding", "diff": "+x = 1", "trace_summary": "", "final_message": "Done."}
FLAG = {"verdict": "flag", "severity": "high", "explanation": "adds a factory", "evidence": ["src/a.py:3"]}


def usage(i=1000, o=200, cr=0, cw=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr, cache_creation_input_tokens=cw)


def response(payload=FLAG, stop_reason="end_turn", u=None, blocks=None):
    content = blocks if blocks is not None else [SimpleNamespace(type="thinking", text=None),
                                                 SimpleNamespace(type="text", text=json.dumps(payload))]
    return SimpleNamespace(stop_reason=stop_reason, content=content, usage=u or usage())


class FakeMessages:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, BaseException):
            raise r
        return r


class FakeClient:
    def __init__(self, *responses):
        self.messages = FakeMessages(*(responses or (response(),)))
        self.beta = SimpleNamespace(messages=self.messages)

    @property
    def calls(self):
        return self.messages.calls


def run(tmp_path, client, graders=None, cal=None, budget=1.0, inputs=None):
    return model.run(inputs or INPUTS, budget, tmp_path / "cache", client=client,
                     graders=[GRADER] if graders is None else graders, cal={} if cal is None else cal)


def test_flag_result_records_output_cost_and_usage(tmp_path):
    client = FakeClient()
    out = run(tmp_path, client)
    [r] = out["results"]
    assert r["grader"] == "over_engineering" and r["version"] == "1" and r["model"] == "claude-opus-5-5"
    assert r["output"] == FLAG
    assert r["usage"] == {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0}
    # claude-opus-5-5: $4 in / $20 out per million tokens
    assert r["cost_usd"] == pytest.approx((1000 * 4 + 200 * 20) / 1e6)
    assert out["spent_usd"] == pytest.approx(0.008)
    assert out["budget_usd"] == 1.0
    assert r["calibrated"] is False and r["runs"] == 0
    assert "skipped" not in r


def test_request_uses_structured_output_and_server_side_fallback(tmp_path):
    client = FakeClient()
    run(tmp_path, client, inputs={**INPUTS, "trace_summary": ""})
    [kw] = client.calls
    assert kw["model"] == "claude-opus-5-5"
    assert kw["max_tokens"] == 4000
    assert kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["effort"] == "medium"
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": model.SCHEMA}
    assert "Is it over-engineered?" in kw["system"] and "Flag new layers" in kw["system"]
    [msg] = kw["messages"]
    assert msg["role"] == "user"
    assert "<diff>\n+x = 1\n</diff>" in msg["content"] and "<task>" in msg["content"]
    assert "<trace_summary>" not in msg["content"], "empty inputs are left out of the prompt"


def test_grader_defaults_apply_when_fields_are_missing(tmp_path):
    client = FakeClient()
    g = {"id": "minimal", "question": "q", "rubric": "r"}
    out = run(tmp_path, client, graders=[g])
    [kw] = client.calls
    assert kw["model"] == model.DEFAULT_MODEL and kw["max_tokens"] == 4000
    assert kw["output_config"]["effort"] == "medium"
    assert out["results"][0]["version"] == "None"


def test_second_run_with_same_inputs_is_a_cache_hit(tmp_path):
    client = FakeClient()
    run(tmp_path, client)
    out = run(tmp_path, client)
    assert len(client.calls) == 1, "the cached result must not call the model again"
    [r] = out["results"]
    assert r["cached"] is True and r["output"] == FLAG
    assert out["spent_usd"] == 0
    # different inputs miss the cache
    run(tmp_path, client, inputs={**INPUTS, "diff": "+y = 2"})
    assert len(client.calls) == 2


def test_cached_result_reports_the_current_calibration_status(tmp_path):
    client = FakeClient()
    run(tmp_path, client, cal={})
    cal = {"over_engineering": {"version": 1, "runs": 40, "agreement": 0.9}}
    [r] = run(tmp_path, client, cal=cal)["results"]
    assert r["cached"] is True and r["calibrated"] is True and r["runs"] == 40


def test_budget_skip_before_calling(tmp_path):
    client = FakeClient()
    out = run(tmp_path, client, budget=0.0001)
    [r] = out["results"]
    assert r["skipped"].startswith("over the per-PR budget (estimate $")
    assert client.calls == []
    assert out["spent_usd"] == 0


def test_budget_is_shared_across_graders(tmp_path):
    # the first call spends ~$0.084, so the second grader's ~$0.08 estimate no longer fits in $0.10
    client = FakeClient(response(u=usage(i=1000, o=4000)))
    second = {**GRADER, "id": "convention_fit"}
    out = run(tmp_path, client, graders=[GRADER, second], budget=0.10)
    first, skipped = out["results"]
    assert "output" in first
    assert skipped["grader"] == "convention_fit" and "over the per-PR budget" in skipped["skipped"]
    assert len(client.calls) == 1


def test_refusal_is_skipped_costed_and_not_cached(tmp_path):
    client = FakeClient(response(stop_reason="refusal", blocks=[]))
    [r] = run(tmp_path, client)["results"]
    assert r["skipped"] == "refused" and "output" not in r
    assert r["cost_usd"] > 0
    assert not list((tmp_path / "cache").glob("*.json"))
    run(tmp_path, client)
    assert len(client.calls) == 2, "a refusal is retried on the next run"


def test_response_without_text_block_is_skipped(tmp_path):
    client = FakeClient(response(blocks=[SimpleNamespace(type="thinking", text=None)], stop_reason="max_tokens"))
    [r] = run(tmp_path, client)["results"]
    assert r["skipped"] == "no text output (stop_reason max_tokens)"


def test_client_exception_never_fails_the_run(tmp_path):
    client = FakeClient(RuntimeError("boom"))
    out = run(tmp_path, client)
    [r] = out["results"]
    assert r["skipped"] == "call failed: RuntimeError: boom"
    assert out["spent_usd"] == 0


def test_invalid_json_output_is_reported_as_a_failed_call(tmp_path):
    client = FakeClient(response(blocks=[SimpleNamespace(type="text", text="not json")]))
    [r] = run(tmp_path, client)["results"]
    assert r["skipped"].startswith("call failed: JSONDecodeError")


def test_unpriced_model_falls_back_to_zero_cost(tmp_path):
    client = FakeClient()
    [r] = run(tmp_path, client, graders=[{**GRADER, "model": "some-other-model"}])["results"]
    assert r["cost_usd"] == 0.0 and r["output"] == FLAG


@pytest.mark.parametrize("rec, ok", [
    ({"version": 1, "runs": 30, "agreement": 0.80}, True),
    ({"version": "1", "runs": 100, "agreement": 0.95}, True),      # versions compare as strings
    ({"version": 1, "runs": 29, "agreement": 0.99}, False),
    ({"version": 1, "runs": 30, "agreement": 0.79}, False),
    ({"version": 2, "runs": 50, "agreement": 0.90}, False),        # calibrated for another version
    ({}, False),
])
def test_calibration_status(rec, ok):
    status = model.calibration_status(GRADER, {"over_engineering": rec} if rec else {})
    assert status["calibrated"] is ok
    assert status["runs"] == rec.get("runs", 0)
    assert status["agreement"] == rec.get("agreement")
    assert status["calibrated_version"] == rec.get("version")


def test_run_reports_calibration_on_each_result(tmp_path):
    cal = {"over_engineering": {"version": 1, "runs": 30, "agreement": 0.8}}
    [r] = run(tmp_path, FakeClient(), cal=cal)["results"]
    assert r["calibrated"] is True and r["agreement"] == 0.8


def test_missing_sdk_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)   # makes `import anthropic` raise ImportError
    out = model.run(INPUTS, 1.0, tmp_path / "cache", client=None, graders=[GRADER], cal={})
    assert out["results"] == []
    assert "anthropic SDK is not installed" in out["skipped"]


def test_load_graders_and_calibration_from_a_directory(tmp_path):
    d = tmp_path / "graders"
    d.mkdir()
    (d / "a.yaml").write_text("id: a\nversion: 2\nquestion: q\nrubric: r\n")
    (d / "b.yaml").write_text("question: no id, ignored\n")
    (d / "c.yaml").write_text("")
    assert [g["id"] for g in model.load_graders(d)] == ["a"]
    assert model.calibration(d) == {}
    (d / "calibration.json").write_text("{not json")
    assert model.calibration(d) == {}
    (d / "calibration.json").write_text(json.dumps({"a": {"version": 2, "runs": 31, "agreement": 0.9}}))
    assert model.calibration(d)["a"]["runs"] == 31


def test_shipped_graders_load_and_are_uncalibrated():
    graders = model.load_graders()
    ids = {g["id"] for g in graders}
    assert {"over_engineering", "convention_fit", "summary_vs_diff"} <= ids
    cal = model.calibration()
    assert all(not model.calibration_status(g, cal)["calibrated"] for g in graders)
    assert model.grader_dir().name == "model"


def test_build_prompt_wraps_inputs_in_tags():
    system, user = model.build_prompt(GRADER, {"a": "one", "b": "", "c": "three"})
    assert system.startswith("You are a code-review grader. Is it over-engineered?")
    assert user == "<a>\none\n</a>\n\n<c>\nthree\n</c>"
