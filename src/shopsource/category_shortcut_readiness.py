"""Local-only readiness planning for four homepage category shortcuts.

This module reads the existing Shopify product cache and local collection/image
registries. It never creates or updates Shopify resources.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .db import connect, get_store, init_db
from .image_validation import inspect_image
from .homepage_featured_products import _install as install_featured_schema
from .merchandising_policy import PURPOSE_CATEGORY_SHORTCUTS, StoreMerchandisingPolicyService, match_policy_exclusion

CATALOG_MAX_AGE_DAYS = 7
_SAFE_HANDLE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_COLLECTION_GID = re.compile(r"gid://shopify/Collection/\d+\Z")
_PUBLICATION_GID = re.compile(r"gid://shopify/Publication/\d+\Z")
_GENERIC_SIGNAL_WORDS = frozenset({
    "a", "an", "and", "for", "with", "the", "to", "of", "in", "on", "by", "from",
    "new", "best", "premium", "quality", "product", "products", "item", "items", "set", "pack",
    "organizer", "organizers", "organization", "organizing", "storage", "accessory", "accessories",
    "collection", "shopify", "uncategorized",
})


def _json(value, fallback):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if fallback is None or isinstance(result, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")[:80] or "category"


def _product_text(product: dict) -> str:
    tags = product.get("tags") or []
    if not isinstance(tags, (list, tuple, set)):
        tags = [tags]
    return " ".join(str(value or "") for value in (
        product.get("title"), product.get("product_type"), product.get("category"),
        product.get("category_key"), product.get("collection_key"), *tags,
    )).casefold()


def _profile_exclusions(profile: dict) -> list[tuple[str, str]]:
    def values(value):
        if isinstance(value, str):
            return [value]
        return list(value) if isinstance(value, (list, tuple, set)) else []

    exclusions = [(str(value).casefold(), "STORE_PROFILE_EXCLUDE_KEYWORD")
                  for value in values(profile.get("exclude_keywords")) if str(value).strip()]
    exclusions.extend((str(value).casefold(), "STORE_PROFILE_MERCHANDISING_EXCLUSION")
                      for value in values(profile.get("category_shortcut_exclude_keywords")) if str(value).strip())
    exclusions.extend((str(value).casefold(), "STORE_PROFILE_MERCHANDISING_EXCLUSION")
                      for value in profile.get("merchandising_exclusions", []) if isinstance(value, str) and value.strip())
    for rule in profile.get("risk_rules", []) or []:
        if not isinstance(rule, dict) or str(rule.get("status") or "").upper() not in {"REVIEW", "RESTRICTED"}:
            continue
        exclusions.extend((str(term).casefold(), f"STORE_PROFILE_RISK_{str(rule.get('status')).upper()}")
                          for term in values(rule.get("terms")) if str(term).strip())
    return exclusions


def _is_eligible(product: dict, profile: dict | None = None) -> bool:
    if str(product.get("remote_status") or "").upper() != "ACTIVE":
        return False
    if product.get("eligible") is False or product.get("storefront_eligible") is False:
        return False
    if product.get("verification_status") != "REMOTE_READ_VERIFIED":
        return False
    if not (product.get("shopify_product_id") or product.get("source_key")):
        return False
    if any(str(product.get(key) or "").upper() in {"REVIEW", "REVIEW_REQUIRED", "RESTRICTED", "EXCLUDED"}
           for key in ("final_status", "decision_status", "risk_status")):
        return False
    text = _product_text(product)
    return not any(term and term in text for term, _reason in _profile_exclusions(profile or {}))


def _eligible_products(products: list[dict], profile: dict | None = None,
                       policy: dict | None = None) -> tuple[list[dict], dict[str, int]]:
    excluded = {"not_active_or_ineligible": 0, "unverified_or_missing_identity": 0,
                "store_profile_exclusion": 0, "decision_or_risk_exclusion": 0,
                "excluded_by_policy_count": 0, "policy_exclusion_reasons": {}}
    eligible, seen = [], set()
    exclusions = _profile_exclusions(profile or {})
    for product in products:
        identity = str(product.get("shopify_product_id") or product.get("source_key") or "")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        if (str(product.get("remote_status") or "").upper() != "ACTIVE" or
                product.get("eligible") is False or product.get("storefront_eligible") is False):
            excluded["not_active_or_ineligible"] += 1
            continue
        if product.get("verification_status") != "REMOTE_READ_VERIFIED":
            excluded["unverified_or_missing_identity"] += 1
            continue
        text = _product_text(product)
        if any(str(product.get(key) or "").upper() in {"REVIEW", "REVIEW_REQUIRED", "RESTRICTED", "EXCLUDED"}
               for key in ("final_status", "decision_status", "risk_status")):
            excluded["decision_or_risk_exclusion"] += 1
            continue
        if any(term and term in text for term, _reason in exclusions):
            excluded["store_profile_exclusion"] += 1
            continue
        policy_reason = match_policy_exclusion(product, policy) if policy else None
        if policy_reason:
            excluded["excluded_by_policy_count"] += 1
            reasons = excluded["policy_exclusion_reasons"]
            reasons[policy_reason] = reasons.get(policy_reason, 0) + 1
            continue
        eligible.append(product)
    return eligible, excluded


def _candidate_matches(product: dict, candidate: dict) -> bool:
    conditions = candidate.get("conditions") or []
    if conditions:
        from .collection_planner import condition_matches
        normalized = {**product, "category": product.get("category") or product.get("product_type") or ""}
        checks = [condition_matches(normalized, condition) for condition in conditions]
        return all(checks) if str(candidate.get("match_mode") or "ANY").upper() == "ALL" else any(checks)
    mapped = product.get("shopsource_collection_keys") or product.get("collection_keys") or product.get("collection_key")
    mapped_values = mapped if isinstance(mapped, (list, tuple, set)) else [mapped]
    mapped_values = {_slug(value) for value in mapped_values if value}
    if _slug(candidate.get("collection_key") or "") in mapped_values or _slug(candidate.get("category_key") or "") in mapped_values:
        return True
    text = _product_text(product)
    return any(str(signal).strip() and str(signal).casefold() in text
               for signal in candidate.get("match_signals", []))


def _title_from_signal(value: str) -> str:
    return " ".join(word.capitalize() for word in re.sub(r"[_-]+", " ", str(value or "")).split())


def _product_derived_candidates(products: list[dict]) -> list[dict]:
    """Conservative categories from repeated actual product taxonomy, tags, and title phrases."""
    buckets: dict[str, dict] = {}

    def add(value: str, source_field: str, product_identity: str):
        value = " ".join(str(value or "").strip().split())
        words = re.findall(r"[a-z0-9]+", value.casefold())
        if not words or all(word in _GENERIC_SIGNAL_WORDS for word in words):
            return
        # Taxonomy paths can be whole navigation trees, not useful card labels.
        if source_field == "PRODUCT_CATEGORY_KEY" and len(words) > 5:
            return
        key = _slug(value)
        if not key:
            return
        row = buckets.setdefault(key, {"category_key": key, "collection_key": key,
            "title": _title_from_signal(value), "candidate_source": "PRODUCT_DERIVED_FALLBACK",
            "source_field": source_field, "match_signals": [value], "conditions": [],
            "match_mode": "ANY", "_product_identities": set()})
        row["_product_identities"].add(product_identity)
        if value.casefold() not in {signal.casefold() for signal in row["match_signals"]}:
            row["match_signals"].append(value)

    for product in products:
        identity = str(product.get("shopify_product_id") or product.get("source_key") or "")
        for value in (product.get("category_key"), product.get("collection_key")):
            if value and str(value).casefold() != "uncategorized":
                add(value, "PRODUCT_CATEGORY_KEY", identity)
        add(product.get("product_type") or product.get("category"), "PRODUCT_TYPE", identity)
        tags = product.get("tags") or []
        if not isinstance(tags, (list, tuple, set)):
            tags = [tags]
        for value in tags:
            add(value, "MERCHANT_TAG", identity)
        words = [word for word in re.findall(r"[a-z0-9]+", str(product.get("title") or "").casefold())
                 if word not in _GENERIC_SIGNAL_WORDS]
        for size in (2, 3):
            for start in range(max(0, len(words) - size + 1)):
                add(" ".join(words[start:start + size]), "PRODUCT_TITLE_PHRASE", identity)
    candidates = list(buckets.values())
    for candidate in candidates:
        candidate["product_count"] = len(candidate.pop("_product_identities"))
        candidate["usefulness"] = 0
    candidates = [candidate for candidate in candidates if candidate["product_count"] > 0]
    candidates.sort(key=lambda row: (-row["product_count"], len(row["match_signals"][0]), row["title"].casefold()))
    return candidates


def category_image_prompt(title: str, store_profile: dict | None = None,
                          brand_profile: dict | None = None) -> str:
    store_profile = store_profile or {}
    brand = (brand_profile or {}).get("profile", brand_profile or {})
    primary_category = (brand.get("primary_category") or store_profile.get("primary_category") or
                        store_profile.get("category") or "the store's products")
    personality = brand.get("personality") or store_profile.get("brand_personality") or store_profile.get("brand_voice") or []
    keywords = brand.get("brand_keywords") or store_profile.get("brand_keywords") or []
    direction = (brand.get("visual_direction") or store_profile.get("visual_direction") or
                 store_profile.get("visual_identity") or "clean, practical, modern")
    palette = brand.get("colors") or store_profile.get("brand_colors")
    def compact(value):
        if isinstance(value, dict):
            return ", ".join(str(item) for item in value.values() if item)
        if isinstance(value, (list, tuple, set)):
            return ", ".join(str(item) for item in value if item)
        return str(value or "")
    context = ", ".join(part for part in (compact(personality), compact(keywords), compact(direction), compact(palette)) if part)
    return (f"Premium ecommerce category-card photograph for {title} in the context of {primary_category}; "
            f"show relevant products in realistic use, consistent with this store's visual direction: {context}. "
            "Square composition, natural light, clear subject, balanced negative space; no text, logos, or watermark.")


def readiness_summary(package: dict) -> dict:
    items = package.get("items") or []
    identity_ready = sum(item.get("mapping_identity_status") == "VERIFIED" for item in items)
    nonempty_remote_ready = sum(item.get("remote_count_status") == "VERIFIED" and
                                isinstance(item.get("remote_product_count"), int) and
                                item["remote_product_count"] > 0 for item in items)
    publication_ready = sum(item.get("publication_status") == "KNOWN_PUBLISHED" for item in items)
    mapping_ready = sum(item.get("mapping_status") == "READY" for item in items)
    image_ready = sum(item.get("image_status") == "READY" for item in items)
    theme_status = package.get("theme_schema_status", "WAITING_FOR_LIVE_READ")
    fallback_reviews = sum(item.get("candidate_status") == "REVIEW_REQUIRED" for item in items)
    sufficient = (len(items) == 4 and mapping_ready == 4 and image_ready == 4 and
                  fallback_reviews == 0 and theme_status == "READY")
    return {
        "selected_count": len(items), "candidates_selected": len(items), "mapping_ready": mapping_ready,
        "identity_ready": identity_ready, "collection_identity_verified": identity_ready,
        "nonempty_remote_ready": nonempty_remote_ready, "remote_nonempty_verified": nonempty_remote_ready,
        "publication_ready": publication_ready,
        "publication_unknown": sum(item.get("publication_status") == "UNKNOWN" for item in items),
        "image_ready": image_ready, "candidate_review_required": fallback_reviews,
        "theme_schema_status": theme_status, "theme_write_status": "NOT_RUN",
        "preview_enabled": sufficient,
    }


class CategoryShortcutReadinessService:
    """Prepare and persist a local readiness snapshot; no remote mutations."""

    def __init__(self, db=None):
        self.db = db
        init_db(db)
        install_featured_schema(db)
        with connect(db) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS homepage_category_shortcut_plans (
                plan_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, source_hash TEXT NOT NULL,
                catalog_fetched_at TEXT, plan_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS homepage_category_collection_remote_cache (
                store_id TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, source_hash TEXT NOT NULL,
                collections_json TEXT NOT NULL)""")

    def _store_context(self, store_id: str) -> tuple[dict, dict]:
        try:
            store_profile = get_store(str(store_id), self.db)
        except (KeyError, ValueError):
            store_profile = {"store_id": str(store_id), "store_name": str(store_id)}
        brand_profile = {}
        with connect(self.db) as con:
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='brand_profiles'").fetchone()
            row = con.execute("SELECT profile_json FROM brand_profiles WHERE store_id=?", (str(store_id),)).fetchone() if exists else None
        if row:
            brand_profile = _json(row["profile_json"], {})
        return store_profile, brand_profile

    def _plan_candidates(self, store_id: str, products: list[dict]) -> tuple[list[dict], bool]:
        with connect(self.db) as con:
            plan_rows = con.execute("SELECT plan_id,status FROM store_collection_plans WHERE store_id=? ORDER BY version DESC,created_at DESC",
                                    (str(store_id),)).fetchall()
            for plan in plan_rows:
                if str(plan["status"] or "").upper() in {"CANCELLED", "ARCHIVED", "DELETED"}:
                    continue
                definitions = con.execute("""SELECT * FROM store_collection_definitions
                    WHERE plan_id=? AND enabled=1 ORDER BY priority,id""", (plan["plan_id"],)).fetchall()
                if not definitions:
                    continue
                candidates = []
                for definition in definitions:
                    row = dict(definition)
                    conditions = [dict(condition) for condition in con.execute("""SELECT field,relation,value,group_operator
                        FROM store_collection_conditions WHERE collection_definition_id=? ORDER BY priority,id""",
                        (row["id"],)).fetchall()]
                    signals = [row.get("title", ""), row.get("collection_key", "")]
                    signals.extend(condition.get("value", "") for condition in conditions)
                    candidate = {
                        "category_key": row["collection_key"], "collection_key": row["collection_key"],
                        "title": row["title"], "proposed_handle": row.get("handle") or "",
                        "usefulness": max(0, 100 - int(row.get("priority") or 0)),
                        "candidate_source": "COLLECTION_PLAN", "source_plan_id": plan["plan_id"],
                        "conditions": conditions, "match_mode": row.get("match_mode") or "ANY",
                        "match_signals": [str(value) for value in signals if value],
                        "local_collection_definition": row,
                    }
                    candidate["product_count"] = sum(_candidate_matches(product, candidate) for product in products)
                    if candidate["product_count"] > 0:
                        candidates.append(candidate)
                if candidates:
                    return candidates, True
        return [], False

    @staticmethod
    def _profile_candidates(profile: dict, brand_profile: dict, products: list[dict]) -> tuple[list[dict], bool]:
        source = profile.get("category_shortcut_strategy") or profile.get("category_shortcuts")
        if isinstance(source, dict):
            source = source.get("categories") or source.get("candidates")
        if not isinstance(source, list):
            brand = (brand_profile or {}).get("profile", brand_profile or {})
            source = brand.get("category_shortcut_strategy") or brand.get("category_shortcuts")
        if isinstance(source, dict):
            source = source.get("categories") or source.get("candidates")
        if not isinstance(source, list):
            return [], False
        candidates, has_strategy = [], False
        for spec in source:
            if not isinstance(spec, dict):
                continue
            title = str(spec.get("title") or spec.get("name") or "").strip()
            collection_key = str(spec.get("collection_key") or spec.get("category_key") or _slug(title))
            if not title or not collection_key or spec.get("enabled") is False:
                continue
            has_strategy = True
            signals = spec.get("match_signals") or spec.get("signals") or spec.get("keywords") or []
            if isinstance(signals, str):
                signals = [signals]
            signals = [str(value) for value in [*signals, title] if value]
            candidate = {**spec, "category_key": str(spec.get("category_key") or collection_key),
                         "collection_key": collection_key, "title": title,
                         "candidate_source": "STORE_PROFILE", "conditions": spec.get("conditions") or [],
                         "match_signals": signals, "match_mode": spec.get("match_mode") or "ANY",
                         "usefulness": float(spec.get("usefulness") or 0)}
            candidate["product_count"] = sum(_candidate_matches(product, candidate) for product in products)
            if candidate["product_count"] > 0:
                candidates.append(candidate)
        return candidates, has_strategy

    def collection_snapshot(self, store_id: str) -> dict | None:
        """Return the last bounded read-only Shopify collection snapshot, if any."""
        with connect(self.db) as con:
            row = con.execute("SELECT fetched_at,source_hash,collections_json FROM homepage_category_collection_remote_cache WHERE store_id=?",
                              (str(store_id),)).fetchone()
        if not row:
            return None
        collections = _json(row["collections_json"], [])
        if not isinstance(collections, list):
            return None
        return {"fetched_at": row["fetched_at"], "source_hash": row["source_hash"], "collections": collections}

    def refresh_collection_snapshot(self, store_id: str) -> dict:
        """Refresh collection evidence using Shopify's read-only collections query only."""
        from .shopify_collections import COLLECTIONS_QUERY, ShopifyGraphQLClient, get_connection, get_shopify_token

        try:
            config = get_connection(str(store_id), db=self.db)
            token, _source = get_shopify_token(str(store_id), db=self.db)
            if not config or not token:
                return {"status": "WAITING_FOR_SHOPIFY_CONNECTION", "snapshot": self.collection_snapshot(store_id)}
            client = ShopifyGraphQLClient(config["shop_domain"], token, config.get("api_version"))
            result = client.execute(COLLECTIONS_QUERY)
            collections = result.get("collections", {}).get("nodes", [])
            normalized = []
            for row in collections if isinstance(collections, list) else []:
                products_count = row.get("productsCount") or {}
                count = products_count.get("count")
                normalized.append({
                    "id": row.get("id"), "handle": row.get("handle"), "title": row.get("title"),
                    "products_count": count if isinstance(count, int) and not isinstance(count, bool) else None,
                    "products_count_precision": products_count.get("precision"),
                })
            normalized.sort(key=lambda item: (str(item.get("id") or ""), str(item.get("handle") or "")))
            payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
            source_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with connect(self.db) as con:
                con.execute("INSERT OR REPLACE INTO homepage_category_collection_remote_cache(store_id,fetched_at,source_hash,collections_json) VALUES(?,?,?,?)",
                            (str(store_id), fetched_at, source_hash, payload))
            return {"status": "READ_ONLY_REFRESHED", "snapshot": {"fetched_at": fetched_at,
                    "source_hash": source_hash, "collections": normalized}}
        except Exception as exc:
            # Never expose exception messages: HTTP/library errors can contain request details.
            return {"status": "READ_ONLY_REFRESH_FAILED", "error_type": type(exc).__name__,
                    "snapshot": self.collection_snapshot(store_id)}

    def build(self, store_id: str, *, persist: bool = True, theme_snapshot: dict | None = None,
              simulate_draft: bool = False) -> dict:
        with connect(self.db) as con:
            cache = con.execute("SELECT fetched_at,source_hash,candidates_json FROM homepage_featured_product_remote_cache WHERE store_id=?", (str(store_id),)).fetchone()
            collection_rows = con.execute("SELECT collection_key,handle,shopify_collection_id,published_ids_json,last_synced_at FROM shopify_collection_mappings WHERE store_id=?", (str(store_id),)).fetchall() if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shopify_collection_mappings'").fetchone() else []
            image_rows = con.execute("SELECT collection_key,path,approval_status,alt_text,metadata_json FROM collection_image_assets WHERE store_id=?", (str(store_id),)).fetchall() if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_image_assets'").fetchone() else []
            homepage_image_rows = con.execute("SELECT asset_id,asset_type,local_path,approval_status,shopify_file_id,shopify_url FROM store_homepage_assets WHERE store_id=?", (str(store_id),)).fetchall() if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_homepage_assets'").fetchone() else []
            brand_image_rows = con.execute("SELECT asset_id,asset_type,local_path,approval_status,shopify_file_id,shopify_url,metadata_json FROM brand_assets WHERE store_id=?", (str(store_id),)).fetchall() if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='brand_assets'").fetchone() else []
        policy_service = StoreMerchandisingPolicyService(self.db)
        active_policy = policy_service.effective_policy(str(store_id), PURPOSE_CATEGORY_SHORTCUTS)
        policy_evidence = {
            "active_policy_id": active_policy.get("policy_id") if active_policy else None,
            "active_policy_version": active_policy.get("version") if active_policy else None,
            "policy_source": active_policy.get("source") if active_policy else None,
            "policy_status": "APPROVED" if active_policy else "REVIEW_REQUIRED",
            "excluded_by_policy_count": 0,
            "policy_exclusion_reasons": {},
        }
        if not cache:
            return {"status": "WAITING_FOR_CATALOG", "store_id": str(store_id), "catalog_fetched_at": None,
                    **policy_evidence,
                    "distribution": {}, "items": [], "summary": readiness_summary({"items": []}),
                    "blockers": ["No cached ACTIVE Shopify product catalog is available; refresh with read_products only."]}
        products = _json(cache["candidates_json"], [])
        store_profile, brand_profile = self._store_context(store_id)
        active_policy_data = active_policy.get("policy") if active_policy else None
        eligible_products, excluded = _eligible_products(products, store_profile, active_policy_data)
        now = datetime.now(timezone.utc)
        try:
            age_days = max(0, (now - datetime.fromisoformat(cache["fetched_at"].replace("Z", "+00:00"))).total_seconds() / 86400)
        except (ValueError, AttributeError, TypeError):
            age_days = None
        if age_days is None or age_days > CATALOG_MAX_AGE_DAYS:
            return {"status": "STALE_CATALOG", "store_id": str(store_id), "catalog_fetched_at": cache["fetched_at"],
                    **policy_evidence,
                    "catalog_age_days": age_days, "distribution": {}, "items": [],
                    "summary": readiness_summary({"items": []}),
                    "blockers": ["Cached catalog is stale; a read_products refresh is needed before selecting categories."]}

        mappings = {row["collection_key"]: dict(row) for row in collection_rows}
        remote_snapshot = self.collection_snapshot(store_id)
        remote_collections = (remote_snapshot or {}).get("collections", [])
        images = {row["collection_key"]: dict(row) for row in image_rows}
        for row in [*homepage_image_rows, *brand_image_rows]:
            candidate = dict(row)
            metadata = _json(candidate.get("metadata_json"), {})
            key = (metadata.get("collection_key") or metadata.get("category_key")) if isinstance(metadata, dict) else None
            if key and key not in images and str(candidate.get("asset_type") or "").upper() in {"COLLECTION_IMAGE", "CATEGORY_SHORTCUT"}:
                images[str(key)] = {"path": candidate.get("local_path"), "approval_status": candidate.get("approval_status"),
                                    "alt_text": metadata.get("alt_text", "") if isinstance(metadata, dict) else "",
                                    "metadata_json": candidate.get("metadata_json"), "asset_id": candidate.get("asset_id")}
        from .category_strategy import HomepageCategoryStrategyService
        strategy_service = HomepageCategoryStrategyService(self.db)
        strategy_candidates, selected_strategy = strategy_service.candidates(
            str(store_id), eligible_products, simulate_draft=simulate_draft)
        latest_strategy = strategy_service.latest(str(store_id), include_draft=True)
        plan_candidates, _has_collection_plan = self._plan_candidates(store_id, eligible_products)
        candidate_pools = [strategy_candidates] if strategy_candidates else [plan_candidates]
        known_candidate_keys = {str(item.get("collection_key") or "") for item in candidate_pools[0]}
        if strategy_candidates:
            # An approved strategy has exactly four reviewed entries and is authoritative.
            plan_candidates = []
        if len(known_candidate_keys) < 4:
            profile_candidates, _has_profile_strategy = self._profile_candidates(
                store_profile, brand_profile, eligible_products)
            candidate_pools.append(profile_candidates)
            known_candidate_keys.update(str(item.get("collection_key") or "") for item in profile_candidates)
        if len(known_candidate_keys) < 4:
            candidate_pools.append(_product_derived_candidates(eligible_products))
        selected, selected_keys, sources_used = [], set(), []
        for pool in candidate_pools:
            if pool and all(item.get("candidate_source") == "CATEGORY_STRATEGY" for item in pool):
                pool.sort(key=lambda item: (int(item.get("priority") or 999), str(item.get("collection_key") or "")))
            else:
                pool.sort(key=lambda item: (-item.get("product_count", 0),
                                            -float(item.get("usefulness") or 0),
                                            int((item.get("local_collection_definition") or {}).get("priority") or 0),
                                            str(item.get("title") or "").casefold(),
                                            str(item.get("collection_key") or "")))
            for candidate in pool:
                candidate_key = str(candidate.get("collection_key") or "")
                if not candidate_key or candidate_key in selected_keys:
                    continue
                selected.append(candidate)
                selected_keys.add(candidate_key)
                if candidate.get("candidate_source") not in sources_used:
                    sources_used.append(candidate["candidate_source"])
                if len(selected) == 4:
                    break
            if len(selected) == 4:
                break
        candidate_source = "+".join(sources_used) if sources_used else "NO_CANDIDATES"
        items = []
        used_asset_paths: set[str] = set()
        store_name = str(store_profile.get("store_name") or store_profile.get("name") or store_id)
        for position, item in enumerate(selected, 1):
            key = item["collection_key"]
            mapping = mappings.get(key) or {}
            handle = mapping.get("handle")
            collection_id = mapping.get("shopify_collection_id")
            publication_ids = _json(mapping.get("published_ids_json"), [])
            publication_ids = [value for value in publication_ids if isinstance(value, str) and _PUBLICATION_GID.fullmatch(value)] if isinstance(publication_ids, list) else []
            publication_status = "KNOWN_PUBLISHED" if publication_ids else "UNKNOWN"
            has_local_identity = bool(handle or collection_id)
            valid_local_identity = bool(_SAFE_HANDLE.fullmatch(str(handle or "")) and _COLLECTION_GID.fullmatch(str(collection_id or "")))
            remote_collection = next((row for row in remote_collections
                                      if row.get("id") == collection_id), None) if valid_local_identity else None
            identity_status = "VERIFIED" if remote_collection and remote_collection.get("handle") == handle else (
                "IDENTITY_MISMATCH" if remote_collection else ("REMOTE_NOT_VERIFIED" if valid_local_identity else
                "INVALID_LOCAL_IDENTITY" if has_local_identity else "NOT_MAPPED"))
            remote_count = remote_collection.get("products_count") if identity_status == "VERIFIED" else None
            remote_precision = remote_collection.get("products_count_precision") if identity_status == "VERIFIED" else None
            remote_count_status = "VERIFIED" if isinstance(remote_count, int) and not isinstance(remote_count, bool) else "UNKNOWN"
            mapping_valid = (identity_status == "VERIFIED" and remote_count_status == "VERIFIED" and
                             remote_count > 0 and publication_status == "KNOWN_PUBLISHED")
            mapping_status = "READY" if mapping_valid else ("REMOTE_EMPTY" if remote_count_status == "VERIFIED" and remote_count == 0 else
                "REMOTE_COUNT_UNKNOWN" if identity_status == "VERIFIED" and remote_count_status == "UNKNOWN" else
                "IDENTITY_MISMATCH" if identity_status == "IDENTITY_MISMATCH" else
                "REMOTE_NOT_VERIFIED" if identity_status == "REMOTE_NOT_VERIFIED" else
                "NOT_MAPPED" if identity_status == "NOT_MAPPED" else "NOT_READY")
            target_url = f"/collections/{handle}" if identity_status == "VERIFIED" else None
            asset = images.get(key) or {}
            path = asset.get("path")
            image_status, inspection = "NEEDS_ASSET", None
            asset_path = str(Path(path).resolve()) if path else None
            if asset.get("approval_status") == "APPROVED" and asset_path and asset_path not in used_asset_paths:
                inspection = inspect_image(asset_path, asset_type="CATEGORY_SHORTCUT")
                metadata = _json(asset.get("metadata_json"), {})
                review = metadata.get("content_review") if isinstance(metadata, dict) else None
                content_ok = isinstance(review, dict) and all(review.get(flag) is True for flag in ("no_text", "no_logo", "no_watermark"))
                if (inspection.get("valid") and inspection.get("width", 0) >= 800 and inspection.get("height", 0) >= 800
                        and .75 <= inspection.get("aspect_ratio", 0) <= 1.25 and content_ok):
                    image_status = "READY"
                    used_asset_paths.add(asset_path)
            plan_definition = item.get("local_collection_definition") or {}
            proposed_handle = item.get("proposed_handle") or re.sub(
                r"[^a-z0-9]+", "-", f"{store_name} {item['title']}".casefold()).strip("-")[:80]
            if not _SAFE_HANDLE.fullmatch(proposed_handle):
                proposed_handle = re.sub(r"[^a-z0-9]+", "-", f"{store_name} {item['title']}".casefold()).strip("-")[:80]
            item_candidate_source = item.get("candidate_source", candidate_source)
            selection_reason = (f"{item.get('product_count', 0)} eligible products matched {item_candidate_source} evidence")
            items.append({
                "shortcut_key": item.get("category_key") or key, "category_key": item.get("category_key") or key,
                "title": item["title"], "merchandising_group": item["title"],
                "candidate_source": item_candidate_source,
                "product_count": item["product_count"], "selection_reason": selection_reason, "collection_key": key,
                "conditions": item.get("conditions") or [], "match_signals": item.get("match_signals") or [],
                "shopify_collection_id": collection_id if identity_status == "VERIFIED" else None,
                "handle": handle if identity_status == "VERIFIED" else None, "storefront_url": target_url,
                "mapping_status": mapping_status, "mapping_identity_status": identity_status,
                "publication_ids": publication_ids, "publication_status": publication_status,
                "remote_product_count": remote_count, "remote_product_count_precision": remote_precision,
                "remote_count_status": remote_count_status,
                "local_collection_definition": plan_definition or None,
                "proposed_collection_definition": {"collection_key": key, "title": item["title"],
                    "estimated_active_product_count": item["product_count"], "proposed_handle": proposed_handle,
                    "remote_id": None, "status": "LOCAL_PROPOSAL_ONLY"},
                "proposed_handle": proposed_handle,
                "candidate_status": "REVIEW_REQUIRED" if item_candidate_source == "PRODUCT_DERIVED_FALLBACK" else "STORE_DATA",
                "image_asset_id": asset.get("asset_id") if image_status == "READY" else None,
                "image_asset_path": asset_path if image_status == "READY" else None,
                "image_status": image_status, "image_inspection": inspection,
                "alt_text": asset.get("alt_text") if image_status == "READY" else f"{item['title']} products in realistic use",
                "image_prompt": item.get("image_prompt") or category_image_prompt(item["title"], store_profile, brand_profile), "position": position,
                "readiness_reasons": (["Remote collection identity and productsCount evidence are required; publication IDs are not product counts."] if mapping_status != "READY" else []) +
                    (["Approved square image with verified no-text/no-logo/no-watermark content review is missing."] if image_status != "READY" else []),
            })
        package_id = "CSP_" + hashlib.sha256(f"{store_id}:{cache['source_hash']}:{json.dumps(items,sort_keys=True)}".encode()).hexdigest()[:20]
        theme_info = None
        theme_status = "WAITING_FOR_LIVE_READ"
        if isinstance(theme_snapshot, dict) and theme_snapshot.get("status") == "CONNECTED":
            from .homepage_automation import discover_homepage_sections
            found = discover_homepage_sections(theme_snapshot.get("theme_files") or {})
            category_schema = found.get("category") or {}
            theme_status = "READY" if category_schema else "CATEGORY_SCHEMA_NOT_FOUND"
            theme_info = {"status": theme_status, "filename": category_schema.get("filename"),
                          "type": category_schema.get("type"), "mode": category_schema.get("mode"),
                          "discovery_status": found.get("category_status")}
        distribution = {item["collection_key"]: {"title": item["title"], "product_count": item["product_count"],
                         "candidate_source": item.get("candidate_source", candidate_source),
                         "status": "REVIEW_REQUIRED" if item.get("candidate_source") == "PRODUCT_DERIVED_FALLBACK" else "STORE_DATA"}
                       for item in selected}
        package = {"plan_id": package_id, "store_id": str(store_id), "store_name": store_name,
                   "candidate_source": candidate_source,
                   "active_strategy_id": selected_strategy.get("strategy_id") if selected_strategy else None,
                   "active_strategy_version": selected_strategy.get("version") if selected_strategy else None,
                   "strategy_status": selected_strategy.get("status") if selected_strategy else "NONE",
                   "strategy_source": selected_strategy.get("source") if selected_strategy else None,
                   "latest_strategy_id": latest_strategy.get("strategy_id") if latest_strategy else None,
                   "latest_strategy_version": latest_strategy.get("version") if latest_strategy else None,
                   "latest_strategy_status": latest_strategy.get("status") if latest_strategy else "NONE",
                   "draft_simulation": bool(simulate_draft and selected_strategy and selected_strategy.get("status") == "DRAFT"),
                   "active_policy_id": active_policy.get("policy_id") if active_policy else None,
                   "active_policy_version": active_policy.get("version") if active_policy else None,
                   "policy_source": active_policy.get("source") if active_policy else None,
                   "policy_status": "APPROVED" if active_policy else "REVIEW_REQUIRED",
                   "excluded_by_policy_count": excluded["excluded_by_policy_count"],
                   "policy_exclusion_reasons": excluded["policy_exclusion_reasons"],
                   "status": "CATEGORIES_SELECTED_PREREQUISITES_BLOCKED" if len(items) == 4 else "INSUFFICIENT_CATEGORIES",
                   "catalog_fetched_at": cache["fetched_at"], "catalog_age_days": round(age_days, 3),
                   "catalog_product_count": len(products), "eligible_product_count": len(eligible_products),
                   "excluded_counts": excluded, "distribution": distribution,
                   "items": items, "theme_schema_status": theme_status, "theme_schema": theme_info, "theme_write_status": "NOT_RUN",
                   "mapping_refresh_status": "READ_ONLY_SNAPSHOT" if remote_snapshot else "REMOTE_EVIDENCE_UNAVAILABLE",
                   "collection_snapshot_fetched_at": (remote_snapshot or {}).get("fetched_at"), "summary": {},
                   "blockers": ["Shopify theme schema live read pending.", "Theme apply remains blocked pending themeFilesUpsert exemption."]}
        package["summary"] = readiness_summary(package)
        if persist:
            with connect(self.db) as con:
                con.execute("INSERT OR REPLACE INTO homepage_category_shortcut_plans(plan_id,store_id,source_hash,catalog_fetched_at,plan_json,created_at) VALUES(?,?,?,?,?,?)",
                            (package_id, str(store_id), cache["source_hash"], cache["fetched_at"], json.dumps(package, ensure_ascii=False, sort_keys=True), now.isoformat(timespec="seconds")))
        return package

    def latest(self, store_id: str) -> dict | None:
        with connect(self.db) as con:
            row = con.execute("SELECT plan_json FROM homepage_category_shortcut_plans WHERE store_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (str(store_id),)).fetchone()
        return _json(row[0], None) if row else None
