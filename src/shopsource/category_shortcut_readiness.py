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

from .db import connect, init_db
from .image_validation import inspect_image
from .homepage_featured_products import _install as install_featured_schema

CATALOG_MAX_AGE_DAYS = 7
CATEGORY_DEFINITIONS = (
    {"key": "trunk-cargo", "title": "Trunk & Cargo", "collection_key": "trunk-storage", "usefulness": 90,
     "patterns": (r"\btrunk\b", r"\bcargo\b", r"boot organizer", r"car boot")},
    {"key": "seat-backseat", "title": "Seat & Backseat", "collection_key": "seat-organization", "usefulness": 92,
     "patterns": (r"back ?seat", r"seat ?back", r"seatback", r"seat organizer")},
    {"key": "console-small-storage", "title": "Console & Small Storage", "collection_key": "console-storage", "usefulness": 84,
     "patterns": (r"center console", r"\bconsole\b", r"small storage")},
    {"key": "trash-cleanup", "title": "Trash & Cleanup", "collection_key": "trash-cleanup", "usefulness": 88,
     "patterns": (r"\btrash\b", r"\bgarbage\b", r"\bwaste\b", r"\blitter\b", r"clean.?up")},
    {"key": "cup-holder", "title": "Cup Holder & Convenience", "collection_key": "cup-holder", "usefulness": 82,
     "patterns": (r"cup.?holders?", r"cupholder")},
    {"key": "document-visor", "title": "Document & Visor", "collection_key": "document-visor", "usefulness": 65,
     "patterns": (r"\bvisor\b", r"registration holder", r"document holder")},
    {"key": "travel-organization", "title": "Travel Organization", "collection_key": "travel-storage", "usefulness": 68,
     "patterns": (r"travel organizer", r"travel storage", r"travel organization")},
    {"key": "general-organization", "title": "General Organization", "collection_key": "general-organization", "usefulness": 60,
     "patterns": (r"organizer", r"organization", r"storage")},
)
_REPAIR_PART = re.compile(
    r"\b(replacement|repair|oem|direct[ -]?fit|fitment|trim|panel|interior part|replacement part)\b|"
    r"\b(?:19|20)\d{2}\s*(?:-|to)\s*(?:19|20)\d{2}\b|\bfits?\s+(?:19|20)\d{2}\b",
    re.I,
)
_SAFE_HANDLE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_COLLECTION_GID = re.compile(r"gid://shopify/Collection/\d+\Z")
_PUBLICATION_GID = re.compile(r"gid://shopify/Publication/\d+\Z")


