"""pr/bundle.py: build a redacted bundle, push it to a bare remote as refs, fetch it back."""
from __future__ import annotations

import json
import tarfile

import pytest

from harness_evo import install
from harness_evo.pr import bundle
from harness_evo.trace import store
from unit_helpers import ClaudeCode, commit_for, copy_repo, git, run_cli, verified_session

REF = "refs/harness-evo/sessions/"


@pytest.fixture(scope="module")
def linked_template(tmp_path_factory, repo_template):
    """feature: one commit linked to traced session s-1, one linked to s-ghost (no trace) and s-1."""
    root = copy_repo(repo_template, tmp_path_factory.mktemp("linked") / "repo")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CLAUDE_PROJECT_DIR", str(root))
        (root / ".harness-evo" / ".gitignore").write_text(install.GITIGNORE)
        git(root, "add", "-A")
        git(root, "commit", "-qm", "ignore harness-evo state")
        git(root, "checkout", "-qb", "feature")
        verified_session(root, "s-1")
        commit_for(root, "fix rounding", "s-1")
        (root / "docs" / "notes.md").write_text("# notes\nmore\n")
        commit_for(root, "docs", "s-ghost", "s-1")
    return root


@pytest.fixture
def linked(tmp_path, monkeypatch, linked_template):
    root = copy_repo(linked_template, tmp_path / "repo")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    return root


@pytest.fixture
def remote(tmp_path, linked):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(linked, "remote", "add", "origin", str(bare))
    git(linked, "push", "-q", "origin", "main", "feature")
    return bare


def _lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_linked_sessions_are_unique_and_ordered(linked):
    assert bundle.linked_sessions(linked, "main", "HEAD") == ["s-ghost", "s-1"]


def test_build_bundle(linked, tmp_path):
    out = tmp_path / "out" / "b.tgz"
    manifest = bundle.build(linked, "main", "HEAD", out)
    assert manifest["sessions"] == ["s-1"] and manifest["missing"] == ["s-ghost"]
    assert manifest["head"] == git(linked, "rev-parse", "HEAD").strip() and manifest["base"] == "main"
    with tarfile.open(out) as tar:
        names = set(tar.getnames())
        assert names == {"traces/s-1.jsonl", "sessions/s-1.summary.json", "manifest.json"}
        trace = tar.extractfile("traces/s-1.jsonl").read().decode()
        assert json.loads(tar.extractfile("manifest.json").read()) == manifest
    assert _lines(trace) == store.read(linked / ".harness-evo" / "traces" / "s-1.jsonl")


def test_bundle_redacts_again(linked, tmp_path):
    trace = linked / ".harness-evo" / "traces" / "s-1.jsonl"
    events = store.read(trace)
    events[1]["payload"]["text"] = "contact bob@example.invalid"   # PII that slipped through
    trace.write_text("".join(json.dumps(e) + "\n" for e in events))
    out = tmp_path / "b.tgz"
    bundle.build(linked, "main", "HEAD", out)
    with tarfile.open(out) as tar:
        text = tar.extractfile("traces/s-1.jsonl").read().decode()
    assert "bob@example.invalid" not in text and "[REDACTED:pii]" in text


def test_push_fetch_and_fast_forward_round_trip(linked, remote, tmp_path):
    rc, out = run_cli("bundle", "--repo", str(linked), "--base", "main", "--push")
    assert rc == 0 and json.loads(out) == {"pushed": ["s-1"], "remote": "origin"}
    refs = git(linked, "ls-remote", "origin", REF + "*")
    assert f"{REF}s-1" in refs and "s-ghost" not in refs
    assert git(linked, "ls-tree", "--name-only", f"{REF}s-1").split() == ["summary.json", "trace.jsonl"]
    first = git(linked, "rev-parse", f"{REF}s-1").strip()

    # CI side: a fresh clone fetches the refs and writes the traces
    clone = tmp_path / "ci"
    git(tmp_path, "clone", "-q", str(remote), str(clone))
    git(clone, "checkout", "-q", "feature")
    out_dir = tmp_path / "fetched"
    rc, out = run_cli("fetch-bundles", "--repo", str(clone), "--base", "main", "--head", "feature",
                      "--out-dir", str(out_dir))
    assert rc == 0 and json.loads(out) == {"fetched": ["s-1"], "missing": ["s-ghost"]}
    fetched = _lines((out_dir / "s-1.jsonl").read_text())
    assert fetched == store.read(linked / ".harness-evo" / "traces" / "s-1.jsonl")
    assert fetched[0]["type"] == "session_start" and fetched[-1]["type"] == "session_end"

    # the session continues; the second push extends the first bundle commit (a fast-forward)
    ClaudeCode(linked, session="s-1").prompt("one more thing")
    assert bundle.push(linked, "main", "HEAD", "origin") == ["s-1"]
    second = git(linked, "rev-parse", f"{REF}s-1").strip()
    assert second != first
    assert git(linked, "rev-parse", f"{REF}s-1^").strip() == first
    assert git(linked, "ls-remote", "origin", f"{REF}s-1").split()[0] == second

    res = bundle.fetch(clone, "main", "feature", "origin", out_dir)
    assert res == {"fetched": ["s-1"], "missing": ["s-ghost"]}
    events = _lines((out_dir / "s-1.jsonl").read_text())
    assert events[-1]["type"] == "prompt" and events[-1]["payload"]["text"] == "one more thing"


def test_bundle_cli_default_path(linked):
    rc, out = run_cli("bundle", "--repo", str(linked), "--base", "main")
    assert rc == 0
    report = json.loads(out)
    assert report["bundle"] == str(linked / ".harness-evo" / "bundles" / "bundle.tgz")
    assert (linked / ".harness-evo" / "bundles" / "bundle.tgz").is_file()
    assert report["sessions"] == ["s-1"] and report["missing"] == ["s-ghost"]


def test_bundle_cli_git_errors_are_user_errors(linked, tmp_path):
    rc, out = run_cli("bundle", "--repo", str(linked), "--base", "no-such-branch", "--out", str(tmp_path / "b.tgz"))
    assert rc == 1 and out.startswith("error: git log")
    rc, out = run_cli("bundle", "--repo", str(linked), "--base", "main", "--push", "--remote", "nowhere")
    assert rc == 1 and out.startswith("error: git push")
    rc, out = run_cli("fetch-bundles", "--repo", str(linked), "--base", "no-such-branch",
                      "--out-dir", str(tmp_path / "x"))
    assert rc == 1 and out.startswith("error: ")


def test_push_skips_sessions_without_traces(linked, tmp_path):
    (linked / ".harness-evo" / "traces" / "s-1.jsonl").unlink()
    assert bundle.push(linked, "main", "HEAD", "no-remote-needed") == []
