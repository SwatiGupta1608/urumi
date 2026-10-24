"""API keys and error sanitisation.

Key format: ``urumi_<key_id>_<secret>``
  key_id  12 lowercase hex chars (public, used for lookup)
  secret  43 url-safe base64 chars (256 bits from `secrets`)
Only HMAC-SHA256(pepper, full key) is stored; comparison is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

KEY_PREFIX = "urumi_"
_KEY_RE = re.compile(r"^urumi_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")


@dataclass(frozen=True)
class NewKey:
    key_id: str
    raw: str  # shown to the caller exactly once
    key_hash: str


def hash_key(pepper: bytes, raw: str) -> str:
    return hmac.new(pepper, raw.encode(), hashlib.sha256).hexdigest()


def generate_key(pepper: bytes) -> NewKey:
    key_id = secrets.token_hex(6)
    raw = f"{KEY_PREFIX}{key_id}_{secrets.token_urlsafe(32)}"
    return NewKey(key_id=key_id, raw=raw, key_hash=hash_key(pepper, raw))


def parse_key(raw: str) -> str | None:
    """Return the key_id if `raw` is well-formed, else None."""
    m = _KEY_RE.match(raw)
    return m.group(1) if m else None


def key_matches(pepper: bytes, raw: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_key(pepper, raw), stored_hash)


# Anything that looks like a credential is masked before an error string is
# stored, logged or returned.
_REDACTIONS = [
    (re.compile(r"urumi_[0-9a-f]{12}_[A-Za-z0-9_-]+"), "urumi_***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer ***"),
    (re.compile(r"(?i)authorization['\"]?\s*[:=]\s*['\"]?[^'\",\s]+(\s+[^'\",\s]+)?"), "authorization: ***"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "gh_***"),
    (re.compile(r"\b(ck|cs)_[0-9a-f]{40}\b"), r"\1_***"),
    (re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)=([^&\s]+)"), r"\1=***"),
    (re.compile(r"://[^/@\s]+:[^/@\s]+@"), "://***@"),
]


def sanitize(text: str, limit: int = 300) -> str:
    out = text.replace("\r", " ").replace("\n", " ")
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    return out[:limit]


def describe_exception(exc: BaseException) -> str:
    """Short, credential-free description of an exception (unwrapping exception groups)."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    msg = str(exc).strip()
    return sanitize(f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__)
