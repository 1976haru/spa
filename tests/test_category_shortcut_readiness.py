from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
import pytest

from shopsource.category_shortcut_readiness import (
    CategoryShortcutReadinessService,
    _category_match,
    _catalog_counts,
    readiness_summary,
)
from shopsource.db import connect, init_db
from shopsource.homepage_featured_products import _install


@pytest.fixture
def db():
    handle = tempfile.NamedTemporaryFile(prefix="shopsource-category-", suffix=".sqlite3", delete=False)
    handle.close()
    path = Path(handle.name)
    try:
        yield path
    finally:
        for target in (path, Path(str(path) + "-shm"), Path(str(path) + "-wal"), Path(str(path) + ".png"), Path(str(path) + ".invalid.png")):
            target.unlink(missing_ok=True)


def _product(product_id, title, product_type, **extra):
    return {"shopify_product_id": f"gid://shopify/Product/{product_id}",
            "shopify_handle": f"item-{product_id}", "title": title, "product_type": product_type,
            "tags": [], "remote_status": "ACTIVE", "eligible": True,
            "storefront_eligible": True, "verification_status": "REMOTE_READ_VERIFIED", **extra}


def _seed(db, products):
    init_db(db)
    _install(db)
    with connect(db) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS shopify_collection_mappings(
            store_id TEXT,collection_key TEXT,handle TEXT,shopify_collection_id TEXT,
            last_synced_hash TEXT,last_synced_at TEXT,published_ids_json TEXT,image_url TEXT,
            PRIMARY KEY(store_id,collection_key))""")
        con.execute("""CREATE TABLE IF NOT EXISTS collection_image_assets(
            store_id TEXT,collection_key TEXT,path TEXT,provider TEXT,model TEXT,alt_text TEXT,
            metadata_json TEXT,created_at TEXT,approval_status TEXT,PRIMARY KEY(store_id,collection_key))""")
        con.execute("""CREATE TABLE IF NOT EXISTS shopify_collection_mappings(
            store_id TEXT,collection_key TEXT,handle TEXT,shopify_collection_id TEXT,
            last_synced_hash TEXT,last_synced_at TEXT,published_ids_json TEXT,image_url TEXT,
            PRIMARY KEY(store_id,collection_key))""")
        con.execute("""CREATE TABLE IF NOT EXISTS homepage_category_collection_remote_cache(
            store_id TEXT PRIMARY KEY,fetched_at TEXT,source_hash TEXT,collections_json TEXT)""")
        con.execute("INSERT INTO homepage_featured_product_remote_cache VALUES(?,?,?,?,?)",
                    ("001", datetime.now(timezone.utc).isoformat(), len(products), "source-hash",
                     json.dumps(products)))


def test_counts_use_active_eligible_and_exclude_repair_parts():
    products = [
        _product(1, "Cargo trunk organizer", "Automotive Cargo"),
        _product(2, "Seat back storage organizer", "Automotive Interior"),
        _product(3, "Silicone car trash can", "Garbage Cans"),
        _product(4, "Center console organizer", "Automotive Interior"),
        _product(5, "Center console replacement trim panel", "Console Accessories"),
        _product(6, "Cup holder insert", "Convenience", eligible=False),
        _product(1, "Duplicate record", "Trunk"),
    ]
    counts, excluded = _catalog_counts(products)
    assert counts["trunk-cargo"] == 1
    assert counts["seat-backseat"] == 1
    assert counts["trash-cleanup"] == 1
    assert counts["console-small-storage"] == 1
    assert excluded["repair_or_fitment_part"] == 1
    assert excluded["not_active_or_ineligible"] == 1


def test_existing_shopsource_collection_mapping_signal_precedes_product_text():
    assert _category_match({"collection_key": "console-storage", "product_type": "Seat organizer"}) == "console-small-storage"


def test_build_selects_four_nonempty_deterministically_and_never_fakes_mapping(db):
    products = [
        _product(1, "Cargo trunk organizer", "Automotive Cargo"),
        _product(2, "Seat back storage organizer", "Automotive Interior"),
        _product(3, "Silicone car trash can", "Garbage Cans"),
        _product(4, "Center console organizer", "Automotive Interior"),
        _product(5, "Cup holder insert", "Cup Holder"),
    ]
    _seed(db, products)
    service = CategoryShortcutReadinessService(db)
    package = service.build("001")
    assert package["status"] == "CATEGORIES_SELECTED_PREREQUISITES_BLOCKED"
    assert len(package["items"]) == 4
    assert all(item["product_count"] > 0 for item in package["items"])
    assert all(item["mapping_status"] == "NOT_MAPPED" for item in package["items"])
    assert all(item["shopify_collection_id"] is None and item["handle"] is None for item in package["items"])
    assert all(item["storefront_url"] is None for item in package["items"])
    assert all(item["image_status"] == "NEEDS_ASSET" for item in package["items"])
    assert package["summary"]["mapping_ready"] == package["summary"]["image_ready"] == 0
    assert package["summary"]["preview_enabled"] is False
    assert service.latest("001")["plan_id"] == package["plan_id"]
    assert service.build("001", persist=False)["items"] == package["items"]


def _seed_trunk_mapping(db, *, publication_ids=None, remote_id="gid://shopify/Collection/77",
                        remote_handle="trunk-organizers", remote_count=None):
    with connect(db) as con:
        con.execute("INSERT INTO shopify_collection_mappings VALUES(?,?,?,?,?,?,?,?)",
                    ("001", "trunk-storage", "trunk-organizers", "gid://shopify/Collection/77", "h", "2026-10-09",
                     json.dumps(publication_ids if publication_ids is not None else ["gid://shopify/Publication/123"]), None))
        if remote_count is not None:
            con.execute("INSERT OR REPLACE INTO homepage_category_collection_remote_cache VALUES(?,?,?,?)",
                        ("001", datetime.now(timezone.utc).isoformat(), "remote-hash", json.dumps([{
                            "id": remote_id, "handle": remote_handle, "title": "Trunk Organizers",
                            "products_count": remote_count, "products_count_precision": "EXACT"
                        }])) )


def _category_package(db):
    _seed(db, [_product(i, title, kind) for i, title, kind in [
        (1, "Cargo trunk organizer", "Cargo"), (2, "Seat back organizer", "Seat"),
        (3, "Car trash can", "Garbage"), (4, "Center console organizer", "Console"),
    ]])
    return CategoryShortcutReadinessService(db)


def test_publication_ids_are_not_product_count(db):
    service = _category_package(db)
    _seed_trunk_mapping(db, remote_count=9)
    package = service.build("001", persist=False)
    trunk = next(item for item in package["items"] if item["collection_key"] == "trunk-storage")
    assert trunk["publication_ids"] == ["gid://shopify/Publication/123"]
    assert trunk["remote_product_count"] == 9
    assert trunk["remote_product_count"] != len(trunk["publication_ids"])
    assert trunk["remote_count_status"] == "VERIFIED"
    assert trunk["publication_status"] == "KNOWN_PUBLISHED"


def test_mapping_with_publication_id_but_no_remote_count_not_ready(db):
    service = _category_package(db)
    _seed_trunk_mapping(db)
    trunk = next(item for item in service.build("001", persist=False)["items"] if item["collection_key"] == "trunk-storage")
    assert trunk["mapping_status"] == "REMOTE_NOT_VERIFIED"
    assert trunk["mapping_identity_status"] == "REMOTE_NOT_VERIFIED"
    assert trunk["remote_product_count"] is None


def test_mapping_with_remote_zero_products_not_ready(db):
    service = _category_package(db)
    _seed_trunk_mapping(db, remote_count=0)
    trunk = next(item for item in service.build("001", persist=False)["items"] if item["collection_key"] == "trunk-storage")
    assert trunk["mapping_status"] == "REMOTE_EMPTY"
    assert trunk["mapping_identity_status"] == "VERIFIED"
    assert trunk["remote_count_status"] == "VERIFIED"


def test_mapping_with_verified_remote_positive_count_ready_for_identity_count(db):
    service = _category_package(db)
    _seed_trunk_mapping(db, remote_count=9)
    trunk = next(item for item in service.build("001", persist=False)["items"] if item["collection_key"] == "trunk-storage")
    assert trunk["mapping_status"] == "READY"
    assert trunk["mapping_identity_status"] == "VERIFIED"
    assert trunk["remote_product_count"] == 9
    assert trunk["shopify_collection_id"] == "gid://shopify/Collection/77"
    assert trunk["handle"] == "trunk-organizers"
    assert trunk["storefront_url"] == "/collections/trunk-organizers"


def test_remote_collection_identity_mismatch_blocks_ready(db):
    service = _category_package(db)
    _seed_trunk_mapping(db, remote_id="gid://shopify/Collection/77", remote_handle="different-handle", remote_count=9)
    trunk = next(item for item in service.build("001", persist=False)["items"] if item["collection_key"] == "trunk-storage")
    assert trunk["mapping_status"] == "IDENTITY_MISMATCH"
    assert trunk["mapping_identity_status"] == "IDENTITY_MISMATCH"
    assert trunk["remote_product_count"] is None


def test_missing_publication_evidence_does_not_make_mapping_ready(db):
    service = _category_package(db)
    _seed_trunk_mapping(db, publication_ids=[], remote_count=9)
    trunk = next(item for item in service.build("001", persist=False)["items"] if item["collection_key"] == "trunk-storage")
    assert trunk["mapping_identity_status"] == "VERIFIED"
    assert trunk["remote_count_status"] == "VERIFIED"
    assert trunk["publication_status"] == "UNKNOWN"
    assert trunk["mapping_status"] == "NOT_READY"


def test_remote_snapshot_cache_reused(db, monkeypatch):
    service = _category_package(db)
    _seed_trunk_mapping(db)
    calls = []
    class FakeClient:
        def __init__(self, *args): pass
        def execute(self, query):
            calls.append(query)
            return {"collections": {"nodes": [{"id": "gid://shopify/Collection/77", "handle": "trunk-organizers",
                "title": "Trunk", "productsCount": {"count": 9, "precision": "EXACT"}}]}}
    monkeypatch.setattr("shopsource.shopify_collections.get_connection", lambda *a, **k: {"shop_domain": "test.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr("shopsource.shopify_collections.get_shopify_token", lambda *a, **k: ("fake-token", "test"))
    monkeypatch.setattr("shopsource.shopify_collections.ShopifyGraphQLClient", FakeClient)
    assert service.refresh_collection_snapshot("001")["status"] == "READ_ONLY_REFRESHED"
    assert service.collection_snapshot("001")["collections"][0]["products_count"] == 9
    trunk = next(item for item in service.build("001", persist=False)["items"] if item["collection_key"] == "trunk-storage")
    assert trunk["remote_product_count"] == 9 and trunk["mapping_identity_status"] == "VERIFIED"
    assert len(calls) == 1


def test_remote_snapshot_force_refresh_read_only(db, monkeypatch):
    service = _category_package(db)
    calls = []
    class FakeClient:
        def __init__(self, *args): pass
        def execute(self, query):
            calls.append(query)
            return {"collections": {"nodes": []}}
    monkeypatch.setattr("shopsource.shopify_collections.get_connection", lambda *a, **k: {"shop_domain": "test.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr("shopsource.shopify_collections.get_shopify_token", lambda *a, **k: ("fake-token", "test"))
    monkeypatch.setattr("shopsource.shopify_collections.ShopifyGraphQLClient", FakeClient)
    assert service.refresh_collection_snapshot("001")["status"] == "READ_ONLY_REFRESHED"
    assert len(calls) == 1 and "productsCount" in calls[0] and "mutation" not in calls[0].casefold()


def test_no_collection_mutation(db, monkeypatch):
    service = _category_package(db)
    queries = []
    class FakeClient:
        def __init__(self, *args): pass
        def execute(self, query):
            queries.append(query)
            assert "mutation" not in query.casefold()
            return {"collections": {"nodes": []}}
    monkeypatch.setattr("shopsource.shopify_collections.get_connection", lambda *a, **k: {"shop_domain": "test.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr("shopsource.shopify_collections.get_shopify_token", lambda *a, **k: ("fake-token", "test"))
    monkeypatch.setattr("shopsource.shopify_collections.ShopifyGraphQLClient", FakeClient)
    service.refresh_collection_snapshot("001")
    assert len(queries) == 1


def test_no_theme_mutation():
    from shopsource.category_shortcut_readiness import readiness_summary
    package = {"items": [], "theme_schema_status": "WAITING_FOR_LIVE_READ"}
    assert readiness_summary(package)["preview_enabled"] is False


def test_only_approved_valid_reviewed_unique_image_is_ready(db):
    products = [_product(i, title, kind) for i, title, kind in [
        (1, "Cargo trunk organizer", "Cargo"), (2, "Seat back organizer", "Seat"),
        (3, "Car trash can", "Garbage"), (4, "Center console organizer", "Console"),
    ]]
    _seed(db, products)
    image = Path(str(db) + ".png")
    Image.new("RGB", (900, 900), "white").save(image)
    with connect(db) as con:
        con.execute("INSERT INTO collection_image_assets VALUES(?,?,?,?,?,?,?,?,?)",
                    ("001", "trunk-storage", str(image), "MANUAL", "", "Trunk storage",
                     json.dumps({"content_review": {"no_text": True, "no_logo": True, "no_watermark": True}}),
                     datetime.now(timezone.utc).isoformat(), "APPROVED"))
        con.execute("INSERT INTO collection_image_assets VALUES(?,?,?,?,?,?,?,?,?)",
                    ("001", "seat-organization", str(image), "MANUAL", "", "Seat storage",
                     json.dumps({"content_review": {"no_text": True, "no_logo": True, "no_watermark": True}}),
                     datetime.now(timezone.utc).isoformat(), "APPROVED"))
    package = CategoryShortcutReadinessService(db).build("001", persist=False)
    ready = [item for item in package["items"] if item["image_status"] == "READY"]
    assert len(ready) == 1
    assert ready[0]["image_asset_path"] == str(image.resolve())
    assert sum(item["image_status"] == "NEEDS_ASSET" for item in package["items"]) == 3


def test_invalid_or_unapproved_category_artwork_never_becomes_ready(db):
    products = [_product(i, title, kind) for i, title, kind in [
        (1, "Cargo trunk organizer", "Cargo"), (2, "Seat back organizer", "Seat"),
        (3, "Car trash can", "Garbage"), (4, "Center console organizer", "Console"),
    ]]
    _seed(db, products)
    invalid = Path(str(db) + ".invalid.png")
    invalid.write_bytes(b"not an image")
    valid_unapproved = Path(str(db) + ".png")
    Image.new("RGB", (900, 900), "white").save(valid_unapproved)
    with connect(db) as con:
        for key, path, status in (("trunk-storage", invalid, "APPROVED"),
                                  ("seat-organization", valid_unapproved, "NEEDS_REVIEW")):
            con.execute("INSERT INTO collection_image_assets VALUES(?,?,?,?,?,?,?,?,?)",
                        ("001", key, str(path), "MANUAL", "", "category image", "{}",
                         datetime.now(timezone.utc).isoformat(), status))
    package = CategoryShortcutReadinessService(db).build("001", persist=False)
    assert all(item["image_status"] == "NEEDS_ASSET" for item in package["items"])


def test_prompt_only_and_readiness_summary_keep_preview_blocked():
    from shopsource.category_shortcut_readiness import category_image_prompt
    prompt = category_image_prompt("Trunk & Cargo")
    assert "no text" in prompt and "no logos" in prompt and "no watermark" in prompt
    assert readiness_summary({"items": [{"mapping_status": "READY", "image_status": "READY"} for _ in range(4)],
                              "theme_schema_status": "WAITING_FOR_LIVE_READ"})["preview_enabled"] is False
