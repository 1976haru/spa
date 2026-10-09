"""Homepage hero and collection-shortcut planning with guarded theme patches.

Plans are local metadata only. Theme mutation is isolated behind an injected
client and requires a fresh high-confidence preview, approved assets, write
scope, and an explicit confirmation.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect, init_db
from .paths import EXPORT_DIR
from .shopify_collections import ShopifyGraphQLClient, get_connection, get_shopify_token
from .shopify_theme_json import (
    ShopifyJsonDocument,
    ShopifyJsonDocumentError,
    parse_shopify_json_document,
    render_shopify_json_document,
    shopify_json_semantic_hash,
)
from .shopify_theme_ids import legacy_managed_id, legacy_shopify_instance_kind, shopify_instance_id

UPSERT_THEME_FILES = "mutation HomepageFiles($themeId: ID!, $files: [OnlineStoreThemeFilesUpsertFileInput!]!) { themeFilesUpsert(themeId: $themeId, files: $files) { upsertedThemeFiles { filename } userErrors { field message } } }"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _snapshot_document(snapshot: dict) -> ShopifyJsonDocument | None:
    filename = snapshot.get("template_filename")
    raw = (snapshot.get("theme_files") or {}).get(filename) if filename else None
    if raw is None:
        return None
    return parse_shopify_json_document(raw)


def _document_preview_metadata(snapshot: dict, proposed: dict | None = None) -> dict:
    filename = snapshot.get("template_filename")
    try:
        document = _snapshot_document(snapshot)
    except ShopifyJsonDocumentError as exc:
        return {"filename": filename, "status": exc.code, "error": exc.as_dict()}
    if document is None:
        return {"filename": filename, "status": snapshot.get("template_status", "TEMPLATE_BODY_MISSING")}
    return {
        "filename": filename,
        "status": "READY",
        "before_raw_hash": document.raw_hash,
        "before_semantic_hash": document.semantic_hash,
        "proposed_raw_hash": _raw_hash(render_shopify_json_document(document, proposed)) if proposed is not None else None,
        "had_leading_comment": document.had_leading_comment,
        "prefix_hash": _raw_hash(document.prefix),
        "suffix_hash": _raw_hash(document.suffix),
    }


def _raw_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _remote_theme_document(theme: dict, filename: str) -> tuple[str, ShopifyJsonDocument]:
    rows = ((theme.get("files") or {}).get("nodes") or [])
    row = next((item for item in rows if item.get("filename") == filename), None)
    if not row:
        raise ShopifyJsonDocumentError("TEMPLATE_BODY_MISSING", f"Theme file {filename} was not returned.")
    body = row.get("body") or {}
    raw = body.get("content")
    if raw is None and body.get("contentBase64"):
        try:
            raw = base64.b64decode(body["contentBase64"], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raw = None
    if raw is None:
        raise ShopifyJsonDocumentError("TEMPLATE_BODY_MISSING", f"Theme file {filename} body is not readable text.")
    return raw, parse_shopify_json_document(raw)


def _read_exact_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def normalize_text_value(value: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> str:
    """Safely flatten profile values used as prompt/display text without mutation."""
    if value is None or _depth > 8:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    seen = _seen if _seen is not None else set()
    ident = id(value)
    if ident in seen:
        return ""
    if isinstance(value, dict):
        seen.add(ident)
        parts = [f"{normalize_text_value(key, _depth=_depth+1, _seen=seen)}: {normalize_text_value(item, _depth=_depth+1, _seen=seen)}"
                 for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))]
        seen.discard(ident)
        return ", ".join(part for part in parts if part and not part.endswith(": "))
    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(ident)
        items = sorted(value, key=lambda item: str(item)) if isinstance(value, (set, frozenset)) else value
        parts = [normalize_text_value(item, _depth=_depth+1, _seen=seen) for item in items]
        seen.discard(ident)
        return ", ".join(part for part in parts if part)
    return str(value).strip()


def _install(db=None):
    init_db(db)
    with connect(db) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS store_homepage_plans (
          plan_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, brand_profile_id TEXT,
          collection_plan_id TEXT, planner_version TEXT NOT NULL, status TEXT NOT NULL,
          source_hash TEXT NOT NULL, plan_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_homepage_plan_store ON store_homepage_plans(store_id, created_at);
        CREATE TABLE IF NOT EXISTS store_homepage_previews (
          preview_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, store_id TEXT NOT NULL,
          theme_id TEXT NOT NULL, source_hash TEXT NOT NULL, status TEXT NOT NULL,
          preview_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS store_homepage_backups (
          backup_id TEXT PRIMARY KEY, preview_id TEXT NOT NULL, store_id TEXT NOT NULL,
          theme_id TEXT NOT NULL, filename TEXT NOT NULL, folder TEXT NOT NULL,
          before_json TEXT NOT NULL, proposed_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS store_homepage_managed_state (
          store_id TEXT NOT NULL, theme_id TEXT NOT NULL, section_id TEXT NOT NULL,
          section_json TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(store_id,theme_id,section_id)
        );
        CREATE TABLE IF NOT EXISTS store_homepage_assets (
          asset_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, plan_id TEXT NOT NULL, asset_type TEXT NOT NULL,
          local_path TEXT NOT NULL, provider TEXT NOT NULL, sha256 TEXT NOT NULL, width INTEGER NOT NULL,
          height INTEGER NOT NULL, format TEXT NOT NULL, approval_status TEXT NOT NULL,
          shopify_file_id TEXT, shopify_url TEXT, created_at TEXT NOT NULL
        );
        """)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-") or "category"


def _safe_store(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(value))


def _section_schema(raw: str) -> dict | None:
    if not isinstance(raw, str): return None
    match = re.search(r"\{%[- ]*schema[- ]*%\}(.*?)\{%[- ]*endschema[- ]*%\}", raw, re.S | re.I)
    if not match:
        return None
    try:
        data = json.loads(match.group(1).strip())
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _field_semantics(schema: dict) -> dict[str, dict]:
    fields = schema.get("settings", [])
    result = {}
    for field in fields:
        ident = str(field.get("id", ""))
        label = str(field.get("label", ""))
        words = f"{ident} {label}".casefold().replace("_", " ")
        if any(x in words for x in ("mobile image", "mobile_image")): key = "mobile_image"
        elif "image" in words and "overlay" not in words: key = "image"
        elif any(x in words for x in ("heading", "headline", "title")): key = "heading"
        elif any(x in words for x in ("text", "body", "subheading", "description")): key = "body"
        elif any(x in words for x in ("button label", "button text", "label")) and "button" in words: key = "button_label"
        elif any(x in words for x in ("button link", "button_url", "link")) and "button" in words: key = "button_link"
        elif "overlay" in words and any(x in words for x in ("opacity", "alpha")): key = "overlay"
        elif "position" in words: key = "position"
        elif "align" in words: key = "alignment"
        else: continue
        result.setdefault(key, field)
    return result


