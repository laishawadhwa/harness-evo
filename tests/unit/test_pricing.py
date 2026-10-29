"""pricing.py: prefix matching, cache-rate defaults, unknown models, multi-model estimates."""
from __future__ import annotations

import pytest

from harness_evo import pricing


def test_longest_prefix_wins():
    assert pricing.rates("claude-opus-5-5")["input"] == 4.0
    assert pricing.rates("claude-opus-5-5-20261001")["input"] == 4.0   # dated id
    assert pricing.rates("claude-opus-5")["input"] == 5.0
    assert pricing.rates("claude-opus-5-1")["input"] == 5.0            # falls back to claude-opus-5
    assert pricing.rates("claude-fable-5-1")["cache_read"] == 0.25
    assert pricing.rates("claude-fable-5")["cache_read"] == 1.0


def test_cache_rates_default_from_input_price():
    r = pricing.rates("claude-opus-5")
    assert r["cache_write"] == pytest.approx(6.25)     # 1.25 x input
    assert r["cache_read"] == pytest.approx(0.5)       # 0.1 x input
    explicit = pricing.rates("claude-opus-5-5")
    assert explicit["cache_read"] == 0.2               # explicit value kept
    assert explicit["cache_write"] == pytest.approx(5.0)
    tiny = pricing.rates("claude-haiku-5-5")
    assert tiny["cache_write"] == pytest.approx(0.125) and tiny["cache_read"] == pytest.approx(0.01)


def test_rates_are_copies():
    r = pricing.rates("claude-opus-5")
    r["input"] = 999
    assert pricing.rates("claude-opus-5")["input"] == 5.0


@pytest.mark.parametrize("m", [None, "", "gpt-5", "opus-5-5", "Claude-Opus-5-5"])
def test_unknown_model_has_no_rates(m):
    assert pricing.rates(m) is None
    assert pricing.cost(m, {"input_tokens": 10}) is None


def test_cost_counts_every_token_kind():
    u = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 2_000_000,
         "cache_creation_input_tokens": 400_000}
    # opus-5-5: 4 + 0.1*20 + 2*0.2 + 0.4*5
    assert pricing.cost("claude-opus-5-5", u) == pytest.approx(4 + 2 + 0.4 + 2)
    assert pricing.cost("claude-opus-5-5", {}) == 0
    assert pricing.cost("claude-sonnet-5", {"output_tokens": 1000}) == pytest.approx(0.01)


def test_estimate_sums_priced_models_only():
    usage = {"claude-opus-5-5": {"input_tokens": 1_000_000}, "claude-haiku-5-5": {"output_tokens": 2_000_000},
             "unknown": {"input_tokens": 5_000_000}}
    assert pricing.estimate(usage) == pytest.approx(4.0 + 1.0)
    assert pricing.estimate({"unknown": {"input_tokens": 1}}) is None
    assert pricing.estimate({}) is None
