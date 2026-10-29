"""Install, uninstall and mute: hook registration, the git trailer hook and the repo profile."""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .cli import CliError
from .paths import Layout, skill_file

GITIGNORE = """# harness-evo: traces, state and labels stay on this machine (spec section 4).
# Only configuration is committed.
*
!.gitignore
!profile.yaml
!nudge-policy.yaml
!policy-state.json
!mutes.json
"""
MARKER = "harness-evo"


def settings_path(root: Path, scope: str) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    return root / ".claude" / ("settings.json" if scope == "project" else "settings.local.json")


def launcher() -> str | None:
    """`sh <plugin>/bin/harness-evo` when the bundled launcher and package are present (source tree or plugin)."""
    shim, scripts = skill_file("bin", "harness-evo"), skill_file("scripts")
    if shim.exists() and scripts.is_dir():
        return f"sh {shlex.quote(str(shim))}"
    return None


def default_command() -> str:
    """The hook command for settings-file installs: the bundled launcher, else a pip-installed harness-evo."""
    shim = launcher()
    if shim:
        return f"{shim} hook"
    if shutil.which("harness-evo"):
        return "harness-evo hook"
    return f"{shlex.quote(sys.executable)} -m harness_evo hook"


_OURS = re.compile(r"harness[-_]evo|hook_entry\.sh")


def _ours(entry: dict) -> bool:
    return any(_OURS.search(h.get("command") or "") for h in entry.get("hooks") or [])


def merge_settings(path: Path, command: str) -> list[str]:
    snippet = json.loads(skill_file("hooks", "settings.snippet.json").read_text().replace("__HOOK_COMMAND__", command))
    data = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    added = []
    for event, entries in snippet["hooks"].items():
        existing = hooks.setdefault(event, [])
        if any(_ours(e) for e in existing):
            continue
        existing.extend(entries)
        added.append(event)
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_suffix(path.suffix + ".bak")
            if backup.exists():  # never overwrite an earlier backup
                import time
                backup = path.with_suffix(path.suffix + f".bak.{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(path, backup)
        path.write_text(json.dumps(data, indent=2) + "\n")
    return added


def install_git_hook(root: Path) -> str:
    try:
        hooks_dir = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=root, capture_output=True,
                                   text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "skipped: not a git repository"
    target = (root / hooks_dir) / "prepare-commit-msg"
    source = skill_file("hooks", "prepare-commit-msg").read_text()
    if target.exists() and MARKER not in target.read_text():
        alt = target.with_name("prepare-commit-msg.harness-evo")
        alt.write_text(source)
        alt.chmod(0o755)
        return f"an existing prepare-commit-msg hook was left alone; call {alt} from it to add the trailer"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)
    target.chmod(0o755)
    return f"installed {target}"


def install(root: Path, scope: str = "local", settings: bool = True, git_hook: bool = True,
            command: str | None = None) -> dict:
    root = Path(root).resolve()
    layout = Layout(root)
    layout.base.mkdir(parents=True, exist_ok=True)
    report = {"project": str(root)}
    gi = layout.base / ".gitignore"
    if layout.mode == "repo" and not gi.exists():
        gi.write_text(GITIGNORE)
    if not layout.profile.exists():
        shutil.copy(skill_file("config", "repo-profile.template.yaml"), layout.profile)
        report["profile"] = f"created {layout.profile} from the template; set test_map, risky_paths, protected_paths"
    if settings:
        path = settings_path(root, scope)
        report["settings"] = {"file": str(path), "events_added": merge_settings(path, command or default_command())}
    if git_hook:
        report["git_hook"] = install_git_hook(root)
    return report


def cmd_install(args, out):
    if args.settings_only:
        path = settings_path(Path(args.project_dir).resolve(), args.scope)
        report = {"settings": {"file": str(path), "events_added": merge_settings(path, args.command or default_command())}}
        out.write(json.dumps(report, indent=2) + "\n")
        return 0
    report = install(Path(args.project_dir), args.scope, not args.no_settings, not args.no_git_hook, args.command)
    out.write(json.dumps(report, indent=2) + "\n")
    return 0


def cmd_uninstall(args, out):
    path = settings_path(Path(args.project_dir).resolve(), args.scope)
    if not path.exists():
        raise CliError(f"{path} does not exist")
    data = json.loads(path.read_text())
    removed = []
    for event, entries in list((data.get("hooks") or {}).items()):
        keep = [e for e in entries if not _ours(e)]
        if len(keep) != len(entries):
            removed.append(event)
        if keep:
            data["hooks"][event] = keep
        else:
            del data["hooks"][event]
    path.write_text(json.dumps(data, indent=2) + "\n")
    out.write(json.dumps({"file": str(path), "events_removed": removed}, indent=2) + "\n")
    return 0


def _mutes(layout: Layout) -> dict:
    try:
        return json.loads(layout.mutes.read_text())
    except (OSError, ValueError):
        return {"repo": [], "sessions": {}}


def _set_mute(args, out, on: bool):
    from .detectors import BY_ID
    det = args.detector.upper()
    if det not in BY_ID:
        raise CliError(f"unknown detector {args.detector}; expected one of {', '.join(BY_ID)}")
    layout = Layout(Path(args.project_dir).resolve())
    layout.base.mkdir(parents=True, exist_ok=True)
    mutes = _mutes(layout)
    bucket = mutes.setdefault("sessions", {}).setdefault(args.session, []) if args.session \
        else mutes.setdefault("repo", [])
    if on and det not in bucket:
        bucket.append(det)
    if not on and det in bucket:
        bucket.remove(det)
    layout.mutes.write_text(json.dumps(mutes, indent=2) + "\n")
    scope = f"session {args.session}" if args.session else "this repo"
    out.write(f"{det} {'muted' if on else 'unmuted'} for {scope}\n")
    return 0


def cmd_mute(args, out):
    return _set_mute(args, out, True)


def cmd_unmute(args, out):
    return _set_mute(args, out, False)
