"""`harness-evo init`: detect a repo's stack and write a starter profile (spec section 4).

Hooks work in any repo with no setup. A profile makes them precise: which commands are test runs, and
which tests cover which source (the test map behind D3, D4 and D5). Nothing is written by a dry run, and
an existing profile is never replaced without --force.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .cli import CliError
from .paths import Layout, skill_file

_PY_MARKERS = ("pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini")
_SKIP_DIRS = {"tests", "test", "docs", "scripts", "build", "dist", "node_modules", "venv", ".venv", "examples"}


def _exists(root: Path, *names: str) -> bool:
    return any((root / n).exists() for n in names)


def _python(root: Path, out: dict) -> None:
    if not _exists(root, *_PY_MARKERS):
        return
    out["stacks"].append("python")
    out["test_commands"].append("pytest")
    tests_root = next((d for d in ("tests", "test") if (root / d).is_dir()), None)
    packages = []
    if (root / "src").is_dir():
        packages += [(p.name, f"src/{p.name}") for p in sorted((root / "src").iterdir())
                     if p.is_dir() and (p / "__init__.py").exists()]
    packages += [(p.name, p.name) for p in sorted(root.iterdir())
                 if p.is_dir() and p.name not in _SKIP_DIRS and not p.name.startswith(".")
                 and (p / "__init__.py").exists()]
    for name, src in packages:
        if tests_root and (root / tests_root / name).is_dir():
            tests, cmd = f"{tests_root}/{name}/**", f"pytest {tests_root}/{name}"
        else:
            tests, cmd = f"{tests_root or 'tests'}/**", "pytest"
        out["test_map"].append({"name": name, "source": [f"{src}/**"], "tests": [tests], "command": cmd})
    pyproject = (root / "pyproject.toml").read_text(errors="replace") if (root / "pyproject.toml").exists() else ""
    if _exists(root, "ruff.toml", ".ruff.toml") or "[tool.ruff" in pyproject:
        out["lint_commands"].append({"name": "ruff", "command": "ruff check ."})
    if _exists(root, "mypy.ini", ".mypy.ini") or "[tool.mypy" in pyproject:
        out["typecheck_commands"].append({"name": "mypy", "command": "mypy ."})


def _node(root: Path, out: dict) -> None:
    pkg = root / "package.json"
    if not pkg.exists():
        return
    try:
        scripts = (json.loads(pkg.read_text()) or {}).get("scripts") or {}
    except ValueError:
        scripts = {}
    out["stacks"].append("node")
    manager = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() \
        else "bun" if _exists(root, "bun.lockb", "bun.lock") else "npm"
    if "test" in scripts:
        cmd = f"{manager} test"
        out["test_commands"].append(cmd)
        out["test_map"].append({"name": "js", "source": ["src/**", "lib/**"],
                                "tests": ["**/*.{test,spec}.{ts,tsx,js,jsx}", "**/__tests__/**"], "command": cmd})
    if _exists(root, ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", "eslint.config.js",
               "eslint.config.mjs", "eslint.config.cjs"):
        out["lint_commands"].append({"name": "eslint", "command": "npx eslint ."})
    if (root / "tsconfig.json").exists():
        out["typecheck_commands"].append({"name": "tsc", "command": "npx tsc --noEmit"})


def _simple(root: Path, out: dict) -> None:
    rules = [
        ("go.mod", "go", "go test ./...", {"source": ["**/*.go"], "tests": ["**/*_test.go"]}),
        ("Cargo.toml", "rust", "cargo test", {"source": ["src/**"], "tests": ["tests/**", "src/**"]}),
        ("pom.xml", "jvm", "mvn -q test", {"source": ["src/main/**"], "tests": ["src/test/**"]}),
    ]
    for marker, name, cmd, globs in rules:
        if (root / marker).exists():
            out["stacks"].append(name)
            out["test_commands"].append(cmd)
            out["test_map"].append({"name": name, **globs, "command": cmd})
    if _exists(root, "build.gradle", "build.gradle.kts") and not (root / "pom.xml").exists():
        cmd = "./gradlew test" if (root / "gradlew").exists() else "gradle test"
        out["stacks"].append("jvm")
        out["test_commands"].append(cmd)
        out["test_map"].append({"name": "jvm", "source": ["src/main/**"], "tests": ["src/test/**"], "command": cmd})
    if (root / "Gemfile").exists() and (root / "spec").is_dir():
        out["stacks"].append("ruby")
        out["test_commands"].append("bundle exec rspec")
        out["test_map"].append({"name": "ruby", "source": ["lib/**", "app/**"], "tests": ["spec/**"],
                                "command": "bundle exec rspec"})


def detect(root: Path) -> dict:
    out: dict = {"stacks": [], "test_commands": [], "test_map": [], "lint_commands": [], "typecheck_commands": []}
    for step in (_python, _node, _simple):
        step(root, out)
    return out


def profile_from(detected: dict) -> dict:
    profile = {"version": 1}
    for key in ("test_map", "lint_commands", "typecheck_commands"):
        if detected[key]:
            profile[key] = detected[key]
    return profile


def render(profile: dict, detected: dict) -> str:
    from ._yaml import yaml
    stacks = ", ".join(detected["stacks"]) or "nothing recognised"
    head = (f"# harness-evo repo profile, written by `harness-evo init` (detected: {stacks}).\n"
            "# Check test_map: which tests cover which source decides whether a test run counts as verification.\n")
    body = yaml.safe_dump(profile, sort_keys=False, default_flow_style=False, width=110)
    template = skill_file("config", "repo-profile.template.yaml").read_text()
    rest = "\n".join(f"# {line}" if line.strip() and not line.startswith("#") else line
                     for line in template.splitlines() if not re.match(r"^version:", line))
    return head + body + "\n# Everything below is the built-in default; uncomment a key to change it.\n" + rest + "\n"


def run(root: Path, dry_run: bool = False, force: bool = False, git_hook: bool | None = None) -> dict:
    root = Path(root).resolve()
    layout = Layout(root)
    target = layout.config_dir / "profile.yaml" if layout.mode == "repo" else layout.base / "profile.yaml"
    detected = detect(root)
    profile = profile_from(detected)
    result = {"project": str(root), "path": str(target), "detected": detected, "profile": profile,
              "written": False, "git_hook": None}
    if dry_run:
        result["text"] = render(profile, detected)
        return result
    if target.exists() and not force:
        raise CliError(f"{target} already exists; pass --force to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    if layout.mode == "repo":
        from .install import GITIGNORE
        gi = layout.config_dir / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE)
    target.write_text(render(profile, detected))
    result["written"] = True
    if git_hook:
        from .install import install_git_hook
        result["git_hook"] = install_git_hook(root)
    return result


def cmd_init(args, out):
    result = run(Path(args.project_dir), args.dry_run, args.force, args.git_hook)
    if args.json:
        out.write(json.dumps({k: v for k, v in result.items() if k != "text"}, indent=2) + "\n")
        return 0
    d = result["detected"]
    out.write(f"Detected: {', '.join(d['stacks']) or 'no known stack'}; test commands: "
              f"{', '.join(d['test_commands']) or 'none'}\n")
    if args.dry_run:
        out.write(f"\nWould write {result['path']}:\n\n{result['text']}")
    else:
        out.write(f"Wrote {result['path']}\n")
        if result["git_hook"]:
            out.write(f"Git hook: {result['git_hook']}\n")
        elif args.git_hook is None:
            out.write("Commit trailers are off. Run `harness-evo init --git-hook --force` (or "
                      "`harness-evo install --no-settings`) to tag agent commits for the PR check.\n")
    return 0
