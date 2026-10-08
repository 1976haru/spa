"""Lossless parsing helpers for Shopify theme JSON documents.

Shopify themes commonly prefix JSON templates with a Liquid-editor block
comment. Only comments in that leading prefix are supported; comment markers
inside JSON strings remain ordinary string data and comments elsewhere are
rejected instead of being silently stripped.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


def _raw_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _semantic_hash(parsed: Any) -> str:
    canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ShopifyJsonDocumentError(ValueError):
    """Precise, safe-to-report parse failure for a Shopify theme document."""

    def __init__(self, code: str, message: str, *, offset: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.offset = offset

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "offset": self.offset}


@dataclass(frozen=True)
class ShopifyJsonDocument:
    raw: str
    parsed: Any
    prefix: str
    suffix: str
    raw_hash: str
    semantic_hash: str
    had_leading_comment: bool

    def metadata(self, *, include_affixes: bool = True) -> dict[str, Any]:
        value = {
            "raw_hash": self.raw_hash,
            "semantic_hash": self.semantic_hash,
            "had_leading_comment": self.had_leading_comment,
        }
        if include_affixes:
            # Internal metadata only. Do not render these fields in beginner UI.
            value.update(prefix=self.prefix, suffix=self.suffix)
        return value


def _scan_prefix(raw: str) -> tuple[str, bool, int]:
    """Return leading BOM/whitespace/comments, whether comments occurred, and JSON start."""
    index = 0
    if raw.startswith("\ufeff"):
        index = 1
    had_comment = False
    while index < len(raw):
        whitespace_start = index
        while index < len(raw) and raw[index].isspace():
            index += 1
        if raw.startswith("/*", index):
            had_comment = True
            close = raw.find("*/", index + 2)
            if close < 0:
                raise ShopifyJsonDocumentError(
                    "UNSUPPORTED_THEME_JSON_COMMENT_SHAPE",
                    "A leading Shopify block comment is not closed.",
                    offset=index,
                )
            index = close + 2
            continue
        if raw.startswith("//", index):
            raise ShopifyJsonDocumentError(
                "UNSUPPORTED_THEME_JSON_COMMENT_SHAPE",
                "Only leading block comments are supported in Shopify JSON templates.",
                offset=index,
            )
        # Preserve all whitespace even when it is the whole prefix.
        if index == whitespace_start:
            break
    return raw[:index], had_comment, index


def parse_shopify_json_document(raw: str) -> ShopifyJsonDocument:
    if not isinstance(raw, str):
        raise ShopifyJsonDocumentError("TEMPLATE_BODY_MISSING", "Shopify theme file body is not readable text.")
    prefix, had_comment, start = _scan_prefix(raw)
    decoder = json.JSONDecoder()
    try:
        parsed, end = decoder.raw_decode(raw, start)
    except json.JSONDecodeError as exc:
        # A comment-like token after the JSON root is not part of the supported prefix.
        if "*/" in raw[start:] or raw.startswith(("/*", "//"), start):
            code = "UNSUPPORTED_THEME_JSON_COMMENT_SHAPE"
            message = "A comment occurs outside the supported leading block-comment prefix."
        else:
            code = "INVALID_THEME_JSON"
            message = "Shopify theme file body is not valid JSON after its supported prefix."
        raise ShopifyJsonDocumentError(code, message, offset=exc.pos) from None
    suffix = raw[end:]
    if suffix and not suffix.isspace():
        raise ShopifyJsonDocumentError(
            "UNSUPPORTED_THEME_JSON_COMMENT_SHAPE" if suffix.lstrip().startswith(("/*", "//")) else "INVALID_THEME_JSON",
            "Only whitespace may follow the Shopify JSON document.",
            offset=end,
        )
    return ShopifyJsonDocument(
        raw=raw,
        parsed=parsed,
        prefix=prefix,
        suffix=suffix,
        raw_hash=_raw_hash(raw),
        semantic_hash=_semantic_hash(parsed),
        had_leading_comment=had_comment,
    )


def render_shopify_json_document(document: ShopifyJsonDocument, parsed: Any) -> str:
    """Render changed JSON while keeping the source's exact prefix and suffix."""
    # Preview payloads are persisted with sorted keys; render deterministically so
    # the exact proposed raw hash survives that round trip.
    serialized = json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)
    return f"{document.prefix}{serialized}{document.suffix}"


def shopify_json_semantic_hash(parsed: Any) -> str:
    """Public semantic hash helper for verification paths operating on parsed data."""
    return _semantic_hash(parsed)
