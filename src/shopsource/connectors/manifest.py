from __future__ import annotations

import hashlib
from datetime import datetime, timezone

MANIFEST_SCHEMA_VERSION = "1"


def build_manifest(
    *,
    store_id: str,
    store_name: str,
    asins: list[str],
    source_job_ids: list[str] | None = None,
    filter_profile: dict | None = None,
    export_status: str = "PREPARED",
) -> dict:
    """Build credential-free metadata shared by future export connectors."""
    normalized_asins = sorted({str(asin).strip().upper() for asin in asins if str(asin).strip()})
    digest = hashlib.sha256("\n".join(normalized_asins).encode("utf-8")).hexdigest()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "store_id": store_id,
        "store_name": store_name,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_job_ids": sorted(set(source_job_ids or [])),
        "product_count": len(asins),
        "asin_count": len(normalized_asins),
        "asin_hash": f"sha256:{digest}",
        "filter_profile": filter_profile or {},
        "export_status": export_status,
    }
