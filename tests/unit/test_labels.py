"""labels.py: finding ids, PR comment checkboxes, the de-duplicating label store, and its CLI."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from harness_evo import labels
from harness_evo.cli import CliError
from unit_helpers import run_cli


@pytest.mark.parametrize("fid, parsed", [
    ("s-1#12:D4", {"rule": "D4", "session": "s-1", "seq": 12}),
    ("pr:tests_pass:abcd1234", {"rule": "tests_pass", "session": None, "seq": None}),
    ("pr:model:over_engineering:abcd1234", {"rule": "model:over_engineering", "session": None, "seq": None}),
    ("s-1:D4", {"rule": "D4", "session": "s-1", "seq": None}),
    ("s-1#x:D4", {"rule": "D4", "session": "s-1", "seq": None}),
])
def test_parse_id(fid, parsed):
    assert labels.parse_id(fid) == parsed


def box(fid, label, ticked):
    return f"   - [{'x' if ticked else ' '}] {label} <!-- finding:{fid} -->"


def test_from_comment_one_box_ticked():
    body = "\n".join([box("s-1#12:D4", "confirmed", True), box("s-1#12:D4", "false positive", False),
                      box("pr:tests_pass:abcd1234", "confirmed", False),
                      box("pr:tests_pass:abcd1234", "false positive", True)])
    recs = {r["finding"]: r for r in labels.from_comment(body, 7)}
    assert set(recs) == {"s-1#12:D4", "pr:tests_pass:abcd1234"}
    d4 = recs["s-1#12:D4"]
    assert d4["label"] == "confirmed" and d4["rule"] == "D4" and d4["session"] == "s-1" and d4["seq"] == 12
    assert d4["pr"] == 7 and d4["source"] == "pr-comment" and d4["ts"].endswith("Z")
    assert recs["pr:tests_pass:abcd1234"]["label"] == "false_positive"


def test_from_comment_uppercase_x_counts():
    assert labels.from_comment("- [X] confirmed <!-- finding:a#1:D1 -->", None)[0]["label"] == "confirmed"


def test_from_comment_both_boxes_ticked_is_skipped():
    body = box("s-1#3:D1", "confirmed", True) + "\n" + box("s-1#3:D1", "false positive", True)
    assert labels.from_comment(body, 1) == []


def test_from_comment_none_ticked():
    body = box("s-1#3:D1", "confirmed", False) + "\n" + box("s-1#3:D1", "false positive", False)
    assert labels.from_comment(body, 1) == []
    assert labels.from_comment("no checkboxes here", 1) == []


def rec(fid, label, ts="2026-10-27T00:00:00Z"):
    return {"finding": fid, **labels.parse_id(fid), "label": label, "pr": None, "source": "manual", "ts": ts}


def test_append_de_duplicates_the_same_label(tmp_path):
    path = tmp_path / "sub" / "labels.jsonl"
    assert labels.append(path, [rec("s-1#3:D1", "confirmed")]) == 1
    assert labels.append(path, [rec("s-1#3:D1", "confirmed")]) == 0
    assert len(labels.read(path)) == 1
    # a changed label is appended and becomes the latest
    assert labels.append(path, [rec("s-1#3:D1", "false_positive")]) == 1
    assert labels.latest(labels.read(path))["s-1#3:D1"]["label"] == "false_positive"
    assert len(labels.read(path)) == 2


def test_append_nothing_new_does_not_create_the_file(tmp_path):
    assert labels.append(tmp_path / "labels.jsonl", []) == 0
    assert not (tmp_path / "labels.jsonl").exists()


def test_latest_keys_by_finding_or_trial():
    out = labels.latest([{"finding": "a", "label": "confirmed"}, {"trial": "run:t#0", "label": "acceptable"},
                         {"finding": "a", "label": "false_positive"}, {"label": "orphan"}])
    assert out == {"a": {"finding": "a", "label": "false_positive"},
                   "run:t#0": {"trial": "run:t#0", "label": "acceptable"}}


def test_read_missing_file(tmp_path):
    assert labels.read(tmp_path / "nope.jsonl") == []


def test_read_skips_a_malformed_line(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text(json.dumps(rec("s-1#3:D1", "confirmed")) + "\n" + '{"finding": "s-1#4:D1", "lab\n')
    assert [r["finding"] for r in labels.read(path)] == ["s-1#3:D1"]


def test_label_cli(tmp_path):
    rc, out = run_cli("label", "s-1#12:D4", "confirmed", "--project-dir", str(tmp_path))
    assert rc == 0 and out == "1 label written\n"
    rc, out = run_cli("label", "s-1#12:D4", "confirmed", "--project-dir", str(tmp_path))
    assert rc == 0 and out == "0 label written\n"
    [r] = labels.read(tmp_path / ".harness-evo" / "labels.jsonl")
    assert r["finding"] == "s-1#12:D4" and r["rule"] == "D4" and r["seq"] == 12 and r["source"] == "manual"


def test_label_cli_rejects_unknown_labels(tmp_path):
    with pytest.raises(SystemExit):
        run_cli("label", "s-1#12:D4", "maybe", "--project-dir", str(tmp_path))


def test_sync_labels_needs_a_source(tmp_path):
    rc, out = run_cli("sync-labels", "--project-dir", str(tmp_path))
    assert rc == 1 and out == "error: give --comment-file or --pr\n"


def test_sync_labels_from_a_comment_file(tmp_path):
    comment = tmp_path / "c.md"
    comment.write_text(labels.MARKER + "\n" + box("s-2#5:D3", "false positive", True))
    rc, out = run_cli("sync-labels", "--project-dir", str(tmp_path), "--comment-file", str(comment), "--pr", "4")
    assert rc == 0 and out == "1 new label(s) from 1 ticked finding(s)\n"
    [r] = labels.read(tmp_path / ".harness-evo" / "labels.jsonl")
    assert r["label"] == "false_positive" and r["pr"] == 4


def _fake_gh(monkeypatch, returncode=0, stdout="", stderr=""):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(labels.subprocess, "run", fake_run)
    return calls


def test_sync_labels_fetches_the_newest_report_comment_with_gh(tmp_path, monkeypatch):
    old = labels.MARKER + "\n" + box("s-1#1:D1", "confirmed", True)
    new = labels.MARKER + "\n" + box("s-1#2:D4", "confirmed", True)
    calls = _fake_gh(monkeypatch, stdout=f"unrelated comment\n{old}\nlgtm\n{new}\n")
    rc, out = run_cli("sync-labels", "--project-dir", str(tmp_path), "--pr", "9", "--repo-slug", "eg/app")
    assert rc == 0 and out.startswith("1 new label(s)")
    assert calls[0][:3] == ["gh", "api", "--paginate"] and calls[0][3] == "repos/eg/app/issues/9/comments"
    [r] = labels.read(tmp_path / ".harness-evo" / "labels.jsonl")
    assert r["finding"] == "s-1#2:D4" and r["pr"] == 9


def test_fetch_comment_defaults_to_the_current_repo(monkeypatch):
    calls = _fake_gh(monkeypatch, stdout=labels.MARKER + " body")
    assert labels.fetch_comment(3, None) == labels.MARKER + " body"
    assert calls[0][3] == "repos/{owner}/{repo}/issues/3/comments"


def test_fetch_comment_errors(monkeypatch):
    _fake_gh(monkeypatch, returncode=1, stderr="HTTP 404\n")
    with pytest.raises(CliError, match="gh api failed: HTTP 404"):
        labels.fetch_comment(3, "eg/app")
    _fake_gh(monkeypatch, stdout="just a human comment")
    with pytest.raises(CliError, match="no harness-evo report comment on PR #3"):
        labels.fetch_comment(3, "eg/app")
