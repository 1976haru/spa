"""Versioned scope contract derived from the Shopify operations in this repository."""
from __future__ import annotations

import json
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "config" / "shopify_scope_contract.json"


def load_scope_contract() -> dict:
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if not value.get("contract_version") or not isinstance(value.get("features"), dict):
        raise ValueError("Shopify scope contract is malformed")
    return value


def scope_preflight(granted: list[str] | set[str], *, gate: str = "G0_READ_ONLY") -> dict:
    contract = load_scope_contract()
    features = contract["features"]
    current = features.get(gate, {})
    granted_set = set(granted)
    required = sorted(set(current.get("required_now", [])))
    optional = sorted(set(current.get("optional_read", [])))
    future = sorted({scope for feature in features.values() for scope in feature.get("future_write", [])})
    restricted = sorted({scope for feature in features.values() for scope in feature.get("restricted_or_approval_required", [])})
    return {
        "contract_version": contract["contract_version"],
        "declared_required": required,
        "declared_optional": optional,
        "granted": sorted(granted_set),
        "missing_for_current_gate": sorted(set(required) - granted_set),
        "future_write": future,
        "restricted_or_approval_required": restricted,
    }
