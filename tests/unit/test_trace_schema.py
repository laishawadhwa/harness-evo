"""trace/schema.py: event construction, validation, stream monotonicity, timestamps, validate-trace CLI."""
from __future__ import annotations

import json

import pytest

from harness_evo.trace import schema
from unit_helpers import run_cli

HARNESS = {"name": "claude-code", "version": None}


def ev(seq=1, **over):
    e = schema.make_event("s-1", seq, "tool_call", harness=HARNESS, model="claude-opus-5-5",
                          payload={"tool": "Read"}, ts="2026-10-27T10:00:00.123Z")
    e.update(over)
    return e


def test_make_event_shape_and_defaults():
    e = schema.make_event("s-1", 3, "prompt", harness=HARNESS, model=None)
    assert e["v"] == 1 and e["session"] == "s-1" and e["seq"] == 3 and e["type"] == "prompt"
    assert e["actor"] == "main" and e["payload"] == {} and e["derived"] == {} and e["model"] is None
    assert schema.validate(e) == []


def test_now_ts_is_a_valid_millisecond_utc_timestamp():
    ts = schema.now_ts()
    assert ts.endswith("Z") and len(ts) == len("2026-10-27T10:00:00.123Z")
    assert schema.validate(ev(ts=ts)) == []


def test_good_event_validates():
    assert schema.validate(ev()) == []
    assert schema.validate(ev(ts="2026-10-27T10:00:00Z")) == []   # milliseconds are optional


@pytest.mark.parametrize("over, fragment", [
    ({"v": 2}, "v must be 1"),
    ({"session": ""}, "session must be a non-empty string"),
    ({"actor": None}, "actor must be a non-empty string"),
    ({"seq": 0}, "seq must be a positive integer"),
    ({"seq": "1"}, "seq must be a positive integer"),
    ({"ts": "2026-10-27 10:00:00"}, "ts must be an ISO-8601 UTC timestamp ending in Z"),
    ({"ts": "2026-10-27T10:00:00+00:00"}, "ts must be an ISO-8601 UTC timestamp ending in Z"),
    ({"ts": 1730000000}, "ts must be an ISO-8601 UTC timestamp ending in Z"),
    ({"type": "tool_use"}, "type must be one of"),
    ({"harness": {"version": "1"}}, "harness.name is required"),
    ({"harness": "claude-code"}, "harness.name is required"),
    ({"payload": []}, "payload must be an object"),
    ({"derived": None}, "derived must be an object"),
])
def test_bad_events_are_rejected(over, fragment):
    errors = schema.validate(ev(**over))
    assert len(errors) == 1 and fragment in errors[0], errors


def test_missing_model_key_is_an_error_but_null_is_fine():
    e = ev()
    del e["model"]
    assert schema.validate(e) == ["model is required (may be null)"]
    assert schema.validate(ev(model=None)) == []


def test_every_type_is_accepted():
    for t in schema.TYPES:
        assert schema.validate(ev(type=t)) == []


def test_validate_stream_requires_strictly_increasing_seq():
    assert schema.validate_stream([ev(1), ev(2), ev(5)]) == []
    errors = schema.validate_stream([ev(1), ev(2), ev(2), ev(1)])
    assert errors == ["seq 2: not monotonic (after 2)", "seq 1: not monotonic (after 2)"]


def test_validate_stream_prefixes_event_errors_with_seq():
    errors = schema.validate_stream([ev(1), ev(2, type="bogus")])
    assert len(errors) == 1 and errors[0].startswith("seq 2: type must be one of")
    errors = schema.validate_stream([ev(seq=None)])
    assert errors == ["seq None: seq must be a positive integer"]


def test_ts_epoch_with_and_without_milliseconds():
    whole = schema.ts_epoch("2026-10-27T10:00:00Z")
    assert schema.ts_epoch("2026-10-27T10:00:00.123Z") - whole == pytest.approx(0.123, abs=1e-6)
    assert schema.ts_epoch("2026-10-27T10:00:00.123456Z") - whole == pytest.approx(0.123, abs=1e-6)
    assert schema.ts_epoch("2026-10-27T10:00:01Z") - whole == pytest.approx(1.0)
    assert whole == pytest.approx(1793095200.0)   # 2026-10-27T10:00:00 UTC


def _write(path, events):
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def test_validate_trace_cli_valid(tmp_path):
    trace = _write(tmp_path / "t.jsonl", [ev(1), ev(2)])
    rc, out = run_cli("validate-trace", str(trace))
    assert rc == 0 and out == "valid: 0 error(s)\n"


def test_validate_trace_cli_invalid(tmp_path):
    trace = _write(tmp_path / "t.jsonl", [ev(1), ev(1, v=9)])
    rc, out = run_cli("validate-trace", str(trace))
    assert rc == 1
    lines = out.splitlines()
    assert "seq 1: v must be 1" in lines and "seq 1: not monotonic (after 1)" in lines
    assert lines[-1] == "invalid: 2 error(s)"


def test_validate_trace_cli_on_a_real_session(cc):
    cc.prompt("hello")
    cc.read("src/util/strings.py")
    cc.end()
    rc, out = run_cli("validate-trace", str(cc.trace_path))
    assert rc == 0, out