def discover_homepage_sections(theme_files: dict[str, str]) -> dict:
    """Find hero and category section schemas without theme-name assumptions."""
    if not isinstance(theme_files, dict): theme_files = {}
    heroes, categories = [], []
    for filename, raw in sorted(theme_files.items()):
        if not isinstance(filename, str) or not isinstance(raw, str) or not filename.startswith("sections/") or not filename.endswith(".liquid"):
            continue
        schema = _section_schema(raw)
        if not schema:
            continue
        label = re.sub(r"[-_/.:]+", " ", f"{filename} {schema.get('name', '')}".casefold())
        fields = _field_semantics(schema)
        if any(term in label for term in ("hero", "banner", "slideshow", "image with text")):
            required = {"image", "heading", "body", "button_label", "button_link"}
            found = sorted(required.intersection(fields))
            status = "HERO_SUPPORTED_HIGH_CONFIDENCE" if {"image", "heading", "button_link"}.issubset(fields) else "HERO_SUPPORTED_REVIEW_REQUIRED"
            heroes.append({"filename": filename, "type": filename.split("/")[-1].removesuffix(".liquid"), "name": schema.get("name", filename), "schema": schema, "fields": fields, "supported_fields": found, "status": status})
        list_fields = [f for f in schema.get("settings", []) if f.get("type") in {"collection_list", "collection_list_picker"}]
        blocks = schema.get("blocks", []) or []
        collection_blocks = [block for block in blocks if any(
            field.get("type") == "collection" for field in (block.get("settings") or []))]
        block_fields = [field for block in blocks for field in (block.get("settings") or [])]
        has_link = bool(list_fields or collection_blocks or any(
            field.get("type") in {"url", "link"} for field in block_fields))
        has_image = bool(list_fields or collection_blocks or any(
            field.get("type") == "image_picker" for field in block_fields))
        label_match = any(term in label for term in (
            "collection list", "multicolumn", "collection cards", "collection grid",
            "featured collection list"))
        multicolumn_semantics = label_match and has_link and has_image
        if list_fields or collection_blocks or multicolumn_semantics:
            mode = "COLLECTION_LIST" if list_fields else "COLLECTION_BLOCKS" if collection_blocks else "MULTICOLUMN"
            strong = bool(list_fields or collection_blocks)
            matched = []
            if list_fields: matched.append("COLLECTION_LIST_SETTING")
            if collection_blocks: matched.append("COLLECTION_BLOCK_SETTING")
            if label_match: matched.append("NORMALIZED_LABEL")
            if has_link: matched.append("COLLECTION_LINK")
            if has_image: matched.append("IMAGE")
            max_blocks = schema.get("max_blocks")
            supports_four = max_blocks is None or int(max_blocks) >= 4
            categories.append({
                "filename": filename,
                "type": filename.split("/")[-1].removesuffix(".liquid"),
                "name": schema.get("name", filename), "schema": schema, "mode": mode,
                "status": "CATEGORY_SUPPORTED_HIGH_CONFIDENCE" if strong else "CATEGORY_SUPPORTED_REVIEW_REQUIRED",
                "matched_semantics": matched,
                "collection_list_field_ids": [field.get("id") for field in list_fields if field.get("id")],
                "collection_block_types": [block.get("type") for block in collection_blocks if block.get("type")],
                "supports_4_cards": supports_four,
                "supports_collection_link": has_link,
                "supports_image": has_image,
            })
    heroes.sort(key=lambda x: (x["status"] != "HERO_SUPPORTED_HIGH_CONFIDENCE", x["filename"]))
    categories.sort(key=lambda x: (x["mode"] != "COLLECTION_LIST",
                                   _words_for_schema_rank(x["type"]) != "collection list",
                                   x["status"] != "CATEGORY_SUPPORTED_HIGH_CONFIDENCE", x["filename"]))
    return {"hero": heroes[0] if heroes else None, "category": categories[0] if categories else None,
            "hero_status": heroes[0]["status"] if heroes else "HERO_NOT_FOUND",
            "category_status": categories[0]["status"] if categories else "CATEGORY_NOT_FOUND"}


