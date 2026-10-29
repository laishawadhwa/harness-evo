"""trace/redact.py: write-time redaction and the PR diff secret scan."""
from __future__ import annotations

import pytest

from harness_evo.trace import redact

GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.mark.parametrize("text", [
    "token = get_token()",
    "password = os.environ['DB_PASSWORD']",
    'api_key = "your-api-key-here-123"',
    'secret = "example-secret-12345"',
    'token: "${TOKEN_FROM_ENV_1}"',
    'password = "short1"',
    'url = "https://user:pa55word@example.invalid/x"',   # URL credentials are not part of the PR scan
])
def test_contains_secret_no_hit(text):
    assert redact.contains_secret(text) == []


@pytest.mark.parametrize("text, kind", [
    ('api_key = "Ab3dEf6hIj9kLm2nOp4q"', "assigned_secret"),
    ("PASSWORD: 'hunter2hunter2'", "assigned_secret"),
    (f"GITHUB={GH}", "github_token"),
    ("key AKIAABCDEFGHIJKLMNOP", "aws_key"),
    ("x = 'sk-ant-api03-abcdefghijklmnop'", "anthropic_key"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----", "private_key"),
])
def test_contains_secret_hit(text, kind):
    assert kind in redact.contains_secret(text)


def test_redact_text_masks_builtins_and_assignments():
    text = f"export API_KEY=abcd1234efgh and {GH} and Authorization: Bearer abcdefghijklmnop1234"
    out = redact.redact_text(text)
    assert "abcd1234efgh" not in out and GH not in out and "abcdefghijklmnop1234" not in out
    assert "API_KEY=[REDACTED:secret]" in out
    assert "[REDACTED:github_token]" in out and "[REDACTED:bearer]" in out


def test_redact_text_keeps_url_shape():
    assert redact.redact_text("git clone https://bob:s3cret@host.invalid/r.git") == \
        "git clone https://[REDACTED:url_credentials]@host.invalid/r.git"


def test_redact_uses_pii_patterns_and_walks_structures():
    obj = {"a": ["mail bob@example.invalid", 3, None], "b": {"c": "password=abcd1234"}, "d": True}
    out = redact.redact(obj, (r"[a-z]+@example\.invalid",))
    assert out == {"a": ["mail [REDACTED:pii]", 3, None], "b": {"c": "password=[REDACTED:secret]"}, "d": True}


def test_redact_empty_and_already_redacted():
    assert redact.redact_text("") == ""
    assert redact.redact_text(None) is None
    once = redact.redact_text("token=sk-abcdefghijklmnopqrstuvwxyz")
    assert redact.redact_text(once) == once


def test_sha256():
    assert redact.sha256("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
