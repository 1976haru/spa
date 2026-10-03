from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from shopsource.db import connect, init_db, upsert_store
from shopsource.shopify_collections import save_connection
from shopsource.shopify_products import (
    DirectShopifyProductPublisher, PRODUCT_SET_MUTATION, collection_tag,
    identity_handle, source_identity,
)


@pytest.fixture
def tmp_path():
    path = Path.cwd() / "exports" / ".test_scratch" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    return path


class FakeProductShopify:
    def __init__(self):
        self.rows = {}
        self.writes = []
        self.request_bodies = []
        self.scopes = {"read_products", "write_products"}
        self.user_errors = False
        self.force_mismatch = False

    def __call__(self, domain, token, version):
        assert domain == "fixture.myshopify.com"
        assert token == "test-token-never-log"
        assert version == "2026-07"
        return self

    def execute(self, query, variables=None):
        variables = variables or {}
        self.request_bodies.append((query, variables))
        if "currentAppInstallation" in query:
            return {"currentAppInstallation": {"accessScopes": [{"handle": scope} for scope in self.scopes]}}
        if "productByIdentifier" in query:
            ident = variables["identifier"]
            handle = ident.get("handle")
            return {"productByIdentifier": next((row for row in self.rows.values() if row["handle"] == handle), None)}
        if "ShopSourceProductById" in query:
            product = self.rows.get(variables["id"])
            if product and self.force_mismatch and self.writes:
                product = {**product, "status": "ACTIVE"}
            return {"product": product}
        if "productSet(" in query:
            self.writes.append("productSet")
            if self.user_errors:
                return {"productSet": {"product": None, "userErrors": [{"field": ["title"], "message": "rejected"}]}}
            payload = variables["input"]
            product = next((row for row in self.rows.values() if row["handle"] == payload["handle"]), None)
            if not product:
                product = {"id": f"gid://shopify/Product/{len(self.rows)+1}", "handle": payload["handle"],
                           "variants": {"nodes": [{"id": f"gid://shopify/ProductVariant/{len(self.rows)+1}", "price": "0.00", "compareAtPrice": None}]},
                           "variantsCount": {"count": 1}, "tags": ["merchant:featured"]}
                self.rows[product["id"]] = product
            product.update(payload)
            return {"productSet": {"product": product, "userErrors": []}}
        if "productVariantsBulkUpdate" in query:
            self.writes.append("variantPrice")
            for candidate in self.rows.values():
                for variant in candidate["variants"]["nodes"]:
                    change = next((row for row in variables["variants"] if row["id"] == variant["id"]), None)
                    if change: variant.update(change)
            return {"productVariantsBulkUpdate": {"userErrors": [], "productVariants": []}}
        if "tagsAdd" in query:
            self.writes.append("tagsAdd")
            row = self.rows[variables["id"]]
            row["tags"] = sorted(set(row.get("tags", [])) | set(variables["tags"]))
            return {"tagsAdd": {"node": {"id": row["id"]}, "userErrors": []}}
        if "productCreateMedia" in query:
            self.writes.append("productCreateMedia")
            product = self.rows[variables["productId"]]
            media = []
            for item in variables["media"]:
                record = {"id": f"gid://shopify/MediaImage/{len(product.get('media', []))+1}", "alt": item["alt"],
                          "status": "READY", "mediaContentType": "IMAGE", "image": {"url": item["originalSource"]}}
                product.setdefault("media", []).append(record); media.append(record)
            return {"productCreateMedia": {"media": media, "mediaUserErrors": []}}
        if "ShopSourceProductMediaRead" in query:
            product = self.rows[variables["id"]]
            return {"product": {"id": variables["id"], "media": {"nodes": product.get("media", [])}}}
        raise AssertionError(query)


