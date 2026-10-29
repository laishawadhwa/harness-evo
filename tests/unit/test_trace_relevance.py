"""trace/relevance.py: which changed files a test run covers through the profile's test map."""
from __future__ import annotations

import pytest

from harness_evo.config import Profile
from harness_evo.trace.relevance import covers

MAPPED = Profile({
    "test_globs": ["tests/**"],
    "test_map": [
        {"name": "billing", "source": ["src/billing/**"], "tests": ["tests/billing/**"]},
        {"name": "util", "source": ["src/util/**"], "tests": ["tests/test_strings.py"]},
    ],
})
UNMAPPED = Profile({"test_globs": ["tests/**", "**/test_*.py"]})
BILLING, UTIL = "src/billing/invoice.py", "src/util/strings.py"


def test_full_suite_covers_everything():
    assert covers([], [], [BILLING, UTIL, "docs/x.md"], MAPPED) == [BILLING, UTIL, "docs/x.md"]
    assert covers([], [], iter([BILLING]), MAPPED) == [BILLING]


def test_group_test_directory_covers_its_sources_only():
    assert covers(["tests/billing"], [], [BILLING, UTIL], MAPPED) == [BILLING]


def test_parent_directory_covers_every_group_below_it():
    assert covers(["tests"], [], [BILLING, UTIL], MAPPED) == [BILLING, UTIL]
    assert covers(["tests/"], [], [BILLING], MAPPED) == [BILLING]


def test_exact_test_file_covers_its_group():
    assert covers(["tests/billing/test_invoice.py"], [], [BILLING, UTIL], MAPPED) == [BILLING]
    assert covers(["tests/test_strings.py"], [], [BILLING, UTIL], MAPPED) == [UTIL]


def test_repo_root_target_covers_mapped_groups():
    assert covers(["."], [], [BILLING, UTIL], MAPPED) == [BILLING, UTIL]


def test_filter_naming_the_group_covers_it():
    assert covers([], ["billing"], [BILLING, UTIL], MAPPED) == [BILLING]
    assert covers([], ["Billing and not slow"], [BILLING], MAPPED) == [BILLING]
    assert covers([], ["billingx"], [BILLING], MAPPED) == []        # whole word only


def test_test_file_itself_is_covered_by_its_directory():
    test_file = "tests/billing/test_invoice.py"
    assert covers(["tests/billing"], [], [test_file], MAPPED) == [test_file]
    # unmapped test file: covered by itself or a parent directory
    assert covers(["tests"], [], ["tests/test_misc.py"], UNMAPPED) == ["tests/test_misc.py"]
    assert covers(["tests/test_misc.py"], [], ["tests/test_misc.py"], UNMAPPED) == ["tests/test_misc.py"]
    assert covers(["docs"], [], ["tests/test_misc.py"], UNMAPPED) == []


def test_unmapped_files_match_by_stem_or_parent_directory():
    assert covers(["tests/test_parser.py"], [], ["lib/parser.py"], UNMAPPED) == ["lib/parser.py"]
    assert covers([], ["parser"], ["lib/parser.py"], UNMAPPED) == ["lib/parser.py"]
    assert covers(["tests/lib"], [], ["lib/parser.py"], UNMAPPED) == ["lib/parser.py"]   # parent dir name


def test_unrelated_target_does_not_cover():
    assert covers(["tests/test_trivial.py"], [], [BILLING], MAPPED) == []
    assert covers(["tests/test_other.py"], [], ["lib/parser.py"], UNMAPPED) == []
    assert covers([], ["slow"], ["lib/parser.py"], UNMAPPED) == []


def test_repo_root_target_covers_unmapped_code():
    assert covers(["."], [], ["lib/parser.py"], UNMAPPED) == ["lib/parser.py"]


def test_trivial_test_does_not_cover_an_unrelated_test_file():
    assert covers(["tests/test_trivial.py"], [], ["tests/test_misc.py"], UNMAPPED) == []