def _words_for_schema_rank(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def hero_copy(brand: dict, collections: list[dict], collection_handles: dict[str, str] | None = None) -> dict:
    profile = brand.get("profile", brand) if isinstance(brand, dict) else {}
    if not isinstance(profile, dict): profile = {}
    brand_name = normalize_text_value(profile.get("brand_name")) or "our store"
    category = normalize_text_value(profile.get("primary_category")).lower() or "everyday essentials"
    market = normalize_text_value(profile.get("target_country"))
    handles = collection_handles if isinstance(collection_handles, dict) else {}
    collections = collections if isinstance(collections, list) else []
    first = next((row for row in collections if isinstance(row, dict) and row.get("enabled", 1) and handles.get(row.get("collection_key"))), None)
    cta_target = f"/collections/{handles[first['collection_key']]}" if first else None
    cta_label = f"Shop {first.get('title')}" if first else "Explore collections"
    # Copy remains category-oriented and avoids claims, urgency, discounts, or social proof.
    if category and category != "everyday essentials":
        headline = f"A More Thoughtful Approach to {category.title()}"
        body = f"Explore practical, considered solutions designed for everyday use{f' in {market}' if market else ''}."
    else:
        headline = f"Discover a Better Everyday with {brand_name}"
        body = "Thoughtful, practical essentials designed to fit naturally into your everyday routine."
    if first:
        cta_label = "Shop " + str(first.get("title", "the collection"))
    return {"headline": headline, "body": body, "cta_label": cta_label, "cta_target": cta_target,
            "cta_valid": bool(cta_target), "alt_text": f"Lifestyle scene featuring {category} products from {brand_name}",
            "focal_point": "Keep the main subject near the center third for responsive crops.",
            "overlay_hint": "Use a restrained overlay only where needed to keep text readable.",
            "mobile_crop_hint": "Keep the subject and headline-safe negative space within the center 60% of the frame."}


def hero_image_prompt(brand: dict) -> str:
    profile = brand.get("profile", brand) if isinstance(brand, dict) else {}
    if not isinstance(profile, dict): profile = {}
    category = normalize_text_value(profile.get("primary_category")) or "the store's product category"
    palette = normalize_text_value(profile.get("colors")) or "the approved brand palette"
    tone = normalize_text_value(profile.get("personality") or profile.get("brand_keywords")) or "clean, practical, premium"
    avoid = normalize_text_value(profile.get("avoid_styles"))
    return (f"Photorealistic premium lifestyle wide desktop ecommerce hero for {category}. "
            f"Match this brand tone and palette: {tone}; {palette}. Keep the main subject mobile-safe near center, "
            "with clear negative space on one side for headline and CTA overlay, realistic natural lighting, "
            "coherent crop at desktop and mobile widths. No embedded text, no logo, no watermark, no fake UI, "
            "no illegible signs, and no copied third-party branded packaging. " + (f"Avoid: {avoid}." if avoid else ""))


def category_shortcuts(collection_plan: dict, *, collection_handles: dict[str, str] | None = None,
                       collection_assets: dict[str, dict] | None = None, maximum: int = 8, brand: dict | None = None) -> dict:
    handles, assets = collection_handles or {}, collection_assets or {}
    rows = []
    if not isinstance(collection_plan, dict): collection_plan = {}
    source_rows = collection_plan.get("collections", [])
    if not isinstance(source_rows, list): source_rows = []
    for source in source_rows:
        if not isinstance(source, dict): continue
        if not source.get("enabled", 1): continue
        warnings = [str(item).upper() for item in source.get("warnings", [])]
        if any(any(token in warning for token in ("ZERO_MATCH", "CONFLICT", "MISSING", "EXTREME_OVERLAP")) for warning in warnings):
            continue
        rows.append(dict(source))
    rows.sort(key=lambda row: (int(row.get("priority", 999)), -int(row.get("estimated_product_count", 0)), row.get("collection_key", "")))
    result, warnings, seen_targets, seen_assets = [], [], {}, {}
    profile = (brand or {}).get("profile", brand or {}) if isinstance(brand, dict) else {}
    if not isinstance(profile, dict): profile = {}
    brand_tone = normalize_text_value(profile.get("personality") or profile.get("brand_keywords")) or "clean, practical, premium"
    brand_palette = normalize_text_value(profile.get("colors")) or "approved brand palette"
    target_limit = min(8, max(4, int(maximum)), len(rows)) if rows else 0
    for row in rows[:target_limit]:
        key = str(row.get("collection_key") or _slug(row.get("title", "category")))
        handle = handles.get(key)
        target = f"/collections/{handle}" if handle else None
        action = "READY" if target else "SKIP_REMOTE"
        if not target:
            warnings.append({"code": "MISSING_COLLECTION", "collection_key": key, "action": action})
        if target and target in seen_targets and seen_targets[target] != key:
            warnings.append({"code": "DUPLICATE_TARGET", "collection_key": key, "target": target})
        elif target:
            seen_targets[target] = key
        asset = assets.get(key)
        if asset and asset.get("approval_status") != "APPROVED":
            asset = None
        asset_path = asset.get("path") if asset else None
        if asset_path and asset_path in seen_assets and seen_assets[asset_path] != key:
            warnings.append({"code": "DUPLICATE_ASSET", "collection_key": key, "path": asset_path})
        elif asset_path:
            seen_assets[asset_path] = key
        title = str(row.get("title", "")).strip()
        result.append({"shortcut_key": key, "collection_key": key, "title": title, "subtitle": None,
                       "target_collection_id": (collection_handles or {}).get(f"{key}:id"), "target_handle": handle,
                       "target": target, "status": action, "image_asset": asset_path,
                       "image_source": "COLLECTION_IMAGE_REUSE" if asset_path else "ICON_FALLBACK",
                       "image_sha256": hashlib.sha256(Path(asset_path).read_bytes()).hexdigest() if asset_path and Path(asset_path).is_file() else None,
                       "icon_style": "simple-outline", "image_prompt": row.get("image_prompt") or
                       f"Square premium lifestyle image illustrating {title}; match {brand_tone} brand tone and {brand_palette}; no text, logo, or watermark.",
                       "alt_text": str(row.get("image_alt_text") or f"{title} collection"),
                       "position": len(result) + 1})
    return {"items": result, "warnings": warnings, "ready_count": sum(row["status"] == "READY" for row in result),
            "skipped_count": sum(row["status"] != "READY" for row in result)}


def build_homepage_plan(*, store_id: str, brand: dict, collection_plan: dict,
                        collection_handles: dict[str, str] | None = None,
                        collection_assets: dict[str, dict] | None = None, maximum_categories: int = 8,
                        db=None) -> dict:
    """Persist a canonical hero + category plan; no Shopify calls are made."""
    if not isinstance(brand, dict): raise ValueError("brand_profile must be an object")
    if not isinstance(collection_plan, dict) or not isinstance(collection_plan.get("collections", []), list):
        raise ValueError("collection_plan.collections must be a list")
    _install(db)
    copy = hero_copy(brand, collection_plan.get("collections", []), collection_handles)
    hero = {**copy, "hero_key": "primary", "image_prompt": hero_image_prompt(brand),
            "image_asset_id": None, "image_status": "NEEDS_IMAGE", "enabled": True,
            "cta_target_type": "COLLECTION" if copy["cta_target"] else "UNRESOLVED"}
    categories = category_shortcuts(collection_plan, collection_handles=collection_handles,
                                    collection_assets=collection_assets, maximum=maximum_categories, brand=brand)
    source = {"store_id": store_id, "brand": brand, "collection_plan_id": collection_plan.get("plan_id"),
             "collection_plan": collection_plan, "handles": collection_handles or {}, "assets": collection_assets or {},
             "hero": hero, "categories": categories}
    source_hash = _hash(source)
    plan_id = "HMP_" + hashlib.sha256(f"{store_id}:{source_hash}".encode()).hexdigest()[:16]
    payload = {"plan_id": plan_id, "store_id": store_id, "brand_profile_id": brand.get("version") if isinstance(brand, dict) else None,
               "collection_plan_id": collection_plan.get("plan_id"), "planner_version": "3.9.0", "status": "DRAFT",
               "source_hash": source_hash, "hero": hero, "categories": categories["items"], "warnings": categories["warnings"],
               "category_summary": {k: categories[k] for k in ("ready_count", "skipped_count")}}
    now = _now()
    with connect(db) as con:
        previous = con.execute("SELECT source_hash FROM store_homepage_plans WHERE store_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (str(store_id),)).fetchone()
    if previous and previous["source_hash"] != source_hash:
        invalidate_homepage_previews(store_id, reason="Brand or collection plan changed", db=db)
    with connect(db) as con:
        con.execute("INSERT OR REPLACE INTO store_homepage_plans VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (plan_id, store_id, str(payload.get("brand_profile_id") or "") or None,
                     str(payload.get("collection_plan_id") or "") or None, "3.9.0", "DRAFT", source_hash,
                     _json(payload), now, now))
    return payload


def _managed_id(kind: str, key: str) -> str:
    return shopify_instance_id(kind, key)


def build_homepage_preview(plan: dict, snapshot: dict, *, db=None) -> dict:
    """Create a hash-bound, minimal homepage JSON proposal using discovered schemas."""
    if not isinstance(plan, dict) or not isinstance(plan.get("hero"), dict) or not isinstance(plan.get("categories"), list):
        return {"status": "INVALID_DATA", "error": {"path": "homepage_plan.hero|categories", "message": "홈페이지 계획 형식이 올바르지 않습니다."}, "write_performed": False}
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("theme_files", {}), dict):
        return {"status": "INVALID_DATA", "error": {"path": "theme_snapshot.theme_files", "message": "Shopify 테마 정보 형식이 올바르지 않습니다."}, "write_performed": False}
    _install(db)
    current = snapshot.get("template")
    files = snapshot.get("theme_files") or {}
    discovery = discover_homepage_sections(files)
    warnings = list(plan.get("warnings", []))
    actions, proposed = [], json.loads(json.dumps(current)) if isinstance(current, dict) else None
    theme = snapshot.get("theme") or {}
    template_status = snapshot.get("template_status", "READY" if isinstance(current, dict) else "TEMPLATE_BODY_MISSING")
    if proposed is None or not snapshot.get("template_filename") or template_status != "READY":
        reason = (snapshot.get("template_error") or {}).get("message") or "Homepage JSON template unavailable"
        actions.append({"action": "MANUAL_ACTION_REQUIRED", "reason": reason, "template_status": template_status})
    else:
        sections = proposed.setdefault("sections", {})
        order = proposed.setdefault("order", [])
        previous_ids = {key for key in sections if legacy_shopify_instance_kind(key)}
        hero_schema, category_schema = discovery["hero"], discovery["category"]
        hero = plan["hero"]
        # Migrate only the exact historical IDs implied by this preview's store/category identity.
        migration_pairs = [(legacy_managed_id("hero", "primary"), _managed_id("hero", "primary"), "HERO")]
        migration_pairs.append((legacy_managed_id("categories", plan["store_id"]),
                                _managed_id("categories", plan["store_id"]), "CATEGORY"))
        previous_ids.update(new_id for _, new_id, _ in migration_pairs if new_id in sections)
        block_id_map = {legacy_managed_id("cat", row["collection_key"]): _managed_id("cat", row["collection_key"])
                        for row in plan.get("categories", []) if row.get("collection_key")}
        for old_id, new_id, kind in migration_pairs:
            if old_id not in sections:
                continue
            if new_id in sections:
                actions.append({"action": "CONFLICT", "kind": kind, "reason": "Both legacy and current managed section IDs exist"})
                continue
            migrated = sections.pop(old_id)
            if kind == "CATEGORY" and isinstance(migrated.get("blocks"), dict):
                if any(block_id not in block_id_map for block_id in migrated["blocks"]):
                    sections[old_id] = migrated
                    actions.append({"action": "CONFLICT", "kind": kind, "reason": "Legacy category block identity cannot be proven from current plan"})
                    continue
                migrated["blocks"] = {block_id_map[block_id]: value for block_id, value in migrated["blocks"].items()}
                migrated["block_order"] = [block_id_map.get(block_id, block_id) for block_id in migrated.get("block_order", [])]
            sections[new_id] = migrated
            order[:] = [new_id if item == old_id else item for item in order]
            previous_ids.add(new_id)
        if not hero.get("image_asset_id") and not hero.get("image_url"):
            warnings.append({"code": "HERO_IMAGE_MISSING"})
        if not hero.get("cta_valid"):
            warnings.append({"code": "INVALID_CTA_TARGET"})
        if hero_schema:
            sid = _managed_id("hero", "primary")
            fields = hero_schema["fields"]
            values = {"heading": hero["headline"], "body": hero["body"],
                      "button_label": hero["cta_label"] if hero.get("cta_valid") else None,
                      "button_link": hero["cta_target"] if hero.get("cta_valid") else None,
                      "image": hero.get("image_url"), "mobile_image": hero.get("mobile_image_url")}
            for optional in ("overlay", "position", "alignment"):
                if fields.get(optional) and "default" in fields[optional]: values[optional] = fields[optional]["default"]
            image_field = fields.get("image")
            if image_field and image_field.get("type") != "image_picker":
                values["image"] = None
                warnings.append({"code": "THEME_SCHEMA_MISMATCH", "field": "image", "expected": "image_picker", "actual": image_field.get("type")})
            image_ref = hero.get("theme_image_ref")
            valid_image_ref = isinstance(image_ref, str) and image_ref.startswith("shopify://shop_images/") and ".." not in image_ref
            if hero.get("image_url") and not valid_image_ref:
                actions.append({"action": "MANUAL_ACTION_REQUIRED", "kind": "HERO_IMAGE", "reason": "Shopify Files CDN URL is not an image_picker resource reference; confirm the shopify://shop_images/ reference."})
                warnings.append({"code": "HERO_IMAGE_RESOURCE_MAPPING_REQUIRED"})
                values["image"] = None
            elif valid_image_ref:
                values["image"] = image_ref
            settings, unsupported = {}, []
            for canonical, value in values.items():
                field = fields.get(canonical)
                if field and value is not None:
                    settings[field["id"]] = value
                elif value is not None:
                    unsupported.append(canonical)
            if sid in previous_ids:
                existing = sections[sid]
                if sid not in order:
                    actions.append({"action": "CONFLICT", "section_id": sid, "reason": "Managed hero missing from template order"})
                elif existing.get("type") != hero_schema["type"]:
                    actions.append({"action": "CONFLICT", "section_id": sid, "reason": "Theme schema changed for managed hero"})
                else:
                    updated = json.loads(json.dumps(existing)); old_settings = updated.setdefault("settings", {})
                    changed = any(old_settings.get(k) != v for k, v in settings.items())
                    with connect(db) as con:
                        baseline = con.execute("SELECT section_json FROM store_homepage_managed_state WHERE store_id=? AND theme_id=? AND section_id=?", (plan["store_id"], str(theme.get("id", "")), sid)).fetchone()
                    drifted = bool(baseline and _hash(existing) != _hash(json.loads(baseline["section_json"])))
                    if drifted or (changed and not baseline):
                        actions.append({"action": "CONFLICT", "section_id": sid, "kind": "HERO", "reason": "Managed hero was manually changed since last verified apply"})
                    elif changed:
                        old_settings.update(settings)
                        sections[sid] = updated
                        actions.append({"action": "UPDATE_SECTION", "section_id": sid, "kind": "HERO", "unsupported_fields": unsupported})
                    else: actions.append({"action": "NO_CHANGE", "section_id": sid, "kind": "HERO", "unsupported_fields": unsupported})
            elif any(section.get("type") == hero_schema["type"] for key, section in sections.items() if key not in previous_ids):
                actions.append({"action": "CONFLICT", "kind": "HERO", "reason": "An unmanaged hero/banner section already exists; review before adding another"})
            else:
                # Do not silently claim readiness when image is missing; keep the patch reviewable.
                sections[sid] = {"type": hero_schema["type"], "settings": settings}
                header_index = next((i for i, key in enumerate(order) if "header" in str(key).casefold() or "header" in str(sections.get(key, {}).get("type", "")).casefold()), None)
                order.insert((header_index + 1) if header_index is not None else 0, sid)
                actions.append({"action": "CREATE_SECTION", "section_id": sid, "kind": "HERO", "unsupported_fields": unsupported})
        else:
            actions.append({"action": "MANUAL_ACTION_REQUIRED", "kind": "HERO", "reason": "Hero section schema not found"})
        if category_schema:
            sid = _managed_id("categories", plan["store_id"])
            ready = [row for row in plan.get("categories", []) if row.get("status") == "READY" and row.get("target")]
            blocks_payload, block_order = {}, []
            if category_schema["mode"] == "COLLECTION_LIST":
                field = next((x for x in category_schema["schema"].get("settings", []) if x.get("type") in {"collection_list", "collection_list_picker"}), None)
                handles = [row["target_handle"] for row in ready if row.get("target_handle")]
                settings = {field["id"]: handles} if field and handles else {}
                content_ok = bool(settings)
            elif category_schema["mode"] == "COLLECTION_BLOCKS":
                block_schema = next((b for b in category_schema["schema"].get("blocks", [])
                                     if any(f.get("type") == "collection" for f in b.get("settings", []))), None)
                collection_field = next((f for f in (block_schema or {}).get("settings", []) if f.get("type") == "collection"), None)
                if block_schema and collection_field:
                    for item in ready:
                        if not item.get("target_handle"): continue
                        block_id = _managed_id("cat", item["collection_key"])
                        title_field = next((f for f in block_schema.get("settings", []) if f.get("type") in {"text", "inline_richtext"} and "title" in (str(f.get("id", "")) + str(f.get("label", ""))).casefold()), None)
                        block_settings = {collection_field["id"]: item["target_handle"]}
                        if title_field: block_settings[title_field["id"]] = item["title"]
                        blocks_payload[block_id] = {"type": block_schema.get("type"), "settings": block_settings}
                        block_order.append(block_id)
                settings = {}
                content_ok = bool(block_order)
            elif category_schema["mode"] == "MULTICOLUMN":
                block_schema = next((b for b in category_schema["schema"].get("blocks", []) if b.get("settings")), None)
                if block_schema:
                    fields = block_schema.get("settings", [])
                    title_field = next((f for f in fields if f.get("type") in {"text", "inline_richtext"} and "title" in (str(f.get("id", "")) + str(f.get("label", ""))).casefold()), None)
                    link_field = next((f for f in fields if f.get("type") == "url"), None)
                    image_field = next((f for f in fields if f.get("type") == "image_picker"), None)
                    if title_field and link_field:
                        for item in ready:
                            block_id = _managed_id("cat", item["collection_key"])
                            block_settings = {title_field["id"]: item["title"], link_field["id"]: item["target"]}
                            if image_field and item.get("image_shopify_url"):
                                block_settings[image_field["id"]] = item["image_shopify_url"]
                            elif image_field:
                                warnings.append({"code": "CATEGORY_IMAGE_MISSING", "collection_key": item["collection_key"], "fallback": item.get("icon_style")})
                            blocks_payload[block_id] = {"type": block_schema.get("type"), "settings": block_settings}
                            block_order.append(block_id)
                settings = {}
                content_ok = bool(block_order)
            else:
                settings, content_ok = {}, False
            if not ready:
                actions.append({"action": "SKIP", "kind": "CATEGORY", "reason": "No valid mapped collection targets"})
            elif not content_ok:
                actions.append({"action": "MANUAL_ACTION_REQUIRED", "kind": "CATEGORY", "reason": "Category section schema needs explicit block/content mapping"})
            elif sid in sections:
                existing = sections[sid]
                if existing.get("type") != category_schema["type"]:
                    actions.append({"action": "CONFLICT", "section_id": sid, "kind": "CATEGORY", "reason": "Theme schema changed for managed category section"})
                else:
                    desired_section = {"type": category_schema["type"], "settings": settings,
                                       **({"blocks": blocks_payload, "block_order": block_order} if blocks_payload else {})}
                    with connect(db) as con:
                        baseline = con.execute("SELECT section_json FROM store_homepage_managed_state WHERE store_id=? AND theme_id=? AND section_id=?", (plan["store_id"], str(theme.get("id", "")), sid)).fetchone()
                    drifted = bool(baseline and _hash(existing) != _hash(json.loads(baseline["section_json"])))
                    if existing == desired_section:
                        actions.append({"action": "NO_CHANGE", "section_id": sid, "kind": "CATEGORY"})
                    elif drifted or not baseline:
                        actions.append({"action": "CONFLICT", "section_id": sid, "kind": "CATEGORY", "reason": "Managed category section was manually changed since last verified apply"})
                    else:
                        sections[sid] = {**existing, "settings": {**existing.get("settings", {}), **settings},
                                         **({"blocks": blocks_payload, "block_order": block_order} if blocks_payload else {})}
                        actions.append({"action": "UPDATE_SECTION", "section_id": sid, "kind": "CATEGORY"})
            elif any(section.get("type") == category_schema["type"] for key, section in sections.items() if key not in previous_ids):
                actions.append({"action": "CONFLICT", "kind": "CATEGORY", "reason": "An unmanaged category/collection section already exists; review before adding another"})
            else:
                sections[sid] = {"type": category_schema["type"], "settings": settings,
                                 **({"blocks": blocks_payload, "block_order": block_order} if blocks_payload else {})}
                # Preserve merchant order: managed categories follow the managed hero and precede other sections.
                hero_id = _managed_id("hero", "primary")
                pos = order.index(hero_id) + 1 if hero_id in order else 0
                order.insert(pos, sid)
                actions.append({"action": "CREATE_SECTION", "section_id": sid, "kind": "CATEGORY"})
        else:
            actions.append({"action": "MANUAL_ACTION_REQUIRED", "kind": "CATEGORY", "reason": "Collection-list or category section schema not found"})
    status = "CONFLICT" if any(x["action"] == "CONFLICT" for x in actions) else "MANUAL_ACTION_REQUIRED" if any(x["action"] == "MANUAL_ACTION_REQUIRED" for x in actions) else "PREVIEW"
    source_document = _document_preview_metadata(snapshot, proposed)
    capabilities = {
        "hero": "AUTO" if discovery.get("hero") else "MANUAL",
        "categories": "AUTO" if discovery.get("category") else "MANUAL",
        "featured_products": "NOT_EVALUATED",
        "theme_template": ("COMMENTED_JSON_SUPPORTED" if source_document.get("had_leading_comment") else "JSON_SUPPORTED")
            if source_document.get("status") == "READY" else source_document.get("status", "BLOCKED"),
    }
    preview_payload = {"status": status, "plan_id": plan["plan_id"], "store_id": plan["store_id"], "theme": theme,
        "shop_domain": snapshot.get("shop_domain"), "api_version": snapshot.get("api_version"),
        "template_filename": snapshot.get("template_filename"), "current": current, "proposed": proposed,
        "source_document": source_document, "capabilities": capabilities,
        "actions": actions, "warnings": warnings, "discovery": {key: discovery[key]["status"] if discovery[key] else discovery[key+"_status"] for key in ("hero", "category")},
        "asset_mapping": {"hero": {"asset_id": plan["hero"].get("image_asset_id"), "url": plan["hero"].get("image_url"),
                                    "theme_image_ref": plan["hero"].get("theme_image_ref"),
                                    "theme_image_ref_confirmed": bool(plan["hero"].get("theme_image_ref_confirmed")),
                                    "sha256": plan["hero"].get("asset_sha256"),
                                    "approved": bool(plan["hero"].get("asset_approved"))},
                          "categories": {x["collection_key"]: {"path": x.get("image_asset"), "sha256": x.get("image_sha256")} for x in plan.get("categories", [])}},
        "link_mapping": {x["collection_key"]: x.get("target") for x in plan.get("categories", [])},
        "theme_write_capability": "write_themes" in set(snapshot.get("scopes", [])),
        "diff": {"before_hash": _hash(current), "proposed_hash": _hash(proposed)}, "source_hash": _hash({"plan": plan, "theme": theme, "template": current, "files": files})}
    preview_id = "HMPV_" + secrets.token_hex(9)
    preview_payload["preview_id"] = preview_id
    with connect(db) as con:
        con.execute("INSERT INTO store_homepage_previews VALUES(?,?,?,?,?,?,?,?)", (preview_id, plan["plan_id"], plan["store_id"],
                    str(theme.get("id", "")), preview_payload["source_hash"], status, _json(preview_payload), _now()))
    return preview_payload