@pytest.fixture
def product_setup(tmp_path, monkeypatch):
    db = tmp_path / "fixture.sqlite3"
    init_db(db)
    save_connection("store-a", "fixture.myshopify.com", db=db)
    upsert_store({"store_id": "store-a", "store_name": "Fixture Store", "category": "Car Organization",
                  "concept": "Synthetic pilot products", "sourcing": {"recipes": []}, "include_keywords": [],
                  "exclude_keywords": [], "risk_rules": []}, db)
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "test-token-never-log")
    with connect(db) as con:
        for idx, (asin, title, status, raw, source) in enumerate([
            ("B000000001", "Same Product", "PRIMARY", {"shopify_selling_price": 24.5}, "amazon"),
            ("B000000002", "Same Product", "REVIEW", {"shopify_selling_price": 29.5}, "amazon"),
            ("B000000003", "Risk Product", "RESTRICTED", {"shopify_selling_price": 18}, "amazon"),
            ("B000000004", "Archived Product", "ARCHIVED", {"shopify_selling_price": 18}, "amazon"),
            ("B000000005", "No Retail Price", "PRIMARY", {"price": 4.2}, "amazon"),
        ]):
            cur = con.execute("""INSERT INTO products(asin,source,source_kind,title,brand,category,price,raw_json,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (asin, source, "BROWSER_CAPTURE", title, "Brand", "Organizers", 4.2,
                  json.dumps(raw), "now", "now"))
            con.execute("""INSERT INTO store_product_decisions(store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at)
                VALUES(?,?,?,?,?,?,?)""", ("store-a", cur.lastrowid, "IN_RANGE", "SAFE", status, status, "now"))
    fake = FakeProductShopify()
    publisher = DirectShopifyProductPublisher(db=db, client_factory=fake, wait=lambda _n: None, batch_size=10)
    return db, publisher, fake


def test_direct_product_preview_create_update_nochange_skip(product_setup):
    _db, publisher, _fake = product_setup
    preview = publisher.preview("store-a")
    assert preview["counts"]["CREATE"] == 2
    assert preview["counts"]["SKIP"] == 3  # restricted, archived, no configured store price
    assert preview["inventory"] == "UNMANAGED"


def test_direct_product_idempotent_no_duplicates(product_setup):
    _db, publisher, fake = product_setup
    first = publisher.preview("store-a")
    publisher.sync(first["run_id"], confirmed=True, expected_input_hash=first["input_hash"])
    second = publisher.preview("store-a")
    assert second["counts"]["NO CHANGE"] == 2
    assert len(fake.rows) == 2


def test_direct_product_update_and_remote_drift_conflict(product_setup):
    db, publisher, fake = product_setup
    first = publisher.preview("store-a")
    publisher.sync(first["run_id"], confirmed=True, expected_input_hash=first["input_hash"])
    with connect(db) as con: con.execute("UPDATE products SET title='ShopSource new title' WHERE asin='B000000001'")
    update = publisher.preview("store-a")
    assert update["counts"]["UPDATE"] == 1
    publisher.sync(update["run_id"], confirmed=True, expected_input_hash=update["input_hash"])
    remote = next(row for row in fake.rows.values() if row["handle"] == identity_handle("amazon", "B000000001"))
    remote["title"] = "Merchant edited title"
    conflict = publisher.preview("store-a")
    assert conflict["counts"]["CONFLICT"] == 1


def test_direct_product_restricted_never_synced(product_setup):
    _db, publisher, _fake = product_setup
    preview = publisher.preview("store-a")
    with connect(publisher.db) as con:
        rows = con.execute("SELECT source_id,action FROM shopify_product_sync_items WHERE run_id=?", (preview["run_id"],)).fetchall()
    assert next(row for row in rows if row["source_id"] == "B000000003")["action"] == "SKIP"
    assert next(row for row in rows if row["source_id"] == "B000000004")["action"] == "SKIP"


def test_product_identity_uses_source_id_not_title():
    assert identity_handle("amazon", "B000000001") != identity_handle("amazon", "B000000002")
    assert source_identity("a", "amazon", "B000000001") != source_identity("a", "amazon", "B000000002")


def test_product_payload_does_not_overwrite_unowned_fields():
    payload = DirectShopifyProductPublisher.build_payload({"asin": "B0001", "source": "amazon", "title": "Storage",
        "final_status": "PRIMARY", "selling_price": 20, "brand": "A", "product_type": "Storage"}, store_id="s")
    graphql_input = DirectShopifyProductPublisher._graphql_input(payload)
    assert "tags" not in graphql_input and "variants" not in graphql_input and "metafields" not in graphql_input
    assert "collections" not in graphql_input and "files" not in graphql_input


def test_productSet_list_fields_not_accidentally_cleared():
    payload = {"handle": "x", "title": "X", "status": "DRAFT", "owned_tags": ["shopsource:collection:storage"], "price": "20.00"}
    safe = DirectShopifyProductPublisher._graphql_input(payload)
    assert set(safe) == {"handle", "title", "status"}
    assert "variants" not in safe and "metafields" not in safe and "collections" not in safe


def test_product_tags_preserve_unowned_tags(product_setup):
    _db, publisher, fake = product_setup
    preview = publisher.preview("store-a")
    publisher.sync(preview["run_id"], confirmed=True, expected_input_hash=preview["input_hash"])
    assert all("merchant:featured" in row["tags"] for row in fake.rows.values())


def test_collection_tags_deterministic():
    assert collection_tag("Seat Organizers") == "shopsource:collection:seat-organizers"
    assert collection_tag("Seat Organizers") == collection_tag("seat-organizers")


def test_product_media_failure_isolated(product_setup):
    _db, publisher, _fake = product_setup
    publisher.set_media_mode("store-a", "SOURCE_MEDIA", source_media_rights_confirmed=True)
    publisher.media_handler = lambda *_args: (_ for _ in ()).throw(RuntimeError("mock media failed"))
    # The explicit fixture mode allows an injected media adapter; its isolated failure doesn't
    # mark the containing product mutation failed.
    preview = publisher.preview("store-a")
    assert preview["media_mode"] == "SOURCE_MEDIA"
    result = publisher.sync(preview["run_id"], confirmed=True, expected_input_hash=preview["input_hash"])
    assert result["status"] == "COMPLETE_WITH_WARNINGS"
    assert result["counts"]["SYNCED_WITH_WARNINGS"] == 2
    assert result["counts"].get("FAILED", 0) == 0


def test_source_media_mode_requires_rights_and_verifies_url(product_setup):
    db, publisher, fake = product_setup
    publisher.set_media_mode("store-a", "SOURCE_MEDIA", source_media_rights_confirmed=True)
    with connect(db) as con:
        con.execute("UPDATE products SET images_json=? WHERE asin='B000000001'", (json.dumps([{"url": "https://images.example/item.jpg", "alt_text": "Organizer"}]),))
    preview = publisher.preview("store-a")
    assert preview["source_media_rights_confirmed"] is True
    result = publisher.sync(preview["run_id"], confirmed=True, expected_input_hash=preview["input_hash"])
    assert "productCreateMedia" in fake.writes
    assert result["counts"]["SYNCED"] == 2
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM shopify_product_media_mappings WHERE image_url_verified=1").fetchone()[0] == 1


def test_product_graphql_user_errors_handled(product_setup):
    _db, publisher, fake = product_setup
    first = publisher.preview("store-a")
    original = fake.execute
    def errors(query, variables=None):
        if "productSet(" in query:
            return {"productSet": {"product": None, "userErrors": [{"message": "Rejected by mock"}]}}
        return original(query, variables)
    fake.execute = errors
    result = publisher.sync(first["run_id"], confirmed=True, expected_input_hash=first["input_hash"])
    assert result["status"] == "COMPLETE_WITH_WARNINGS"
    assert result["counts"]["FAILED"] == 2


def test_product_verify_after_write(product_setup):
    _db, publisher, fake = product_setup
    preview = publisher.preview("store-a")
    result = publisher.sync(preview["run_id"], confirmed=True, expected_input_hash=preview["input_hash"])
    assert result["counts"]["SYNCED"] == 2
    assert any("ShopSourceProductById" in query for query, _ in fake.request_bodies)


def test_large_catalog_checkpoint_resume(tmp_path, monkeypatch):
    db = tmp_path / "large.sqlite3"; init_db(db); save_connection("s", "fixture.myshopify.com", db=db)
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "test-token-never-log")
    with connect(db) as con:
        for offset in range(10000):
            cursor = con.execute("INSERT INTO products(asin,title,source,source_kind,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?)",
                                 (f"B{offset:09d}", f"Synthetic {offset}", "amazon", "BROWSER_CAPTURE", "{}", "now", "now"))
            con.execute("INSERT INTO store_product_decisions(store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at) VALUES(?,?,?,?,?,?,?)",
                        ("s", cursor.lastrowid, "OUT", "SAFE", "RESTRICTED", "RESTRICTED", "now"))
    publisher = DirectShopifyProductPublisher(db=db, batch_size=128)
    preview = publisher.preview("s", limit=12)
    assert preview["requested"] == 10000 and preview["counts"]["SKIP"] == 10000
    assert preview["preview_items"] == []
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM shopify_product_sync_items WHERE run_id=?", (preview["run_id"],)).fetchone()[0] == 10000


def test_retry_failed_products_only(product_setup):
    _db, publisher, fake = product_setup
    preview = publisher.preview("store-a")
    with connect(publisher.db) as con:
        con.execute("UPDATE shopify_product_sync_items SET status='FAILED' WHERE run_id=? AND action='CREATE'", (preview["run_id"],))
        con.execute("UPDATE shopify_product_sync_items SET status='SKIP' WHERE run_id=? AND action='SKIP'", (preview["run_id"],))
        con.execute("UPDATE shopify_product_sync_runs SET status='PAUSED' WHERE run_id=?", (preview["run_id"],))
    result = publisher.retry_failed(preview["run_id"], confirmed=True, expected_input_hash=preview["input_hash"])
    assert result["counts"]["SYNCED"] == 2
    assert result["counts"].get("SKIP", 0) == 3


def test_token_never_logged(product_setup, caplog):
    _db, publisher, _fake = product_setup
    assert "test-token-never-log" not in caplog.text
    fake_token = "x" * 40
    assert fake_token not in publisher._safe_error(RuntimeError(f"token {fake_token}"))


def add_pilot_products(db, count=12, *, missing_price=False):
    with connect(db) as con:
        for index in range(count):
            raw = {} if missing_price and index == count - 1 else {"shopify_selling_price": 18 + index}
            cur = con.execute("INSERT INTO products(asin,source,source_kind,title,brand,category,price,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (f"PILOT{index:06}", "amazon", "BROWSER_CAPTURE", f"Pilot item {index}", "Pilot Brand", "Storage", 3.25, json.dumps(raw), "now", "now"))
            con.execute("INSERT INTO store_product_decisions(store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at) VALUES(?,?,?,?,?,?,?)",
                        ("store-a", cur.lastrowid, "IN_RANGE", "SAFE", "PRIMARY", "PRIMARY", "now"))


def test_pilot_default_10(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, _ = product_setup; add_pilot_products(db)
    assert ShopifyLivePilot(db=db, publisher=publisher).preview("store-a")["requested"] == 10


def test_pilot_max_20(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, _ = product_setup
    with pytest.raises(ValueError): ShopifyLivePilot(db=db, publisher=publisher).preview("store-a", limit=21)


def test_pilot_draft_forced(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, _ = product_setup
    assert ShopifyLivePilot(db=db, publisher=publisher).preview("store-a", limit=1)["items"][0]["status"] == "DRAFT"


def test_pilot_deterministic_selection(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, _ = product_setup; add_pilot_products(db)
    service = ShopifyLivePilot(db=db, publisher=publisher)
    assert [r["source_id"] for r in service.preview("store-a")["items"]] == [r["source_id"] for r in service.preview("store-a")["items"]]


def test_pilot_restricted_excluded(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, _ = product_setup
    rows = ShopifyLivePilot(db=db, publisher=publisher).preview("store-a", limit=20)["items"]
    assert all(row["title"] != "Risk Product" for row in rows)


def test_pilot_missing_price_skipped(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, _ = product_setup; add_pilot_products(db, 9, missing_price=True)
    rows = ShopifyLivePilot(db=db, publisher=publisher).preview("store-a", limit=20)["items"]
    assert any(row["selling_price"] is None and row["action"] == "SKIP" for row in rows)


def test_pilot_price_preview(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, _ = product_setup
    row = ShopifyLivePilot(db=db, publisher=publisher).preview("store-a", limit=1)["items"][0]
    assert (row["source_price"], row["selling_price"], row["currency"]) == (4.2, 24.5, "USD")


def test_pilot_requires_shopify_credentials(product_setup, monkeypatch):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, _ = product_setup; monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN")
    assert not ShopifyLivePilot(db=db, publisher=publisher).connection_preflight("store-a")["product_ready"]


def test_pilot_requires_write_products(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, fake = product_setup; fake.scopes.discard("write_products")
    assert not ShopifyLivePilot(db=db, publisher=publisher).connection_preflight("store-a")["product_ready"]


def test_pilot_preview_before_live(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, fake = product_setup
    ShopifyLivePilot(db=db, publisher=publisher).preview("store-a", limit=1)
    assert fake.writes == []


def test_pilot_live_confirmation_required(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, _ = product_setup; service = ShopifyLivePilot(db=db, publisher=publisher)
    preview = service.preview("store-a", limit=1)
    with pytest.raises(RuntimeError): service.execute(preview["run_id"])


def test_pilot_no_image_default(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, fake = product_setup
    preview = ShopifyLivePilot(db=db, publisher=publisher).preview("store-a", limit=1)
    assert preview["items"][0]["media_mode"] == "MANUAL_MEDIA" and fake.writes == []


def test_pilot_verify_after_write_mock(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, fake = product_setup; service = ShopifyLivePilot(db=db, publisher=publisher)
    preview = service.preview("store-a", limit=1)
    result = service.execute(preview["run_id"], live_confirmed=True)
    assert result["counts"].get("SYNCED") == 1 and "productSet" in fake.writes
    assert next(iter(fake.rows.values()))["status"] == "DRAFT"


def test_pilot_multivariant_source_skipped(product_setup):
    db, _publisher, _fake = product_setup
    product = {"asin": "B0001", "source": "amazon", "title": "Variants", "final_status": "PRIMARY",
               "selling_price": 22, "source_variant_count": 2}
    with pytest.raises(ValueError, match="MULTI_VARIANT_SOURCE"):
        DirectShopifyProductPublisher.build_payload(product, store_id="store-a")


def test_pilot_user_errors_fail(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, fake = product_setup; fake.user_errors = True
    service = ShopifyLivePilot(db=db, publisher=publisher); preview = service.preview("store-a", limit=1)
    assert service.execute(preview["run_id"], live_confirmed=True)["counts"].get("FAILED") == 1


def test_pilot_verify_mismatch_is_verify_failed(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, fake = product_setup; fake.force_mismatch = True
    service = ShopifyLivePilot(db=db, publisher=publisher); preview = service.preview("store-a", limit=1)
    assert service.execute(preview["run_id"], live_confirmed=True)["counts"].get("VERIFY_FAILED") == 1


def test_pilot_does_not_auto_delete(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, fake = product_setup; service = ShopifyLivePilot(db=db, publisher=publisher)
    first = service.preview("store-a", limit=1); service.execute(first["run_id"], live_confirmed=True)
    fake.rows.clear()
    assert service.preview("store-a", limit=1)["counts"]["CONFLICT"] == 1
    assert not any("productDelete" in query for query, _ in fake.request_bodies)


def test_pilot_collection_preview_after_verified(product_setup, monkeypatch):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, _ = product_setup; service = ShopifyLivePilot(db=db, publisher=publisher)
    preview = service.preview("store-a", limit=1); service.execute(preview["run_id"], live_confirmed=True)
    class Planner:
        def __init__(self, _db): pass
        def create_plan(self, _store, settings): return {"store_id": "store-a", "collections": []}
    class Collections:
        def __init__(self, db=None): pass
        def dry_run(self, plan, publish_online_store=False): return {"counts": {"CREATE": 0}, "items": []}
    monkeypatch.setattr("shopsource.collection_planner.CollectionPlanner", Planner)
    monkeypatch.setattr("shopsource.shopify_collections.ShopifyCollectionPublisher", Collections)
    assert not service.collection_preview("store-a", preview["pilot_run_id"])["writes_performed"]


def test_no_real_shopify_write_in_tests(product_setup):
    from shopsource.shopify_pilot import ShopifyLivePilot
    db, publisher, fake = product_setup
    ShopifyLivePilot(db=db, publisher=publisher).preview("store-a", limit=1)
    assert fake.writes == []
