"""Small, dependency-free redaction helpers for reports, logs and UI errors."""
from __future__ import annotations

import re

_PATTERNS = (
    re.compile(r"shpat_[A-Za-z0-9_-]+", re.I),
    re.compile(r"sk-[A-Za-z0-9_-]{8,}", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.I),
    re.compile(r"\b(?:access|api|session|auth)[_-]?token\s*[:=]\s*[^\s,;]+", re.I),
    re.compile(r"\b(?:cookie|set-cookie)\s*[:=]\s*[^\r\n]+", re.I),
    re.compile(r"\b[A-Za-z0-9_-]{35,}\b"),
)


def redact_text(value: object, *, limit: int | None = None) -> str:
    text = str(value)
    for pattern in _PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:limit] if limit is not None else text


def redact_value(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"sha256", "prompt_hash", "hash"} and isinstance(item, str) and re.fullmatch(r"[a-fA-F0-9]{64}", item):
                result[key] = item
            else:
                result[key] = redact_value(item)
        return result
    if isinstance(value, list): return [redact_value(item) for item in value]
    if isinstance(value, tuple): return tuple(redact_value(item) for item in value)
    if isinstance(value, str): return redact_text(value, limit=500)
    return value
