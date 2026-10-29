"""Write-time redaction of secrets and configured PII patterns (spec sections 4 and 12)."""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache

_BUILTIN = [
    ("private_key", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"),
    ("aws_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    ("api_key", r"\bsk-[A-Za-z0-9_\-]{20,}"),
    ("slack_token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("bearer", r"(?i)(?<=\bbearer )[A-Za-z0-9._~+/=-]{16,}"),
    ("basic_auth", r"(?i)(?<=\bbasic )[A-Za-z0-9+/]{12,}={0,2}"),
    ("url_credentials", r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
    ("stripe_key", r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}"),
    ("gitlab_token", r"\bgl(?:pat|dt|rt|ptt)-[A-Za-z0-9_\-]{20,}"),
    ("npm_token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("pem_body", r"(?:^|\n)(?:[A-Za-z0-9+/]{60,}={0,2}(?:\r?\n|\Z)){3,}"),   # key material without its BEGIN line
]
# Command-line credentials: (prefix kept, secret redacted)
_CLI = [
    re.compile(r"(?i)((?:^|\s)--?(?:password|passwd|pass|token|api-key|secret)(?:=|\s+))(?!-)(\S+)"),
    re.compile(r"((?:^|\s)(?:-u|--user)\s+[^\s:]+:)(\S+)"),
    re.compile(r"(\b(?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n;|&]*?\s-p)(\S+)"),
]
# Stored lengths, applied only after redaction. Negative keeps the tail.
CLIP = {"output_tail": -1500, "error": -1500, "command": 4000, "text": 4000, "final_message": 4000,
        "old_string": 500, "new_string": 500, "error_head": 200, "message": 2000}
DEFAULT_CLIP = 8000
# `NAME=value`, `name: value`, `"name": "value"` where NAME looks like a credential.
_ASSIGN = re.compile(
    r"""(?ix)
    (?P<name>[A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|
        client[_-]?secret|auth[_-]?key|credentials?)[A-Za-z0-9_.-]*)
    (?P<sep>["']?\s*[:=]\s*["']?)
    (?P<value>[^\s"'&;,)}\]]{4,})
    """)


@lru_cache(maxsize=32)
def _compiled(pii: tuple) -> list:
    return [(name, re.compile(p)) for name, p in _BUILTIN] + [("pii", re.compile(p)) for p in pii]


def redact_text(text: str, pii: tuple = ()) -> str:
    if not text:
        return text
    for name, rx in _compiled(pii):
        text = rx.sub(f"[REDACTED:{name}]", text)

    def _assign(m: re.Match) -> str:
        if m.group("value").startswith("[REDACTED"):
            return m.group(0)
        return f"{m.group('name')}{m.group('sep')}[REDACTED:secret]"

    text = _ASSIGN.sub(_assign, text)
    for rx in _CLI:
        text = rx.sub(lambda m: m.group(1) + ("[REDACTED:secret]" if not m.group(2).startswith("[REDACTED")
                                              else m.group(2)), text)
    return text


def clip(obj, key: str | None = None):
    """Clip stored strings to their limits. Call only on already-redacted data."""
    if isinstance(obj, str):
        n = CLIP.get(key or "", DEFAULT_CLIP)
        if len(obj) <= abs(n):
            return obj
        return obj[n:] if n < 0 else obj[:n]
    if isinstance(obj, dict):
        return {k: clip(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clip(v, key) for v in obj]
    return obj


def redact(obj, pii: tuple = ()):
    """Redact every string inside a JSON-like structure."""
    if isinstance(obj, str):
        return redact_text(obj, pii)
    if isinstance(obj, dict):
        return {k: redact(v, pii) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, pii) for v in obj]
    return obj


# For the PR diff scan, which can fail CI, only a quoted literal counts as an assigned secret,
# so `token = get_token()` is not flagged.
_ASSIGN_LITERAL = re.compile(
    r"""(?i)[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|
    client[_-]?secret)[A-Za-z0-9_.-]*["']?\s*[:=]\s*["'](?=[^"']*\d)(?=[^"']*[A-Za-z])([A-Za-z0-9/+_=.\-]{12,})["']""",
    re.X)
_PLACEHOLDER = re.compile(r"(?i)example|placeholder|changeme|dummy|fake|test|xxx|your[_-]|<|\$\{")


def contains_secret(text: str) -> list[str]:
    """Kinds of high-confidence secrets in text (the PR diff-hygiene check)."""
    kinds = [name for name, rx in _compiled(()) if name != "url_credentials" and rx.search(text)]
    m = _ASSIGN_LITERAL.search(text)
    if m and not _PLACEHOLDER.search(m.group(1)):
        kinds.append("assigned_secret")
    return kinds


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
