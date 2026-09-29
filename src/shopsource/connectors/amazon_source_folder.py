from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from .base import ProductSourceConnector

RUNTIME_FILE_NAMES = {"SDK_SESSION_POOL_STATE.json"}
RUNTIME_FILE_PREFIXES = ("SDK_CRAWLER_STATISTICS_",)


def _is_sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return (
        "cookie" in normalized
        or "authorization" in normalized
        or "password" in normalized
        or "secret" in normalized
        or ("access" in normalized and "token" in normalized)
        or ("session" in normalized and "token" in normalized)
        or ("waf" in normalized and "token" in normalized)
    )


def _contains_sensitive_key(value) -> bool:
    if isinstance(value, dict):
        return any(
            _is_sensitive_key(key) or _contains_sensitive_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_key(child) for child in value)
    return False


class AmazonSourceFolderConnector(ProductSourceConnector):
    """Read user-provided Amazon product JSON without modifying source files."""

    def iter_products(self, source: Path) -> Iterable[tuple[dict, dict]]:
        source = source.resolve()
        files = [source] if source.is_file() else sorted(source.rglob("*.json"))
        for file in files:
            relative = file.name if source.is_file() else str(file.relative_to(source))
            batch_id = file.parent.name if file.parent != source else "inbox_root"
            meta = {
                "job_id": batch_id,
                "batch_id": batch_id,
                "source_kind": "AMAZON_SOURCE_FOLDER",
                "source_file": relative,
            }
            if file.name in RUNTIME_FILE_NAMES or file.name.startswith(RUNTIME_FILE_PREFIXES):
                yield {"_invalid": "Spark runtime/session file is not a product payload"}, {
                    **meta, "error_code": "RUNTIME_FILE_REJECTED",
                }
                continue
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
            except Exception as exc:
                yield {"_invalid": str(exc)}, {**meta, "error_code": "MALFORMED_JSON"}
                continue
            if not isinstance(payload, dict):
                yield {"_invalid": "Product payload must be a JSON object"}, {
                    **meta, "error_code": "INVALID_JSON_TYPE",
                }
                continue
            if _contains_sensitive_key(payload):
                yield {"_invalid": "Credential/session field detected"}, {
                    **meta, "error_code": "SENSITIVE_FIELD_REJECTED",
                }
                continue
            meta.update({
                "collected_at": payload.get("_collectedAt"),
                "source_url": payload.get("_sourceUrl"),
                "list_page": payload.get("_listPage"),
            })
            yield payload, meta