def compose_homepage_preview(plan: dict, snapshot: dict, collection_plan: dict, *, collection_handles: dict[str, str] | None = None,
                             featured_products_plan: dict | None = None, db=None) -> dict:
    """Build the one canonical homepage proposal, including featured products when supplied."""
    from .homepage_collections import build_homepage_plan as build_featured_plan
    featured = build_featured_plan(snapshot, collection_plan, collection_handles=collection_handles or {}, db=db)
    featured_conflict = featured.get("status") == "CONFLICT"
    # Do not apply a partially conflicting featured-collection proposal, but keep
    # independent homepage and featured-product previews visible for review.
    collection_template = snapshot.get("template") if featured_conflict else (featured.get("proposed") or snapshot.get("template"))
    composed_snapshot = {**snapshot, "template": collection_template}
    preview = build_homepage_preview(plan, composed_snapshot, db=db)
    preview["current"] = snapshot.get("template")
    # Keep build_homepage_preview's hero/category proposal instead of replacing it
    # with the pre-composition collection template.
    preview["featured_collection_actions"] = featured.get("operations", [])
    preview["actions"] = list(featured.get("operations", [])) + preview["actions"]
    if featured.get("status") == "MANUAL_PATCH_MODE":
        preview["actions"].append({"action": "MANUAL_ACTION_REQUIRED", "kind": "FEATURED_COLLECTION",
                                   "reason": "Featured collection section schema was not detected; retain the Phase 3.4 manual patch."})
        preview["warnings"].append({"code": "FEATURED_COLLECTION_MANUAL_FALLBACK"})
    preview["diff"] = {"before_hash": _hash(preview["current"]), "proposed_hash": _hash(preview["proposed"])}
    preview["source_hash"] = _hash({"base": preview["source_hash"], "featured": featured.get("diff")})
    if featured_products_plan:
        from .homepage_featured_products import FeaturedProductAssignmentService
        feature_service = FeaturedProductAssignmentService(db=db)
        feature_preview = feature_service.build_theme_preview(featured_products_plan,
            {**snapshot, "template": preview.get("proposed")})
        preview["featured_products_plan_id"] = featured_products_plan.get("plan_id")
        preview["featured_products_source_hash"] = featured_products_plan.get("source_hash")
        preview["featured_products_preview"] = feature_preview
        preview["capabilities"]["featured_products"] = "AUTO" if feature_preview.get("status") == "PREVIEW" else "MANUAL"
        if feature_preview.get("status") == "PREVIEW":
            preview["proposed"] = feature_preview["proposed"]
            preview["actions"].append({"action": "FEATURED_PRODUCTS_PREVIEW", "kind": "FEATURED_PRODUCTS",
                                       "section_id": feature_preview.get("section_id"), "status": "PREVIEW"})
        else:
            preview["actions"].append({"action": "MANUAL_ACTION_REQUIRED", "kind": "FEATURED_PRODUCTS",
                                       "reason": feature_preview.get("reason") or "현재 테마에서 상품 리스트 구성을 안전하게 확인할 수 없습니다."})
        preview["source_hash"] = _hash({"base": preview["source_hash"], "featured_products": feature_preview,
                                        "featured_products_plan": featured_products_plan.get("source_hash")})
        preview["diff"] = {"before_hash": _hash(preview["current"]), "proposed_hash": _hash(preview["proposed"])}
        preview["source_document"] = _document_preview_metadata(snapshot, preview.get("proposed"))
        if preview["status"] == "MANUAL_ACTION_REQUIRED" and preview["capabilities"]["featured_products"] == "AUTO" and "categories" in preview["capabilities"]:
            # A supported featured section remains useful even if unrelated homepage
            # category/hero automation needs manual review; it is never apply-ready.
            preview["status"] = "PARTIAL_PREVIEW"
            preview["manual_reasons"] = ["추천 상품 자동 미리보기 가능 · 카테고리 바로가기는 수동 확인 필요"]
        if preview.get("capabilities", {}).get("featured_products") == "AUTO" and preview.get("capabilities", {}).get("categories") == "MANUAL":
            reason = "Featured products can be previewed automatically; category shortcuts require manual review."
            preview.setdefault("manual_reasons", []).append(reason)
            preview["actions"].append({"action": "MANUAL_ACTION_REQUIRED", "kind": "CATEGORY", "reason": reason})
        preview["capabilities"]["theme_template"] = ("COMMENTED_JSON_SUPPORTED" if preview["source_document"].get("had_leading_comment") else "JSON_SUPPORTED") if preview["source_document"].get("status") == "READY" else preview["source_document"].get("status", "BLOCKED")
    if any(a.get("action") == "CONFLICT" for a in preview["actions"]):
        preview["status"] = "CONFLICT"
    elif any(a.get("action") == "MANUAL_ACTION_REQUIRED" for a in preview["actions"]):
        preview["status"] = ("PARTIAL_PREVIEW" if preview.get("capabilities", {}).get("featured_products") == "AUTO"
                             else "MANUAL_ACTION_REQUIRED")
    with connect(db) as con:
        con.execute("UPDATE store_homepage_previews SET source_hash=?,status=?,preview_json=? WHERE preview_id=?",
                    (preview["source_hash"], preview["status"], _json(preview), preview["preview_id"]))
    return preview