def _json(value, fallback):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if fallback is None or isinstance(result, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def _category_match(product: dict) -> str | None:
    """ShopSource collection mapping, taxonomy/productType, tags, then title."""
    mapped = product.get("shopsource_collection_keys") or product.get("collection_keys") or product.get("collection_key")
    mapped_values = mapped if isinstance(mapped, (list, tuple, set)) else [mapped]
    mapped_values = {str(value).casefold() for value in mapped_values if value}
    for definition in CATEGORY_DEFINITIONS:
        if definition["collection_key"].casefold() in mapped_values or definition["key"].casefold() in mapped_values:
            return definition["key"]
    ordered = (
        " ".join(str(product.get(key) or "") for key in ("category_key", "product_type")),
        " ".join(map(str, product.get("tags") or [])),
        str(product.get("title") or ""),
    )
    for source in ordered:
        folded = source.casefold()
        for definition in CATEGORY_DEFINITIONS[:-1]:
            if any(re.search(pattern, folded, re.I) for pattern in definition["patterns"]):
                return definition["key"]
    combined = " ".join(ordered).casefold()
    if any(re.search(pattern, combined, re.I) for pattern in CATEGORY_DEFINITIONS[-1]["patterns"]):
        return "general-organization"
    return None


def _is_eligible(product: dict) -> bool:
    if str(product.get("remote_status") or "").upper() != "ACTIVE":
        return False
    if product.get("eligible") is False or product.get("storefront_eligible") is False:
        return False
    if product.get("verification_status") != "REMOTE_READ_VERIFIED":
        return False
    if not (product.get("shopify_product_id") or product.get("source_key")):
        return False
    evidence = " ".join(str(product.get(key) or "") for key in ("title", "product_type", "tags"))
    return not bool(_REPAIR_PART.search(evidence))


def _catalog_counts(products: list[dict]) -> tuple[dict[str, int], dict[str, int]]:
    counts = {item["key"]: 0 for item in CATEGORY_DEFINITIONS}
    excluded = {"not_active_or_ineligible": 0, "unverified_or_missing_identity": 0,
                "repair_or_fitment_part": 0, "uncategorized": 0}
    seen: set[str] = set()
    for product in products:
        identity = str(product.get("shopify_product_id") or product.get("source_key") or "")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        if str(product.get("remote_status") or "").upper() != "ACTIVE" or product.get("eligible") is False or product.get("storefront_eligible") is False:
            excluded["not_active_or_ineligible"] += 1
            continue
        if product.get("verification_status") != "REMOTE_READ_VERIFIED" or not identity:
            excluded["unverified_or_missing_identity"] += 1
            continue
        if not _is_eligible(product):
            excluded["repair_or_fitment_part"] += 1
            continue
        category = _category_match(product)
        if category:
            counts[category] += 1
        else:
            excluded["uncategorized"] += 1
    return counts, excluded


def category_image_prompt(title: str) -> str:
    return (f"Premium automotive organization category-card photograph for {title}; clean modern car interior, "
            "realistic useful product in context, neutral natural light, consistent square composition, "
            "no text, no logos, no watermark, no vehicle-specific replacement parts.")


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
    sufficient = len(items) == 4 and mapping_ready == 4 and image_ready == 4 and theme_status == "READY"
    return {
        "selected_count": len(items), "mapping_ready": mapping_ready,
        "identity_ready": identity_ready, "nonempty_remote_ready": nonempty_remote_ready,
        "publication_ready": publication_ready,
        "publication_unknown": sum(item.get("publication_status") == "UNKNOWN" for item in items),
        "image_ready": image_ready,
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

    def build(self, store_id: str, *, persist: bool = True, theme_snapshot: dict | None = None) -> dict:
        with connect(self.db) as con:
            cache = con.execute("SELECT fetched_at,source_hash,candidates_json FROM homepage_featured_product_remote_cache WHERE store_id=?", (str(store_id),)).fetchone()
            collection_rows = con.execute("SELECT collection_key,handle,shopify_collection_id,published_ids_json,last_synced_at FROM shopify_collection_mappings WHERE store_id=?", (str(store_id),)).fetchall() if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shopify_collection_mappings'").fetchone() else []
            image_rows = con.execute("SELECT collection_key,path,approval_status,alt_text,metadata_json FROM collection_image_assets WHERE store_id=?", (str(store_id),)).fetchall() if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_image_assets'").fetchone() else []
            homepage_image_rows = con.execute("SELECT asset_id,asset_type,local_path,approval_status,shopify_file_id,shopify_url FROM store_homepage_assets WHERE store_id=?", (str(store_id),)).fetchall() if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_homepage_assets'").fetchone() else []
            brand_image_rows = con.execute("SELECT asset_id,asset_type,local_path,approval_status,shopify_file_id,shopify_url,metadata_json FROM brand_assets WHERE store_id=?", (str(store_id),)).fetchall() if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='brand_assets'").fetchone() else []
            definition_rows = con.execute("""SELECT d.collection_key,d.title,d.handle,d.estimated_product_count,d.warning_json
                FROM store_collection_plans p JOIN store_collection_definitions d ON d.plan_id=p.plan_id
                WHERE p.store_id=? ORDER BY p.created_at DESC,d.priority,d.id""", (str(store_id),)).fetchall() if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_collection_plans'").fetchone() else []
        if not cache:
            return {"status": "WAITING_FOR_CATALOG", "store_id": str(store_id), "catalog_fetched_at": None,
                    "distribution": {}, "items": [], "summary": readiness_summary({"items": []}),
                    "blockers": ["No cached ACTIVE Shopify product catalog is available; refresh with read_products only."]}
        products = _json(cache["candidates_json"], [])
        counts, excluded = _catalog_counts(products)
        now = datetime.now(timezone.utc)
        try:
            age_days = max(0, (now - datetime.fromisoformat(cache["fetched_at"].replace("Z", "+00:00"))).total_seconds() / 86400)
        except (ValueError, AttributeError, TypeError):
            age_days = None
        if age_days is None or age_days > CATALOG_MAX_AGE_DAYS:
            return {"status": "STALE_CATALOG", "store_id": str(store_id), "catalog_fetched_at": cache["fetched_at"],
                    "catalog_age_days": age_days, "distribution": counts, "items": [],
                    "summary": readiness_summary({"items": []}),
                    "blockers": ["Cached catalog is stale; a read_products refresh is needed before selecting categories."]}

        mappings = {row["collection_key"]: dict(row) for row in collection_rows}
        remote_snapshot = self.collection_snapshot(store_id)
        remote_collections = (remote_snapshot or {}).get("collections", [])
        definitions = {}
        for row in definition_rows:
            definitions.setdefault(row["collection_key"], dict(row))
        images = {row["collection_key"]: dict(row) for row in image_rows}
        for row in [*homepage_image_rows, *brand_image_rows]:
            candidate = dict(row)
            metadata = _json(candidate.get("metadata_json"), {})
            key = (metadata.get("collection_key") or metadata.get("category_key")) if isinstance(metadata, dict) else None
            if key and key not in images and str(candidate.get("asset_type") or "").upper() in {"COLLECTION_IMAGE", "CATEGORY_SHORTCUT"}:
                images[str(key)] = {"path": candidate.get("local_path"), "approval_status": candidate.get("approval_status"),
                                    "alt_text": metadata.get("alt_text", "") if isinstance(metadata, dict) else "",
                                    "metadata_json": candidate.get("metadata_json"), "asset_id": candidate.get("asset_id")}
        # Broad usefulness and use-case diversity break similar counts; count is the primary rank.
        ranked = [item for item in CATEGORY_DEFINITIONS if counts[item["key"]] > 0]
        ranked.sort(key=lambda item: (-counts[item["key"]], -item["usefulness"], item["key"]))
        selected = ranked[:4]
        items = []
        used_asset_paths: set[str] = set()
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
            plan_definition = definitions.get(key) or {}
            proposed_handle = plan_definition.get("handle") or re.sub(r"[^a-z0-9]+", "-", f"cabin-tidy-{item['title']}").strip("-")
            items.append({
                "shortcut_key": item["key"], "title": item["title"], "merchandising_group": item["title"],
                "product_count": counts[item["key"]], "collection_key": key,
                "shopify_collection_id": collection_id if identity_status == "VERIFIED" else None,
                "handle": handle if identity_status == "VERIFIED" else None, "storefront_url": target_url,
                "mapping_status": mapping_status, "mapping_identity_status": identity_status,
                "publication_ids": publication_ids, "publication_status": publication_status,
                "remote_product_count": remote_count, "remote_product_count_precision": remote_precision,
                "remote_count_status": remote_count_status,
                "local_collection_definition": plan_definition or None,
                "proposed_collection_definition": {"collection_key": key, "title": item["title"],
                    "estimated_active_product_count": counts[item["key"]], "proposed_handle": proposed_handle,
                    "remote_id": None, "status": "LOCAL_PROPOSAL_ONLY"}, "proposed_handle": proposed_handle,
                "image_asset_id": asset.get("asset_id") if image_status == "READY" else None,
                "image_asset_path": asset_path if image_status == "READY" else None,
                "image_status": image_status, "image_inspection": inspection,
                "alt_text": asset.get("alt_text") if image_status == "READY" else f"{item['title']} organization in a clean car interior",
                "image_prompt": category_image_prompt(item["title"]), "position": position,
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
        package = {"plan_id": package_id, "store_id": str(store_id), "status": "CATEGORIES_SELECTED_PREREQUISITES_BLOCKED" if len(items) == 4 else "INSUFFICIENT_CATEGORIES",
                   "catalog_fetched_at": cache["fetched_at"], "catalog_age_days": round(age_days, 3),
                   "catalog_product_count": len(products), "excluded_counts": excluded,
                   "distribution": {item["key"]: {"title": item["title"], "product_count": counts[item["key"]]} for item in CATEGORY_DEFINITIONS},
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
