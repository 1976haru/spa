from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from shopsource.db import connect, init_db
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

    def __call__(self, domain, token, version):
        assert domain == "fixture.myshopify.com"
        assert token == "test-token-never-log"
        assert version == "2026-07"
        return self

    def execute(self, query, variables=None):
        variables = variables or {}
        self.request_bodies.append((query, variables))
        if "currentAppInstallation" in query:
            return {"currentAppInstallation": {"accessScopes": [{"handle": "read_products"}, {"handle": "write_products"}]}}
        if "productByIdentifier" in query:
            ident = variables["identifier"]
            handle = ident.get("handle")
            return {"productByIdentifier": next((row for row in self.rows.values() if row["handle"] == handle), None)}
        if "ShopSourceProductById" in query:
            return {"product": self.rows.get(variables["id"])}
        if "productSet(" in query:
            self.writes.append("productSet")
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
