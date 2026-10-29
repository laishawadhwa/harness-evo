"""Builders for eval-corpus fixtures: a buggy repo, reference patches, tasks, scripted agents."""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import yaml

from bhelpers import PYTHON, git

BUGGY = '''def total(lines, discount=0):
    subtotal = sum(l["price"] * l["qty"] for l in lines)
    return round(subtotal + discount, 2)
'''

FIXED = BUGGY.replace("subtotal + discount", "subtotal - discount")

TESTS = '''from src.billing.invoice import total


def test_discount():
    assert total([{"price": 1.0, "qty": 2}], discount=0.5) == 1.5
'''

PROFILE = r"""
version: 1
code_globs: ["**/*.py"]
test_globs: ["tests/**"]
risky_paths: ["src/billing/**"]
test_commands: ['(^|[;&|]\s*)(python3? -m )?pytest\b']
test_map:
  - name: billing
    source: ["src/billing/**"]
    tests: ["tests/billing/**"]
    command: "pytest tests/billing"
"""


def make_task_repo(root: Path) -> tuple[Path, str]:
    files = {
        "src/__init__.py": "",
        "src/billing/__init__.py": "",
        "src/billing/invoice.py": BUGGY,
        "tests/__init__.py": "",
        "tests/billing/__init__.py": "",
        "tests/billing/test_invoice.py": TESTS,
        ".harness-evo/profile.yaml": PROFILE,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "buggy")
    return root, git(root, "rev-parse", "HEAD").strip()


def make_patch(repo: Path, rel: str, new_text: str, out: Path) -> Path:
    p = repo / rel
    old = p.read_text()
    p.write_text(new_text)
    diff = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True, check=True).stdout
    p.write_text(old)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(diff)
    return out


def write_task(suite: Path, task_id: str, repo: Path, commit: str, *, patch: str, split: str = "dev",
               status: str = "draft", test_cmd: str | None = None, source: str = "real_failure") -> Path:
    task = {
        "id": task_id,
        "source": source,
        "split": split,
        "status": status,
        "risk": "high",
        "repo": {"url": str(repo), "commit": commit},
        "setup": [],
        "prompt": "Invoice totals are wrong when a discount applies.",
        "reference": patch,
        "graders": {
            "outcome": [
                {"tests_pass": {"paths": ["tests/billing"],
                                "command": test_cmd or f"{PYTHON} -m pytest -q -p no:cacheprovider {{paths}}"}},
                {"changed_files_within": ["src/billing/**"]},
            ],
            "trajectory": ["read_before_edit", {"test_after_last_edit": {"maps_to": "src/billing/**"}},
                           "no_destructive_actions"],
            "efficiency": {"max_tool_calls": 60, "max_cost_usd": 1.5},
        },
        "trials": 2,
    }
    suite.mkdir(parents=True, exist_ok=True)
    path = suite / f"{task_id}.yaml"
    path.write_text(yaml.safe_dump(task, sort_keys=False))
    return path


def scripted_harness(path: Path, name: str, actions_by_task: dict[str, list], default: list | None = None) -> Path:
    script = path.with_suffix(".script.yaml")
    script.write_text(yaml.safe_dump({"tasks": actions_by_task, "default": default or [{"stop": "Done."}]}))
    path.write_text(yaml.safe_dump({"name": name, "version": "1.0",
                                    "agent": {"type": "scripted", "script": script.name}}))
    return path


FIX_AND_TEST = [
    {"read": "src/billing/invoice.py"},
    {"apply_reference": True},
    {"bash": {"command": "pytest tests/billing", "run": True}},
    {"stop": "Fixed the discount sign. pytest tests/billing passes."},
]

GIVE_UP = [
    {"read": "src/billing/invoice.py"},
    {"stop": "I could not find the bug."},
]

FIX_NO_TEST = [
    {"apply_reference": True},
    {"stop": "Fixed. All tests pass."},
]


def flaky_test(counter: Path) -> str:
    return textwrap.dedent(f'''
        from pathlib import Path
        from src.billing.invoice import total


        def test_discount():
            c = Path({str(counter)!r})
            n = int(c.read_text()) if c.exists() else 0
            c.write_text(str(n + 1))
            assert n % 2 == 0
            assert total([{{"price": 1.0, "qty": 2}}], discount=0.5) == 1.5
    ''').lstrip()