def invalidate_homepage_previews(store_id: str, *, reason: str = "Homepage inputs changed", db=None) -> int:
    """Mark downstream persisted homepage proposals stale without dropping plans/caches."""
    _install(db)
    changed = 0
    with connect(db) as con:
        rows = con.execute("SELECT preview_id,preview_json FROM store_homepage_previews WHERE store_id=? AND status NOT IN ('STALE','CONFLICT')", (str(store_id),)).fetchall()
        for row in rows:
            try: payload = json.loads(row["preview_json"])
            except (TypeError, json.JSONDecodeError): payload = {}
            payload["status"] = "STALE"
            payload["stale_reason"] = str(reason)
            con.execute("UPDATE store_homepage_previews SET status='STALE',preview_json=? WHERE preview_id=?",
                        (_json(payload), row["preview_id"]))
            changed += 1
    return changed


class HomepageAutomationService:
    def __init__(self, *, db=None, export_dir: str | Path | None = None, client_factory=ShopifyGraphQLClient):
        self.db, self.export_dir, self.client_factory = db, Path(export_dir) if export_dir else EXPORT_DIR, client_factory
        _install(db)

    def apply(self, preview_id: str, *, confirmed: bool = False, client=None, approved_assets: bool = False) -> dict:
        if confirmed is not True:
            raise RuntimeError("홈페이지 theme 적용은 별도 명시적 확인이 필요합니다")
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM store_homepage_previews WHERE preview_id=?", (preview_id,)).fetchone()
            latest = con.execute("SELECT preview_id FROM store_homepage_previews WHERE store_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (row["store_id"],)).fetchone() if row else None
            latest_plan = con.execute("SELECT plan_id FROM store_homepage_plans WHERE store_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (row["store_id"],)).fetchone() if row else None
            has_featured_plans = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='homepage_featured_product_plans'").fetchone() if row else None
            latest_featured = con.execute("SELECT plan_id FROM homepage_featured_product_plans WHERE store_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (row["store_id"],)).fetchone() if has_featured_plans else None
        if not row:
            raise KeyError(preview_id)
        preview = json.loads(row["preview_json"])
        if latest["preview_id"] != preview_id:
            return {"status": "CONFLICT", "reason": "A newer homepage preview exists"}
        if latest_plan and latest_plan["plan_id"] != row["plan_id"]:
            return {"status": "CONFLICT", "reason": "Homepage inputs changed after preview"}
        if preview.get("status") == "STALE":
            return {"status": "CONFLICT", "reason": preview.get("stale_reason") or "Homepage preview is stale"}
        if preview.get("featured_products_plan_id") and (not latest_featured or latest_featured["plan_id"] != preview["featured_products_plan_id"]):
            return {"status": "CONFLICT", "reason": "Featured product selection changed after preview"}
        hero_asset = preview.get("asset_mapping", {}).get("hero", {})
        if preview["status"] != "PREVIEW" or not approved_assets or not hero_asset.get("url") or not hero_asset.get("approved") or not hero_asset.get("theme_image_ref") or not hero_asset.get("theme_image_ref_confirmed"):
            return {"status": "MANUAL_ACTION_REQUIRED", "reason": "Resolve preview warnings and approve all referenced assets first"}
        if preview.get("discovery", {}).get("hero") != "HERO_SUPPORTED_HIGH_CONFIDENCE" or preview.get("discovery", {}).get("category") != "CATEGORY_SUPPORTED_HIGH_CONFIDENCE":
            return {"status": "MANUAL_ACTION_REQUIRED", "reason": "Hero and category section mappings must both be high-confidence"}
        if not preview.get("theme_write_capability"):
            return {"status": "MANUAL_ACTION_REQUIRED", "reason": "write_themes capability missing"}
        config = get_connection(row["store_id"], db=self.db); token, _ = get_shopify_token(row["store_id"],db=self.db)
        if not config or not token:
            return {"status": "MANUAL_ACTION_REQUIRED", "reason": "Shopify connection or credential missing"}
        if preview.get("shop_domain") and config.get("shop_domain") != preview["shop_domain"]:
            return {"status": "CONFLICT", "reason": "Shopify connection changed after preview"}
        client = client or self.client_factory(config["shop_domain"], token, config["api_version"])
        # The preview is read again immediately before the only write.
        if preview.get("theme", {}).get("role", "MAIN") != "MAIN":
            return {"status": "CONFLICT", "reason": "Preview is not for the currently published theme"}
        scopes_result = client.execute("query HomepageScopes { currentAppInstallation { accessScopes { handle } } }")
        scopes = {scope.get("handle") for scope in (scopes_result.get("currentAppInstallation") or {}).get("accessScopes", [])}
        if "read_themes" not in scopes or "write_themes" not in scopes:
            return {"status": "MANUAL_ACTION_REQUIRED", "reason": "read_themes and write_themes are required"}
        file_query = "query HomepageCurrentTheme($id: ID!, $filenames: [String!]!) { theme(id: $id) { id role files(first: 1, filenames: $filenames) { nodes { filename body { __typename ... on OnlineStoreThemeFileBodyText { content } ... on OnlineStoreThemeFileBodyBase64 { contentBase64 } } } userErrors { code filename } } } }"
        fresh = client.execute(file_query, {"id": row["theme_id"], "filenames": [preview["template_filename"]]}).get("theme") or {}
        try:
            observed_raw, observed_document = _remote_theme_document(fresh, preview["template_filename"])
        except ShopifyJsonDocumentError as exc:
            return {"status": "CONFLICT", "reason": exc.message, "template_status": exc.code}
        source_document = preview.get("source_document") or {}
        if (fresh.get("id") != row["theme_id"] or fresh.get("role") != "MAIN"
                or not isinstance(observed_document.parsed, dict)):
            return {"status": "CONFLICT", "reason": "Remote theme identity or homepage template changed"}
        if source_document.get("before_raw_hash"):
            raw_matches = observed_document.raw_hash == source_document["before_raw_hash"]
        else:
            # Old previews lack raw hashes. They remain usable for clean legacy JSON,
            # but cannot safely overwrite a commented source whose prefix was unseen.
            raw_matches = not observed_document.had_leading_comment
        expected_semantic_hash = source_document.get("before_semantic_hash") or preview["diff"].get("before_hash")
        if not raw_matches or observed_document.semantic_hash != expected_semantic_hash:
            return {"status": "CONFLICT", "reason": "Remote homepage raw or semantic content drifted since preview"}
        if preview.get("diff", {}).get("before_hash") == preview.get("diff", {}).get("proposed_hash"):
            return {"status": "VERIFIED", "no_change": True, "theme_id": row["theme_id"], "write_performed": False}
        if not isinstance(preview.get("proposed"), dict) or not preview.get("template_filename", "").startswith("templates/index"):
            return {"status": "MANUAL_ACTION_REQUIRED", "reason": "Unsafe template path or empty proposal"}
        proposed_raw = render_shopify_json_document(observed_document, preview["proposed"])
        proposed_raw_hash = _raw_hash(proposed_raw)
        if source_document.get("proposed_raw_hash") and source_document["proposed_raw_hash"] != proposed_raw_hash:
            return {"status": "CONFLICT", "reason": "Rendered homepage proposal no longer matches its preview"}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        folder = self.export_dir / "theme_backups" / _safe_store(row["store_id"]) / (stamp + "-homepage")
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "before.raw.json").write_text(observed_raw, encoding="utf-8", newline="")
        (folder / "proposed.raw.json").write_text(proposed_raw, encoding="utf-8", newline="")
        (folder / "before.parsed.json").write_text(json.dumps(observed_document.parsed, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "proposed.parsed.json").write_text(json.dumps(preview["proposed"], ensure_ascii=False, indent=2), encoding="utf-8")
        # Keep legacy parsed backup filenames for existing tools and tests.
        (folder / "before.json").write_text(json.dumps(observed_document.parsed, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "proposed.json").write_text(json.dumps(preview["proposed"], ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "diff.md").write_text("# Homepage patch\n\n" + "\n".join(f"- {x['action']}: {x.get('kind', '')}" for x in preview["actions"]) + "\n", encoding="utf-8")
        backup_id = "HMB_" + secrets.token_hex(8)
        with connect(self.db) as con:
            con.execute("INSERT INTO store_homepage_backups VALUES(?,?,?,?,?,?,?,?,?)", (backup_id, preview_id, row["store_id"], row["theme_id"],
                        preview["template_filename"], str(folder), _json(observed_document.parsed), _json(preview["proposed"]), _now()))
        try:
            result = client.execute(UPSERT_THEME_FILES, {"themeId": row["theme_id"], "files": [{"filename": preview["template_filename"],
                "body": {"type": "TEXT", "value": proposed_raw}}]}).get("themeFilesUpsert") or {}
        except RuntimeError as exc:
            message = str(exc).casefold()
            if "access denied" in message or "write_themes" in message or "exemption" in message:
                return {"status": "MANUAL_ACTION_REQUIRED", "reason": "Shopify rejected theme write capability; use Theme Editor. No browser automation will be attempted."}
            raise
        if result.get("userErrors"):
            return {"status": "FAILED", "backup_id": backup_id, "errors": [{"field": e.get("field"), "message": e.get("message", "")[:180]} for e in result["userErrors"]]}
        verify = client.execute(file_query, {"id": row["theme_id"], "filenames": [preview["template_filename"]]}).get("theme") or {}
        try:
            _, after_document = _remote_theme_document(verify, preview["template_filename"])
            after = after_document.parsed
        except ShopifyJsonDocumentError:
            after_document, after = None, None
        verified = bool(verify.get("id") == row["theme_id"] and verify.get("role") == "MAIN"
                        and after == preview["proposed"] and after_document
                        and after_document.prefix == observed_document.prefix
                        and after_document.suffix == observed_document.suffix
                        and after_document.raw_hash == proposed_raw_hash)
        if verified:
            with connect(self.db) as con:
                managed_ids = {action.get("section_id") for action in preview.get("actions", [])
                               if action.get("kind") in {"HERO", "CATEGORY"} and action.get("section_id")}
                for section_id, section in (after.get("sections") or {}).items():
                    if section_id in managed_ids:
                        con.execute("INSERT INTO store_homepage_managed_state VALUES(?,?,?,?,?) ON CONFLICT(store_id,theme_id,section_id) DO UPDATE SET section_json=excluded.section_json,updated_at=excluded.updated_at",
                                    (row["store_id"], row["theme_id"], section_id, _json(section), _now()))
        return {"status": "VERIFIED" if verified else "VERIFY_FAILED", "backup_id": backup_id,
                "theme_id": row["theme_id"], "changed_file": preview["template_filename"],
                "raw_hash": after_document.raw_hash if verified else None, "write_performed": True}

    def rollback(self, backup_id: str, *, confirmed: bool = False, client=None) -> dict:
        if confirmed is not True:
            raise RuntimeError("홈페이지 rollback은 별도 명시적 확인이 필요합니다")
        with connect(self.db) as con: row = con.execute("SELECT * FROM store_homepage_backups WHERE backup_id=?", (backup_id,)).fetchone()
        if not row: raise KeyError(backup_id)
        config = get_connection(row["store_id"], db=self.db); token, _ = get_shopify_token(row["store_id"],db=self.db)
        if not config or not token: return {"status": "MANUAL_ACTION_REQUIRED"}
        client = client or self.client_factory(config["shop_domain"], token, config["api_version"])
        scopes_result = client.execute("query HomepageRollbackScopes { currentAppInstallation { accessScopes { handle } } }")
        scopes = {scope.get("handle") for scope in (scopes_result.get("currentAppInstallation") or {}).get("accessScopes", [])}
        if "write_themes" not in scopes or "read_themes" not in scopes:
            return {"status":"MANUAL_ACTION_REQUIRED","reason":"read_themes and write_themes scopes are required"}
        current_query = "query HomepageRollbackCurrent($id: ID!, $filenames: [String!]!) { theme(id: $id) { id role files(first: 1, filenames: $filenames) { nodes { filename body { __typename ... on OnlineStoreThemeFileBodyText { content } ... on OnlineStoreThemeFileBodyBase64 { contentBase64 } } } userErrors { code filename } } } }"
        current_result = client.execute(current_query, {"id": row["theme_id"], "filenames": [row["filename"]]}).get("theme") or {}
        try:
            current_raw, current_document = _remote_theme_document(current_result, row["filename"])
        except ShopifyJsonDocumentError as exc:
            return {"status": "CONFLICT", "reason": exc.message}
        if current_result.get("id") != row["theme_id"] or current_result.get("role") != "MAIN":
            return {"status": "CONFLICT", "reason": "Published theme identity changed since backup"}
        proposed_parsed = json.loads(row["proposed_json"])
        folder = Path(row["folder"])
        proposed_raw_path = folder / "proposed.raw.json"
        if proposed_raw_path.is_file():
            proposed_raw = _read_exact_text(proposed_raw_path)
            if (current_document.raw_hash != _raw_hash(proposed_raw)
                    or current_document.semantic_hash != shopify_json_semantic_hash(proposed_parsed)):
                return {"status": "CONFLICT", "reason": "Remote raw or semantic homepage drifted since this backup was created"}
        elif current_document.parsed != proposed_parsed:
            return {"status": "CONFLICT", "reason": "Remote homepage drifted since this legacy backup was created"}
        before_raw_path = folder / "before.raw.json"
        legacy = not before_raw_path.is_file()
        if legacy:
            before_parsed = json.loads(row["before_json"])
            restore_raw = json.dumps(before_parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        else:
            restore_raw = _read_exact_text(before_raw_path)
            try:
                before_document = parse_shopify_json_document(restore_raw)
            except ShopifyJsonDocumentError as exc:
                return {"status": "FAILED", "reason": f"Raw backup is not a valid Shopify JSON document: {exc.code}"}
            if before_document.parsed != json.loads(row["before_json"]):
                return {"status": "FAILED", "reason": "Raw backup and parsed backup disagree"}
        result = client.execute(UPSERT_THEME_FILES, {"themeId": row["theme_id"], "files": [{"filename": row["filename"],
             "body": {"type": "TEXT", "value": restore_raw}}]}).get("themeFilesUpsert") or {}
        if result.get("userErrors"):
            return {"status": "FAILED", "backup_id": backup_id}
        filename_query = "query HomepageRollbackVerify($id: ID!, $filenames: [String!]!) { theme(id: $id) { id role files(first: 1, filenames: $filenames) { nodes { filename body { __typename ... on OnlineStoreThemeFileBodyText { content } ... on OnlineStoreThemeFileBodyBase64 { contentBase64 } } } userErrors { code filename } } } }"
        checked = client.execute(filename_query, {"id": row["theme_id"], "filenames": [row["filename"]]}).get("theme") or {}
        try:
            restored_raw, restored_document = _remote_theme_document(checked, row["filename"])
        except ShopifyJsonDocumentError:
            restored_raw, restored_document = None, None
        before_parsed = json.loads(row["before_json"])
        verified = bool(checked.get("id") == row["theme_id"] and checked.get("role") == "MAIN"
                        and restored_document and restored_document.parsed == before_parsed)
        if verified and not legacy:
            verified = restored_document.raw_hash == _raw_hash(restore_raw)
        return {"status": "VERIFIED" if verified else "VERIFY_FAILED", "backup_id": backup_id,
                "rollback_mode": "LEGACY_PARSED_ROLLBACK" if legacy else "EXACT_RAW_ROLLBACK",
                "raw_hash": restored_document.raw_hash if verified else None, "write_performed": True}

    def verify(self, preview_id: str, *, client=None) -> dict:
        with connect(self.db) as con: row = con.execute("SELECT * FROM store_homepage_previews WHERE preview_id=?", (preview_id,)).fetchone()
        if not row: return {"status": "NOT_FOUND"}
        preview = json.loads(row["preview_json"])
        config = get_connection(row["store_id"], db=self.db); token, _ = get_shopify_token(row["store_id"],db=self.db)
        if not config or not token: return {"status": "MANUAL_ACTION_REQUIRED", "reason": "Shopify connection or credential missing"}
        client = client or self.client_factory(config["shop_domain"], token, config["api_version"])
        query = "query HomepageVerify($id: ID!, $filenames: [String!]!) { theme(id: $id) { id role files(first: 1, filenames: $filenames) { nodes { filename body { __typename ... on OnlineStoreThemeFileBodyText { content } ... on OnlineStoreThemeFileBodyBase64 { contentBase64 } } } userErrors { code filename } } } }"
        result = client.execute(query, {"id": row["theme_id"], "filenames": [preview["template_filename"]]}).get("theme") or {}
        try:
            _, observed_document = _remote_theme_document(result, preview["template_filename"])
            observed = observed_document.parsed
        except ShopifyJsonDocumentError:
            observed_document, observed = None, None
        source_document = preview.get("source_document") or {}
        valid = bool(result.get("id") == row["theme_id"] and result.get("role") == "MAIN"
                     and observed_document and observed == preview.get("proposed"))
        if valid and source_document.get("proposed_raw_hash"):
            valid = (observed_document.raw_hash == source_document["proposed_raw_hash"]
                     and _raw_hash(observed_document.prefix) == source_document.get("prefix_hash")
                     and _raw_hash(observed_document.suffix) == source_document.get("suffix_hash"))
        return {"status": "VERIFIED" if valid else "MANUAL_ACTION_REQUIRED",
                "observed_hash": _hash(observed), "expected_hash": preview.get("diff", {}).get("proposed_hash"),
                "observed_raw_hash": observed_document.raw_hash if observed_document else None,
                "write_performed": False}

    def export_report(self, plan: dict, *, preview: dict | None = None) -> dict:
        root = self.export_dir / "homepage_reports" / _safe_store(plan["store_id"]) / plan["plan_id"]
        root.mkdir(parents=True, exist_ok=True)
        (root / "homepage_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "hero_plan.json").write_text(json.dumps(plan["hero"], ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "category_shortcuts.json").write_text(json.dumps(plan["categories"], ensure_ascii=False, indent=2), encoding="utf-8")
        if preview:
            (root / "apply_preview.json").write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
        warnings = plan.get("warnings", []) + (preview or {}).get("warnings", [])
        summary = ["# Homepage automation report", "", f"Store: {plan['store_id']}", f"Plan: {plan['plan_id']}",
                   f"Hero: {plan['hero']['headline']}", f"CTA: {plan['hero']['cta_target'] or 'UNRESOLVED'}",
                   f"Categories: {len(plan['categories'])}", f"Apply status: {(preview or {}).get('status', 'NOT_PREVIEWED')}",
                   f"Warnings: {len(warnings)}", "", "No secret values are included.", ""]
        (root / "summary.md").write_text("\n".join(summary), encoding="utf-8")
        return {"folder": str(root), "summary": str(root / "summary.md")}


def validate_homepage_image(path: str | Path, *, minimum=(1600, 600), max_bytes=15_000_000) -> dict:
    target = Path(path)
    if not target.is_file(): return {"valid": False, "reason": "missing"}
    if target.stat().st_size <= 0 or target.stat().st_size > max_bytes: return {"valid": False, "reason": "file_size"}
    try:
        from PIL import Image
        with Image.open(target) as image:
            image.verify()
        with Image.open(target) as image:
            width, height, fmt = image.width, image.height, image.format
    except ModuleNotFoundError:
        return {"valid": False, "reason": "pillow_missing", "message_ko": "이미지 기능에 필요한 Pillow가 현재 실행 환경에 없습니다."}
    except Exception:
        return {"valid": False, "reason": "invalid_image"}
    valid = fmt in {"PNG", "JPEG", "WEBP"} and width >= minimum[0] and height >= minimum[1] and width / height >= 1.5
    return {"valid": valid, "width": width, "height": height, "format": fmt, "size_bytes": target.stat().st_size,
            "reason": None if valid else "unsupported_format_or_aspect_or_dimensions"}


def generate_hero_image(store_id: str, plan: dict, *, provider, enabled=False, output_dir: str | Path | None = None, db=None) -> dict:
    if getattr(provider, "provider_name", "") == "OPENAI_IMAGES" and not enabled:
        raise RuntimeError("유료 이미지 생성은 opt-in 후에만 가능합니다")
    root = Path(output_dir) if output_dir else EXPORT_DIR / "homepage_assets" / _safe_store(store_id) / plan["plan_id"] / "hero"
    root.mkdir(parents=True, exist_ok=True)
    output = root / "hero.png"
    result = provider.generate(plan["hero"]["image_prompt"], "1536x1024", output, **({"enabled": enabled} if getattr(provider, "provider_name", "") == "OPENAI_IMAGES" else {}))
    validation = validate_homepage_image(result.get("path", output), minimum=(1200, 600))
    if not validation["valid"]: raise ValueError(f"Generated hero image failed validation: {validation['reason']}")
    path = Path(result.get("path", output)); content = path.read_bytes(); digest = hashlib.sha256(content).hexdigest()
    asset_id = "HMA_" + digest[:20]; _install(db)
    with connect(db) as con:
        con.execute("INSERT OR REPLACE INTO store_homepage_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (asset_id, store_id, plan["plan_id"], "HERO", str(path.resolve()), getattr(provider, "provider_name", "MANUAL"), digest,
             validation["width"], validation["height"], validation["format"], "NEEDS_REVIEW", None, None, _now()))
    return {"asset_id": asset_id, "path": str(path), "sha256": digest, "validation": validation,
            "provider": getattr(provider, "provider_name", "MANUAL"), "approval_status": "NEEDS_REVIEW", "approved": False}


def register_manual_hero_asset(store_id: str, plan_id: str, filename: str, content: bytes, *, provider="MANUAL", db=None) -> dict:
    """Persist a user-selected hero image locally; DB contains metadata, not image bytes."""
    if Path(filename).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("Hero image must be PNG, JPEG, or WebP")
    root = EXPORT_DIR / "homepage_assets" / _safe_store(store_id) / _safe_store(plan_id) / "hero"
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    target = root / ("manual-" + digest[:12] + Path(filename).suffix.lower())
    target.write_bytes(content)
    validation = validate_homepage_image(target, minimum=(1200, 600))
    if not validation["valid"]:
        target.unlink(missing_ok=True)
        raise ValueError(f"Hero image failed validation: {validation['reason']}")
    asset_id = "HMA_" + digest[:20]
    _install(db)
    with connect(db) as con:
        con.execute("INSERT OR REPLACE INTO store_homepage_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (asset_id, store_id, plan_id, "HERO", str(target), str(provider), digest, validation["width"], validation["height"],
             validation["format"], "NEEDS_REVIEW", None, None, _now()))
    return {"asset_id": asset_id, "path": str(target), "sha256": digest, "approval_status": "NEEDS_REVIEW", "validation": validation}


def approve_hero_asset(asset_id: str, *, db=None) -> bool:
    _install(db)
    with connect(db) as con:
        row=con.execute("SELECT local_path FROM store_homepage_assets WHERE asset_id=?",(asset_id,)).fetchone()
        if not row:return False
        from .image_validation import inspect_image
        inspection=inspect_image(row["local_path"],asset_type="HERO_BANNER")
        if not inspection.get("valid"):
            if inspection.get("status")=="DEPENDENCY_MISSING":raise RuntimeError(inspection.get("message_ko"))
            return False
        return con.execute("UPDATE store_homepage_assets SET approval_status='APPROVED' WHERE asset_id=?", (asset_id,)).rowcount > 0


def latest_hero_asset(store_id: str, plan_id: str, *, db=None) -> dict | None:
    _install(db)
    with connect(db) as con:
        row = con.execute("SELECT * FROM store_homepage_assets WHERE store_id=? AND plan_id=? AND asset_type='HERO' ORDER BY created_at DESC,rowid DESC LIMIT 1", (store_id, plan_id)).fetchone()
    return dict(row) if row else None


def suggested_theme_image_ref(asset: dict) -> str:
    """Return a visible candidate; user must confirm it because CDN URL isn't the picker value."""
    filename = Path(str(asset.get("local_path", ""))).name
    if not filename or not re.fullmatch(r"[A-Za-z0-9._-]+", filename): return ""
    return "shopify://shop_images/" + filename


def upload_approved_hero_asset(store_id: str, asset_id: str, *, alt_text: str = "Homepage hero", db=None, client_factory=ShopifyGraphQLClient, uploader=None) -> dict:
    from .shopify_collections import ShopifyFileUploader
    _install(db)
    with connect(db) as con: row = con.execute("SELECT * FROM store_homepage_assets WHERE asset_id=? AND store_id=?", (asset_id, store_id)).fetchone()
    if not row or row["approval_status"] != "APPROVED": raise RuntimeError("Approve the hero asset before Shopify Files upload")
    if not Path(row["local_path"]).is_file(): raise FileNotFoundError(row["local_path"])
    config = get_connection(store_id, db=db); token, _ = get_shopify_token(store_id,db=db)
    if not config or not token: return {"status": "MANUAL_ACTION_REQUIRED", "reason": "Shopify credential missing"}
    client = client_factory(config["shop_domain"], token, config["api_version"])
    scopes = {scope.get("handle") for scope in (client.execute("query HomepageFileScopes { currentAppInstallation { accessScopes { handle } } }").get("currentAppInstallation") or {}).get("accessScopes", [])}
    if "write_files" not in scopes: return {"status": "MANUAL_ACTION_REQUIRED", "reason": "write_files scope missing"}
    uploaded = uploader.upload(row["local_path"], alt_text) if uploader else ShopifyFileUploader(client).upload(row["local_path"], alt_text)
    url = str(uploaded.get("url", ""))
    if not uploaded.get("id") or not url.startswith("https://"): return {"status": "FAILED", "reason": "Shopify Files URL validation failed"}
    with connect(db) as con: con.execute("UPDATE store_homepage_assets SET shopify_file_id=?,shopify_url=? WHERE asset_id=?", (uploaded["id"], url, asset_id))
    return {"status": "READY", "asset_id": asset_id, "shopify_file_id": uploaded["id"], "shopify_url": url}


def assignment_banner_check(plan: dict) -> dict:
    hero = plan["hero"]
    return {"headline": hero["headline"], "body": hero["body"], "cta": hero["cta_label"], "cta_link": hero["cta_target"],
            "image": hero.get("image_url") or hero.get("image_asset_id"), "alt_text": hero.get("alt_text"),
            "warnings": ([] if hero.get("cta_valid") else ["CTA target is unresolved"]) + ([] if hero.get("image_asset_id") or hero.get("image_url") else ["Hero image missing"])}


def assignment_category_check(plan: dict) -> dict:
    warnings = list(plan.get("warnings", []))
    for item in plan.get("categories", []):
        if not item.get("title"): warnings.append({"code": "MISSING_TITLE", "collection_key": item["collection_key"]})
        if not item.get("target"): warnings.append({"code": "BROKEN_LINK", "collection_key": item["collection_key"]})
        if not item.get("alt_text"): warnings.append({"code": "MISSING_ALT", "collection_key": item["collection_key"]})
    return {"items": plan.get("categories", []), "warnings": warnings, "valid": not warnings}
