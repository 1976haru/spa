from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
import pytest

from shopsource.category_shortcut_readiness import (
    CategoryShortcutReadinessService,
    _eligible_products,
    category_image_prompt,
    readiness_summary,
)
from shopsource.db import connect, init_db, upsert_store


@pytest.fixture
def db(tmp_path):
    return tmp_path / "category-readiness.sqlite3"


def _product(product_id, title, product_type, **extra):
    return {
        "shopify_product_id": f"gid://shopify/Product/{product_id}",
        "shopify_handle": f"item-{product_id}",
        "title": title,
        "product_type": product_type,
        "tags": [],
        "remote_status": "ACTIVE",
        "eligible": True,
        "storefront_eligible": True,
        "verification_status": "REMOTE_READ_VERIFIED",
        **extra,
    }


def _seed(db, store_id, store_name, products, *, profile=None):
    init_db(db)
    store = {
        "store_id": store_id,
        "store_name": store_name,
        "category": "General merchandise",
        **(profile or {}),
    }
    upsert_store(store, db)
    CategoryShortcutReadinessService(db)
    with connect(db) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS shopify_collection_mappings(
            store_id TEXT,collection_key TEXT,handle TEXT,shopify_collection_id TEXT,
            last_synced_hash TEXT,last_synced_at TEXT,published_ids_json TEXT,image_url TEXT,
            PRIMARY KEY(store_id,collection_key))""")
        con.execute("""CREATE TABLE IF NOT EXISTS collection_image_assets(
            store_id TEXT,collection_key TEXT,path TEXT,provider TEXT,model TEXT,alt_text TEXT,
            metadata_json TEXT,created_at TEXT,approval_status TEXT,PRIMARY KEY(store_id,collection_key))""")
        con.execute(
            "INSERT OR REPLACE INTO homepage_featured_product_remote_cache VALUES(?,?,?,?,?)",
            (store_id, datetime.now(timezone.utc).isoformat(), len(products),
             f"source-{store_id}", json.dumps(products)),
        )
    return store


def _add_collection_plan(db, store_id, definitions, *, plan_id="plan-1", version=1):
    now = datetime.now(timezone.utc).isoformat()
    with connect(db) as con:
        con.execute("""INSERT INTO store_collection_plans
            (plan_id,store_id,version,planner_version,status,created_at,updated_at)
            VALUES(?,?,?,'test','DRAFT',?,?)""", (plan_id, store_id, version, now, now))
        for priority, definition in enumerate(definitions):
            cursor = con.execute("""INSERT INTO store_collection_definitions
                (plan_id,collection_key,title,handle,priority,enabled,match_mode,created_at,updated_at)
                VALUES(?,?,?,?,?,1,?,?,?)""",
                (plan_id, definition["key"], definition["title"], definition.get("handle", ""),
                 priority, definition.get("match_mode", "ANY"), now, now))
            for condition in definition.get("conditions", []):
                con.execute("""INSERT INTO store_collection_conditions
                    (collection_definition_id,field,relation,value,group_operator,priority)
                    VALUES(?,?,?,?,?,?)""",
                    (cursor.lastrowid, condition["field"], condition.get("relation", "CONTAINS"),
                     condition["value"], "OR", 0))


def _add_mapping(db, store_id, key, *, collection_id="gid://shopify/Collection/77",
                 handle="example-collection", publication_ids=None):
    with connect(db) as con:
        con.execute("""INSERT INTO shopify_collection_mappings
            (store_id,collection_key,handle,shopify_collection_id,last_synced_hash,last_synced_at,published_ids_json,image_url)
            VALUES(?,?,?,?,?,?,?,NULL)""",
            (store_id, key, handle, collection_id, "hash", datetime.now(timezone.utc).isoformat(),
             json.dumps(publication_ids if publication_ids is not None else ["gid://shopify/Publication/123"])))


def _add_remote_snapshot(db, store_id, *, collection_id="gid://shopify/Collection/77",
                         handle="example-collection", count=8):
    now = datetime.now(timezone.utc).isoformat()
    collections = [{"id": collection_id, "handle": handle, "title": "Example",
                    "products_count": count, "products_count_precision": "EXACT"}]
    with connect(db) as con:
        con.execute("""INSERT OR REPLACE INTO homepage_category_collection_remote_cache
            VALUES(?,?,?,?)""", (store_id, now, "remote-hash", json.dumps(collections)))


def test_collection_plan_drives_cabin_tidy_categories_and_profile_exclusions(db):
    products = [
        _product(1, "Car trunk cargo organizer", "Automotive storage"),
        _product(2, "Backseat organizer", "Automotive storage"),
        _product(3, "Center console organizer", "Automotive storage"),
        _product(4, "Car trash bin", "Automotive storage"),
        _product(5, "Center console replacement fitment panel", "Automotive part"),
    ]
    store = _seed(db, "001", "Cabin Tidy", products, profile={
        "primary_category": "Automotive Interior",
        "exclude_keywords": ["replacement", "fitment"],
    })
    _add_collection_plan(db, "001", [
        {"key": "trunk-storage", "title": "Trunk & Cargo",
         "conditions": [{"field": "TITLE", "value": "trunk"}]},
        {"key": "seat-organization", "title": "Seat & Backseat",
         "conditions": [{"field": "TITLE", "value": "backseat"}]},
        {"key": "console-storage", "title": "Console & Small Storage",
         "conditions": [{"field": "TITLE", "value": "console"}]},
        {"key": "trash-cleanup", "title": "Trash & Cleanup",
         "conditions": [{"field": "TITLE", "value": "trash"}]},
    ])
    package = CategoryShortcutReadinessService(db).build("001", persist=False)
    assert package["candidate_source"] == "COLLECTION_PLAN"
    assert {item["title"] for item in package["items"]} == {
        "Trunk & Cargo", "Seat & Backseat", "Console & Small Storage", "Trash & Cleanup"
    }
    console = next(item for item in package["items"] if item["collection_key"] == "console-storage")
    assert console["product_count"] == 1
    assert all(item["candidate_source"] == "COLLECTION_PLAN" for item in package["items"])
    assert "Automotive Interior" in package["items"][0]["image_prompt"]
    assert package["items"][0]["proposed_handle"].startswith("cabin-tidy-")
    assert store["store_name"] == "Cabin Tidy"


def test_non_automotive_collection_plan_and_visual_context_are_portable(db):
    products = [
        _product(11, "Pantry containers for dry goods", "Pantry storage"),
        _product(12, "Under-sink organizer basket", "Under-sink storage"),
        _product(13, "Kitchen drawer divider set", "Drawer organization"),
        _product(14, "Cabinet rack shelf organizer", "Cabinet storage"),
        _product(15, "Refrigerator bins for produce", "Refrigerator organization"),
    ]
    _seed(db, "002", "Hearth & Order", products, profile={
        "primary_category": "Home & Kitchen Organization",
        "brand_personality": ["practical", "calm"],
        "visual_direction": "warm natural wood, bright neutral background",
    })
    _add_collection_plan(db, "002", [
        {"key": "pantry-containers", "title": "Pantry Containers",
         "conditions": [{"field": "TITLE", "value": "pantry"}]},
        {"key": "under-sink", "title": "Under-Sink Organizers",
         "conditions": [{"field": "TITLE", "value": "under-sink"}]},
        {"key": "drawer-dividers", "title": "Drawer Dividers",
         "conditions": [{"field": "TITLE", "value": "drawer"}]},
        {"key": "cabinet-racks", "title": "Cabinet Racks",
         "conditions": [{"field": "TITLE", "value": "cabinet"}]},
        {"key": "refrigerator-bins", "title": "Refrigerator Bins",
         "conditions": [{"field": "TITLE", "value": "refrigerator"}]},
    ])
    package = CategoryShortcutReadinessService(db).build("002", persist=False)
    titles = {item["title"] for item in package["items"]}
    assert package["candidate_source"] == "COLLECTION_PLAN"
    assert len(titles) == 4
    assert titles.issubset({"Pantry Containers", "Under-Sink Organizers", "Drawer Dividers",
                           "Cabinet Racks", "Refrigerator Bins"})
    assert not any(word in " ".join(titles).casefold() for word in ("automotive", "car", "vehicle", "trunk", "seat"))
    prompt = package["items"][0]["image_prompt"].casefold()
    assert "home & kitchen organization" in prompt
    assert not any(word in prompt for word in ("automotive", "car interior", "vehicle"))
    assert all(item["proposed_handle"].startswith("hearth-order-") for item in package["items"])


def test_product_derived_fallback_is_review_required_and_store_handles_are_isolated(db):
    labels = ("Pantry Containers", "Under-Sink Baskets", "Drawer Dividers", "Cabinet Racks")
    products = [_product(i, f"{title} {i}", title) for i, title in enumerate(labels, start=20)]
    _seed(db, "hearth", "Hearth & Order", products, profile={"primary_category": "Home Organization"})
    _seed(db, "loom", "Loom & Leaf", products, profile={"primary_category": "Home Organization"})
    service = CategoryShortcutReadinessService(db)
    hearth = service.build("hearth", persist=False)
    loom = service.build("loom", persist=False)
    assert hearth["candidate_source"] == loom["candidate_source"] == "PRODUCT_DERIVED_FALLBACK"
    assert len(hearth["items"]) == len(loom["items"]) == 4
    assert all(item["candidate_status"] == "REVIEW_REQUIRED" for item in hearth["items"] + loom["items"])
    hearth_handles = {item["proposed_handle"] for item in hearth["items"]}
    loom_handles = {item["proposed_handle"] for item in loom["items"]}
    assert all(handle.startswith("hearth-order-") for handle in hearth_handles)
    assert all(handle.startswith("loom-leaf-") for handle in loom_handles)
    assert hearth_handles.isdisjoint(loom_handles)
    assert hearth["store_id"] == "hearth"
    assert loom["store_id"] == "loom"


def test_explicit_store_profile_strategy_precedes_product_fallback(db):
    products = [_product(28, "Pantry container bins", "Pantry")]
    _seed(db, "profile-store", "Hearth & Order", products, profile={
        "category_shortcut_strategy": {"categories": [{
            "category_key": "pantry-storage", "title": "Pantry Storage",
            "collection_key": "pantry-storage", "match_signals": ["pantry"],
        }]},
    })
    package = CategoryShortcutReadinessService(db).build("profile-store", persist=False)
    assert package["candidate_source"].startswith("STORE_PROFILE")
    assert len(package["items"]) == 4
    assert package["items"][0]["candidate_source"] == "STORE_PROFILE"
    assert package["items"][0]["candidate_status"] == "STORE_DATA"
    assert all(item["candidate_status"] == "REVIEW_REQUIRED" for item in package["items"][1:])


def test_latest_plan_without_product_evidence_is_skipped_for_older_usable_plan(db):
    _seed(db, "plans", "Plan Store", [_product(29, "Pantry container", "Pantry")])
    service = CategoryShortcutReadinessService(db)
    _add_collection_plan(db, "plans", [{"key": "pantry", "title": "Pantry Containers",
        "conditions": [{"field": "TITLE", "value": "pantry"}]}], plan_id="usable-plan", version=1)
    _add_collection_plan(db, "plans", [{"key": "bath", "title": "Bath Accessories",
        "conditions": [{"field": "TITLE", "value": "bath"}]}], plan_id="empty-plan", version=2)
    package = service.build("plans", persist=False)
    assert package["candidate_source"].startswith("COLLECTION_PLAN")
    assert package["items"][0]["collection_key"] == "pantry"
    assert package["items"][0]["local_collection_definition"]["plan_id"] == "usable-plan"
    assert all(item["candidate_status"] == "REVIEW_REQUIRED" for item in package["items"][1:])


def test_store_profile_exclusions_and_existing_product_decisions_are_respected():
    products = [
        _product(31, "Pantry container set", "Pantry"),
        _product(32, "Restricted pantry item", "Pantry", final_status="RESTRICTED"),
        _product(33, "Disallowed pantry item", "Pantry"),
    ]
    eligible, excluded = _eligible_products(products, {"exclude_keywords": ["disallowed"]})
    assert [item["shopify_product_id"] for item in eligible] == ["gid://shopify/Product/31"]
    assert excluded["decision_or_risk_exclusion"] == 1
    assert excluded["store_profile_exclusion"] == 1


def test_remote_collection_evidence_semantics_remain_separate(db):
    products = [_product(40, "Pantry container", "Pantry")]
    _seed(db, "hearth", "Hearth & Order", products, profile={"primary_category": "Home Organization"})
    _add_collection_plan(db, "hearth", [{"key": "pantry", "title": "Pantry Containers",
        "conditions": [{"field": "TITLE", "value": "pantry"}]}])
    _add_mapping(db, "hearth", "pantry", handle="pantry-containers")
    service = CategoryShortcutReadinessService(db)
    without_snapshot = service.build("hearth", persist=False)["items"][0]
    assert without_snapshot["publication_ids"] == ["gid://shopify/Publication/123"]
    assert without_snapshot["remote_product_count"] is None
    assert without_snapshot["mapping_status"] == "REMOTE_NOT_VERIFIED"
    _add_remote_snapshot(db, "hearth", handle="pantry-containers", count=0)
    empty = service.build("hearth", persist=False)["items"][0]
    assert empty["remote_count_status"] == "VERIFIED"
    assert empty["remote_product_count"] == 0
    assert empty["mapping_status"] == "REMOTE_EMPTY"
    _add_remote_snapshot(db, "hearth", handle="pantry-containers", count=8)
    nonempty = service.build("hearth", persist=False)["items"][0]
    assert nonempty["mapping_identity_status"] == "VERIFIED"
    assert nonempty["remote_product_count"] == 8
    assert nonempty["remote_count_status"] == "VERIFIED"
    assert nonempty["publication_status"] == "KNOWN_PUBLISHED"
    assert nonempty["mapping_status"] == "READY"


def test_remote_identity_mismatch_blocks_readiness(db):
    _seed(db, "hearth", "Hearth & Order", [_product(41, "Pantry container", "Pantry")])
    _add_collection_plan(db, "hearth", [{"key": "pantry", "title": "Pantry Containers",
        "conditions": [{"field": "TITLE", "value": "pantry"}]}])
    _add_mapping(db, "hearth", "pantry", handle="pantry-containers")
    _add_remote_snapshot(db, "hearth", handle="different-handle", count=8)
    item = CategoryShortcutReadinessService(db).build("hearth", persist=False)["items"][0]
    assert item["mapping_identity_status"] == "IDENTITY_MISMATCH"
    assert item["remote_product_count"] is None
    assert item["mapping_status"] == "IDENTITY_MISMATCH"


def test_remote_snapshot_refresh_is_read_only_and_cached(db, monkeypatch):
    _seed(db, "hearth", "Hearth & Order", [_product(42, "Pantry container", "Pantry")])
    service = CategoryShortcutReadinessService(db)
    calls = []

    class FakeClient:
        def __init__(self, *args):
            pass

        def execute(self, query):
            calls.append(query)
            return {"collections": {"nodes": [{
                "id": "gid://shopify/Collection/77", "handle": "pantry-containers",
                "title": "Pantry Containers", "productsCount": {"count": 5, "precision": "EXACT"},
            }]}}

    monkeypatch.setattr("shopsource.shopify_collections.get_connection",
                        lambda *a, **k: {"shop_domain": "test.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr("shopsource.shopify_collections.get_shopify_token", lambda *a, **k: ("fake-token", "test"))
    monkeypatch.setattr("shopsource.shopify_collections.ShopifyGraphQLClient", FakeClient)
    result = service.refresh_collection_snapshot("hearth")
    assert result["status"] == "READ_ONLY_REFRESHED"
    assert service.collection_snapshot("hearth")["collections"][0]["products_count"] == 5
    assert len(calls) == 1
    assert "productsCount" in calls[0] and "mutation" not in calls[0].casefold()


def test_approved_unique_image_validation_is_preserved(db):
    products = [_product(i, title, category) for i, (title, category) in enumerate([
        ("Pantry container", "Pantry"), ("Under-sink basket", "Under Sink"),
        ("Drawer divider", "Drawer"), ("Cabinet rack", "Cabinet"),
    ], start=50)]
    _seed(db, "hearth", "Hearth & Order", products, profile={"primary_category": "Home Organization"})
    definitions = [
        ("pantry", "Pantry Containers", "pantry"),
        ("under-sink", "Under-Sink Baskets", "under-sink"),
        ("drawer", "Drawer Dividers", "drawer"),
        ("cabinet", "Cabinet Racks", "cabinet"),
    ]
    _add_collection_plan(db, "hearth", [
        {"key": key, "title": title, "conditions": [{"field": "TITLE", "value": signal}]}
        for key, title, signal in definitions
    ])
    image_path = Path(db).with_suffix(".png")
    Image.new("RGB", (900, 900), "white").save(image_path)
    with connect(db) as con:
        con.execute("""INSERT INTO collection_image_assets
            (store_id,collection_key,path,provider,model,alt_text,metadata_json,created_at,approval_status)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            ("hearth", "pantry", str(image_path), "MANUAL", "", "Pantry",
             json.dumps({"content_review": {"no_text": True, "no_logo": True, "no_watermark": True}}),
             datetime.now(timezone.utc).isoformat(), "APPROVED"))
    package = CategoryShortcutReadinessService(db).build("hearth", persist=False)
    assert sum(item["image_status"] == "READY" for item in package["items"]) == 1
    assert sum(item["image_status"] == "NEEDS_ASSET" for item in package["items"]) == 3


