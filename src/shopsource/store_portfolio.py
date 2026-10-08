"""Read-only presentation metadata for multi-store production work."""
from __future__ import annotations

import json
from pathlib import Path

from .paths import STORE_DIR

PORTFOLIO_PATH = STORE_DIR / "store_portfolio.json"


def load_portfolio(path: str | Path | None = None) -> dict:
    source = Path(path) if path else PORTFOLIO_PATH
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "stores": {}}
    return value if isinstance(value.get("stores"), dict) else {"schema_version": 1, "stores": {}}


def store_metadata(store_id: str, path: str | Path | None = None) -> dict:
    return dict(load_portfolio(path).get("stores", {}).get(str(store_id), {}))


def selector_label(profile: dict, metadata: dict | None = None) -> str:
    metadata = metadata if metadata is not None else store_metadata(str(profile.get("store_id", "")))
    store_id = str(profile.get("store_id", ""))
    if metadata.get("role") == "GOLDEN_REFERENCE":
        return f"{store_id} | {metadata.get('display_name') or profile.get('store_name')} — GOLDEN REFERENCE"
    if metadata.get("role") == "PRODUCTION_BUILD":
        return f"{store_id} | {metadata.get('display_name', 'Brand TBD')} — PRODUCTION BUILD"
    return f"{store_id} | {profile.get('store_name', '')}"


def production_bootstrap(profile: dict, metadata: dict | None = None) -> dict:
    """Return real-build status without inferring readiness from code capability."""
    metadata = metadata if metadata is not None else store_metadata(str(profile.get("store_id", "")))
    if metadata.get("role") == "GOLDEN_REFERENCE":
        return {
            "role": "GOLDEN_REFERENCE", "build_origin": metadata.get("build_origin"),
            "mode": "PAUSED_REFERENCE", "progress_percent": metadata.get("evidence_percent", 0),
            "evidence": f"{metadata.get('evidence_verified', 0)}/{metadata.get('evidence_denominator', 14)}",
            "read_only": True,
        }
    connected = profile.get("shopify_status") == "CONNECTED"
    return {
        "role": metadata.get("role", profile.get("role", "PRODUCTION_BUILD")),
        "build_origin": metadata.get("build_origin", profile.get("build_origin")),
        "brand": "NOT SELECTED" if profile.get("brand_name_status") == "NOT_SELECTED" else profile.get("brand_name"),
        "reference_store_id": metadata.get("reference_store_id") or profile.get("reference_store_id"),
        "shopify": "CONNECTED" if connected else "NOT CONNECTED",
        "shopify_gate": "READY" if connected else profile.get("shopify_gate", "WAITING_FOR_SHOPIFY_STORE"),
        "lifecycle": profile.get("lifecycle", "PLANNING"),
        "status": "PLANNING" if connected else "WAITING_FOR_INPUT",
        "blocker": None if connected else "Store 002 Shopify Store 생성 필요",
        "current_step": profile.get("current_step", "BRAND / SHOPIFY SETUP"),
        "progress_percent": int(profile.get("production_progress_percent", 0)),
        "read_only": False,
    }
