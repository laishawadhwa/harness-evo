"""trace/store.py: JSONL append/read, the per-session lock, and retention pruning."""
from __future__ import annotations

import os
import time

from harness_evo.paths import Layout
from harness_evo.trace import store

DAY = 86400


def test_append_and_read_round_trip(tmp_path):
    path = tmp_path / "deep" / "t.jsonl"
    store.append(path, [{"seq": 1, "text": "héllo"}])
    store.append(path, [{"seq": 2}, {"seq": 3}])
    assert store.read(path) == [{"seq": 1, "text": "héllo"}, {"seq": 2}, {"seq": 3}]
    assert "héllo" in path.read_text(encoding="utf-8"), "non-ASCII is written as UTF-8, not escaped"


def test_read_skips_blank_and_malformed_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"seq": 1}\n\n   \n{"seq": 2, "trunc\nnot json\n{"seq": 3}\n')
    assert store.read(path) == [{"seq": 1}, {"seq": 3}]


def test_read_missing_file_is_empty(tmp_path):
    assert store.read(tmp_path / "nope.jsonl") == []


def _touch(path, age_days, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    t = now - age_days * DAY
    os.utime(path, (t, t))
    return path


def test_prune_keeps_the_current_session_and_the_active_session_file(tmp_path):
    layout = Layout(tmp_path)
    now = time.time()
    old_trace = _touch(layout.trace("s-old"), 40, now)
    old_state = _touch(layout.state("s-old"), 40, now)
    old_summary = _touch(layout.summary("s-old"), 40, now)
    cur = [_touch(layout.trace("s-cur"), 40, now), _touch(layout.state("s-cur"), 40, now),
           _touch(layout.snapshot("s-cur"), 40, now), _touch(layout.summary("s-cur"), 40, now)]
    active = _touch(layout.active_session, 40, now)
    fresh = _touch(layout.trace("s-new"), 1, now)
    lookalike = _touch(layout.trace("s-cur2"), 40, now)   # shares a prefix with the kept session
    (layout.traces / "subdir").mkdir()

    removed = store.prune(layout, 30, keep="s-cur", now=now)

    assert sorted(removed) == sorted([old_trace, old_state, old_summary, lookalike])
    assert all(p.exists() for p in cur) and active.exists() and fresh.exists()
    assert (layout.traces / "subdir").is_dir()


def test_prune_without_keep_and_missing_folders(tmp_path):
    layout = Layout(tmp_path)
    assert store.prune(layout, 30) == []
    now = time.time()
    old = _touch(layout.trace("s-a"), 2, now)
    assert store.prune(layout, 1, now=now) == [old]


def test_session_lock_gives_up_after_the_timeout(tmp_path):
    layout = Layout(tmp_path)
    with store.session_lock(layout, "s-1"):
        t0 = time.monotonic()
        with store.session_lock(layout, "s-1", timeout_s=0.05):
            waited = time.monotonic() - t0
    assert 0.04 <= waited < 1.0, "the second holder waits, then proceeds rather than blocking the agent"
    assert layout.lock("s-1").exists()
    # the lock is released: a fresh acquire is immediate
    t0 = time.monotonic()
    with store.session_lock(layout, "s-1", timeout_s=1.0):
        pass
    assert time.monotonic() - t0 < 0.5
