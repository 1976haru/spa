"""Local-only, idempotent prerequisite resolution for homepage planning."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .collection_planner import CollectionPlanner
from .db import connect, get_store


@dataclass
class CollectionPrerequisiteResult:
    status: str
    plan: dict[str, Any] | None = None
    source: str | None = None
    created_at: str | None = None
    reason: str | None = None


class CollectionPrerequisiteService:
    """Reuse a valid latest plan or create one from local-only store evidence."""

    def __init__(self, db=None, planner_factory=CollectionPlanner):
        self.db = db
        self.planner_factory = planner_factory

    def ensure_collection_plan(self, store_id: str, *, allow_create: bool = True) -> CollectionPrerequisiteResult:
        latest, invalid_reason = self._latest_valid_plan(store_id)
        if latest:
            settings = latest["plan"].get("settings") or {}
            return CollectionPrerequisiteResult("READY", latest["plan"],
                "AUTO_PREREQUISITE" if settings.get("source") == "AUTO_PREREQUISITE" else "EXISTING",
                latest.get("created_at"))
        if not allow_create:
            return CollectionPrerequisiteResult("WAITING_FOR_INPUT", reason=invalid_reason or "COLLECTION_PLAN_MISSING")
        enough, reason = self._has_local_evidence(store_id)
        if not enough:
            return CollectionPrerequisiteResult("WAITING_FOR_INPUT", reason=reason)

        # Empty collections are useful as future prompt targets when local categories
        # exist but product rows have not arrived yet; this remains a draft plan only.
        try:
            created = self.planner_factory(self.db).create_plan(store_id, settings={
                "include_empty": True, "min_products": 0,
            })
            if not self._valid_structure(created, store_id):
                return CollectionPrerequisiteResult("WAITING_FOR_INPUT", reason="NO_VALID_COLLECTION_DEFINITIONS")
            self._mark_auto_prerequisite(created["plan_id"])
            created = self.planner_factory(self.db).get_plan(created["plan_id"])
            with connect(self.db) as con:
                created_at = con.execute("SELECT created_at FROM store_collection_plans WHERE plan_id=?", (created["plan_id"],)).fetchone()[0]
            return CollectionPrerequisiteResult("READY", created, "AUTO_PREREQUISITE", created_at)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return CollectionPrerequisiteResult("WAITING_FOR_INPUT", reason=f"COLLECTION_PLAN_UNAVAILABLE:{type(exc).__name__}")

    def _latest_valid_plan(self, store_id: str):
        with connect(self.db) as con:
            row = con.execute("SELECT plan_id,created_at,settings_json FROM store_collection_plans WHERE store_id=? ORDER BY version DESC LIMIT 1", (store_id,)).fetchone()
        if not row:
            return None, None
        try:
            plan = self.planner_factory(self.db).get_plan(row["plan_id"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return None, "LATEST_PLAN_INVALID"
        if not self._valid_structure(plan, store_id):
            # Don't repeatedly append empty auto-generated revisions. Keep history
            # untouched and ask for more local evidence instead.
            try:
                settings = json.loads(row["settings_json"] or "{}")
            except json.JSONDecodeError:
                settings = {}
            return None, "LATEST_PLAN_INVALID" if settings.get("source") != "AUTO_PREREQUISITE" else "NO_VALID_COLLECTION_DEFINITIONS"
        return {"plan": plan, "created_at": row["created_at"]}, None

    def _valid_structure(self, plan: dict, store_id: str) -> bool:
        if not isinstance(plan, dict) or plan.get("store_id") != store_id:
            return False
        collections = plan.get("collections")
        if not isinstance(collections, list) or not collections:
            return False
        keys = set()
        source_category_ids = []
        for item in collections:
            if not isinstance(item, dict) or not item.get("collection_key") or not item.get("title"):
                return False
            key = str(item["collection_key"])
            if key in keys or not isinstance(item.get("conditions"), list):
                return False
            source_category_id = item.get("source_category_id")
            if source_category_id is not None:
                source_category_ids.append(source_category_id)
            keys.add(key)
        if source_category_ids:
            placeholders = ",".join("?" for _ in source_category_ids)
            source_plan_id = plan.get("sourcing_plan_id")
            with connect(self.db) as con:
                found = {row[0] for row in con.execute(
                    f"SELECT c.id FROM store_sourcing_categories c JOIN store_sourcing_plans p ON p.plan_id=c.plan_id "
                    f"WHERE c.id IN ({placeholders}) AND p.store_id=? AND p.plan_id=?",
                    [*source_category_ids, store_id, source_plan_id])}
            if found != set(source_category_ids):
                return False
        return True

    def _has_local_evidence(self, store_id: str) -> tuple[bool, str]:
        try:
            profile = get_store(store_id, self.db)
        except KeyError:
            return False, "STORE_PROFILE_MISSING"
        with connect(self.db) as con:
            product_count = con.execute("SELECT COUNT(*) FROM products WHERE archived=0").fetchone()[0]
            source_categories = con.execute("""SELECT COUNT(*) FROM store_sourcing_categories c
                JOIN store_sourcing_plans p ON p.plan_id=c.plan_id WHERE p.store_id=? AND c.enabled=1""", (store_id,)).fetchone()[0]
        profile_categories = profile.get("sourcing_categories") or (profile.get("sourcing") or {}).get("categories") or []
        has_category = any(str(value or "").strip() for value in (
            profile.get("category"), profile.get("concept"), profile.get("primary_category")))
        if product_count or source_categories or profile_categories or has_category:
            return True, ""
        return False, "LOCAL_CATALOG_OR_CATEGORY_INPUT_REQUIRED"

    def _mark_auto_prerequisite(self, plan_id: str) -> None:
        with connect(self.db) as con:
            row = con.execute("SELECT settings_json FROM store_collection_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if not row:
                raise KeyError(plan_id)
            settings = json.loads(row["settings_json"] or "{}")
            settings["source"] = "AUTO_PREREQUISITE"
            settings["prerequisite_version"] = "4.1.3A"
            con.execute("UPDATE store_collection_plans SET settings_json=? WHERE plan_id=?",
                (json.dumps(settings, sort_keys=True), plan_id))


def ensure_collection_plan(store_id: str, *, allow_create: bool = True, db=None) -> CollectionPrerequisiteResult:
    return CollectionPrerequisiteService(db).ensure_collection_plan(store_id, allow_create=allow_create)


def prepare_homepage_prerequisites(store_id: str, *, store: dict | None = None,
                                   brand_profile: dict | None = None,
                                   maximum_categories: int = 8, db=None) -> dict[str, Any]:
    """Prepare brand, collection, homepage and prompt plans using local state only."""
    from .brand_automation import brand_profile_from_store, get_brand_profile
    from .homepage_automation import build_homepage_plan
    from .prompt_assets import PromptAssetService

    brand = brand_profile or get_brand_profile(store_id, db=db) or brand_profile_from_store(store_id, db=db)
    store = store or get_store(store_id, db)
    collection_result = CollectionPrerequisiteService(db).ensure_collection_plan(store_id)
    collection_plan = collection_result.plan
    homepage_collection_input = collection_plan or {"store_id": store_id, "plan_id": None, "collections": []}
    homepage_plan = build_homepage_plan(store_id=store_id, brand=brand,
        collection_plan=homepage_collection_input, maximum_categories=maximum_categories, db=db)
    prompt_set = PromptAssetService().build(store=store, brand=brand,
        collection_plan=collection_plan, homepage_plan=homepage_plan)
    return {"store_id": store_id, "brand": brand, "collection": collection_result,
            "collection_plan": collection_plan, "homepage_plan": homepage_plan,
            "prompt_set": prompt_set,
            "statuses": {"brand_profile": "READY", "collection_plan": collection_result.status,
                         "homepage_plan": "READY", "prompt_set": "READY"}}