def test_generic_image_prompt_uses_store_and_brand_context():
    prompt = category_image_prompt("Pantry Containers", {
        "primary_category": "Home & Kitchen Organization",
        "visual_direction": "warm natural wood",
        "brand_personality": ["calm", "practical"],
    })
    assert "Home & Kitchen Organization" in prompt
    assert "warm natural wood" in prompt
    assert not any(word in prompt.casefold() for word in ("automotive", "car interior", "vehicle"))


def test_readiness_summary_keeps_preview_blocked_without_prerequisites():
    package = {"items": [{"mapping_status": "READY", "image_status": "READY"} for _ in range(4)],
               "theme_schema_status": "WAITING_FOR_LIVE_READ"}
    assert readiness_summary(package)["preview_enabled"] is False
    assert readiness_summary(package)["theme_write_status"] == "NOT_RUN"
    fallback_package = {"items": [{"mapping_status": "READY", "image_status": "READY",
                                    "candidate_status": "REVIEW_REQUIRED"} for _ in range(4)],
                        "theme_schema_status": "READY"}
    assert readiness_summary(fallback_package)["preview_enabled"] is False


def test_core_module_has_no_store_or_automotive_taxonomy_hardcoding():
    source = Path(__import__("shopsource.category_shortcut_readiness", fromlist=["__file__"]).__file__).read_text(encoding="utf-8").casefold()
    assert "cabin-tidy-" not in source
    assert "cabin tidy" not in source
    assert "automotive" not in source
    assert "_repair_part" not in source
    assert "category_definitions" not in source
