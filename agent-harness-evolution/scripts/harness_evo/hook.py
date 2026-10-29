"""The live pipeline behind `harness-evo hook <event>` (spec section 5).

stdin JSON -> adapter -> redact -> reducer -> detectors -> policy -> trace + stdout.
Fail open: any exception is logged and swallowed, and the hook exits 0 with no output.
No network calls and no model calls on this path.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

from . import __version__, config, paths
from .detectors import REGISTRY, fire, run as run_detectors
from .paths import Layout
from .policy import ab_arm, contract, decide
from .trace import state as state_mod, store
from .trace.adapters import claude_code
from .trace.redact import clip, redact
from .trace.schema import make_event
from .trace.state import MAX_LATENCIES, State


def main(event_name: str, stdin, stdout) -> int:
    t0 = time.perf_counter()
    root = None
    try:
        raw = stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("hook input is not a JSON object")
        root = paths.project_root(data)
        out = run(event_name, data, root, t0=t0)
        if out is not None:
            stdout.write(json.dumps(out))
    except Exception as exc:  # fail open
        log_error(root, event_name, exc)
    return 0


def log_error(root, event_name: str, exc: BaseException) -> None:
    try:
        layout = Layout(root or paths.project_root())
        layout.logs.mkdir(parents=True, exist_ok=True)
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:])
        with open(layout.logs / "hook-errors.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {event_name} {type(exc).__name__}: {exc}\n{tb}\n")
    except Exception:
        pass


def _load_state(layout: Layout, session: str, profile) -> State:
    """The state file, else a rebuild from the trace (the source of truth), else the PreCompact snapshot."""
    try:
        s = state_mod.load(layout.state(session))
    except Exception as exc:
        log_error(layout.root, "state", exc)
        s = None
    if s is None and layout.trace(session).exists():
        from .replay import rebuild
        s = rebuild(layout.trace(session), profile)
    if s is None:
        try:
            s = state_mod.load(layout.snapshot(session))
        except Exception:
            s = None
    return s or State(session=session)


def _degraded(event_name: str, data: dict, root, profile, policy, session: str) -> dict | None:
    """The session lock timed out: write nothing, but still deny destructive tool calls."""
    log_error(root, event_name, TimeoutError(f"session lock for {session} timed out; ran D7 only, nothing recorded"))
    if event_name != "PreToolUse" or policy.level("D7") != "deny" or policy.is_muted("D7", session):
        return None
    from .detectors import BY_ID
    s = State(session=session)
    for typ, actor, payload, derived in claude_code.Adapter(root, profile, s).convert(event_name, data):
        ev = make_event(session, 1, typ, harness={"name": claude_code.NAME}, model=None, actor=actor,
                        payload=payload, derived=derived)
        for f in BY_ID["D7"].check(ev, s, profile):
            return claude_code.render(event_name, {"deny": policy.render(f.template, f.params)})
    return None


def run(event_name: str, data: dict, root: Path | str, t0: float | None = None) -> dict | None:
    if event_name not in claude_code.HANDLED:
        return None
    t0 = t0 or time.perf_counter()
    from . import settings as user_settings
    user = user_settings.load()
    try:
        user_settings.persist_plugin_options(user)  # so the CLI, run from the Bash tool, sees the plugin options
    except OSError as exc:
        log_error(root, event_name, exc)
    if event_name == "SessionStart" and user.errors:
        log_error(root, event_name, ValueError("; ".join(user.errors)))
    layout = Layout(root)
    cfg = config.load(root)
    profile, policy = cfg.profile, cfg.policy
    session = paths.safe_session(data.get("session_id"))
    pii = tuple(profile.pii_patterns)
    decision = None

    with store.session_lock(layout, session) as locked:
        if not locked:
            return _degraded(event_name, data, root, profile, policy, session)
        s = _load_state(layout, session, profile)
        s.session = session
        if event_name == "PreCompact":
            state_mod.save(layout.snapshot(session), s)
        else:
            if event_name == "SessionStart" and s.seq == 0:
                store.prune(layout, profile.retention_days, keep=session)
            harness = {"name": claude_code.NAME, "version": os.environ.get("CLAUDE_CODE_VERSION"),
                       "evo": __version__, "config": cfg.digest}
            adapter = claude_code.Adapter(root, profile, s)
            new_events = []

            def emit(typ, actor, payload, derived):
                model = payload.get("model") if typ == "session_start" and payload.get("model") else s.model
                # redact first, then clip: truncation must never cut a secret in half
                ev = make_event(session, s.seq + 1, typ, harness=harness, model=model, actor=actor,
                                payload=clip(redact(payload, pii)), derived=clip(redact(derived, pii)))
                state_mod.apply(s, ev)
                new_events.append(ev)
                return ev

            for typ, actor, payload, derived in adapter.convert(event_name, data):
                if typ == "session_start" and s.seq == 0:
                    derived["arm"] = ab_arm(session) if policy.nudge_ab else None
                ev = emit(typ, actor, payload, derived)
                fired = fire(run_detectors(REGISTRY, ev, s, profile), s)
                if fired:
                    d, records = decide(fired, s, policy, ev, session)
                    for rec in records:
                        emit("nudge", actor, rec, {})
                    decision = d or decision
            store.append(layout.trace(session), new_events)

            if event_name == "SessionStart":
                decision = {"context": contract(policy, profile)}
            elif event_name == "PreToolUse" and data.get("tool_name") == "Bash":
                _write_active(layout, session)
            elif event_name == "SessionEnd":
                _session_end(layout, session, s, data, policy, profile)

        s.latencies_ms.append(round((time.perf_counter() - t0) * 1000, 2))
        del s.latencies_ms[:-MAX_LATENCIES]
        state_mod.save(layout.state(session), s)
    return claude_code.render(event_name, decision)


def _write_active(layout: Layout, session: str) -> None:
    """The prepare-commit-msg git hook reads this to add the Harness-Session trailer."""
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    layout.active_session.write_text(session + "\n")


def _session_end(layout: Layout, session: str, s: State, data: dict, policy, profile) -> None:
    from .summary import summarize, transcript_usage
    events = store.read(layout.trace(session))
    usage = transcript_usage(data.get("transcript_path"))
    summary = summarize(events, profile, policy, latencies=s.latencies_ms, usage=usage)
    layout.sessions.mkdir(parents=True, exist_ok=True)
    layout.summary(session).write_text(json.dumps(summary, indent=2))
    try:
        if layout.active_session.read_text().strip() == session:
            layout.active_session.unlink()
    except OSError:
        pass
    if profile.queue_eval_on_end:
        layout.queue.mkdir(parents=True, exist_ok=True)
        (layout.queue / f"{session}.json").write_text(json.dumps({"session": session, "queued": summary["ended"]}))
