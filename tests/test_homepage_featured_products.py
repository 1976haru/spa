import json
import hashlib
from pathlib import Path

from shopsource.db import connect, init_db
from shopsource.homepage_assignment import HomepageAssignmentService, homepage_assignment_workflow
from shopsource.homepage_featured_products import (
    FeaturedProductAssignmentService, ShopifyExistingProductReader, discover_featured_product_schema,
    merchandising_suitability, normalize_merchandising_group, validate_isolated_featured_diff,
)
from shopsource.shopify_theme_ids import is_valid_shopify_instance_id, legacy_featured_id_for_store, legacy_shopify_instance_kind, shopify_instance_id
from shopsource.homepage_automation import _managed_id
from shopsource.homepage_collections import _section_id
from shopsource.store_build import STAGES
from shopsource.store_completion import DOMAINS, StoreCompletionService
from shopsource.theme_write_backend import (ThemeBackendCapabilities, ThemeFileReadResult,
    ThemeFileWriteResult)


def add_product(db, number, category, *, handle=None, remote_id=None, price=49.0,
                image=True, status="ACTIVE", mapping_status="VERIFIED", decision="PRIMARY", synced_at=None):
    now = synced_at or f"2026-01-{number:02d}T00:00:00+00:00"
    raw = {"shopify_selling_price": price, "shopify_status": status}
    images = [f"https://cdn.example/{number}.jpg"] if image else []
    with connect(db) as con:
        con.execute("INSERT INTO products(asin,title,category,images_json,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?)",
                    (f"B{number:09d}", f"Product {number}", category, json.dumps(images), json.dumps(raw), now, now))
        product_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute("""INSERT INTO store_product_decisions(store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at)
                       VALUES('s1',?,'PRIMARY','CLEAR',?,?,?)""", (product_id, decision, decision, now))
        con.execute("""INSERT INTO shopify_product_mappings(store_id,master_product_id,source_platform,source_id,
                       shopify_product_id,shopify_handle,last_remote_hash,sync_status,synced_at)
                       VALUES('s1',?,'amazon',?,?,?,?,?,?)""",
                    (product_id, f"B{number:09d}", remote_id or f"gid://shopify/Product/{number}",
                     handle if handle is not None else f"product-{number}", "verified", mapping_status, now))
    return product_id


def service(tmp_path):
    db = tmp_path / "featured.sqlite3"
    init_db(db)
    return db, FeaturedProductAssignmentService(db=db, export_dir=tmp_path / "exports")


def seed_four(db):
    return [add_product(db, i, category) for i, category in enumerate(("trunk", "seat", "console", "cleanup"), 1)]


def theme_files(mode="direct"):
    field = {"type": "product_list", "id": "products", "label": "Products"} if mode == "direct" else {"type": "collection", "id": "collection", "label": "Collection"}
    schema = {"name": "Featured products", "settings": [field, {"type": "text", "id": "heading", "label": "Heading"}]}
    return {"sections/featured-products.liquid": "{% schema %}" + json.dumps(schema) + "{% endschema %}"}


def test_isolated_featured_diff_allows_only_managed_section_create():
    sid = shopify_instance_id("featuredproducts", "test")
    before = {"sections": {"hero": {"type": "hero"}}, "order": ["hero"], "settings": {"x": 1}}
    after = {"sections": {"hero": {"type": "hero"}, sid: {"type": "featured", "settings": {"products": ["a"]}}},
             "order": ["hero", sid], "settings": {"x": 1}}
    result = validate_isolated_featured_diff(before, after, sid)
    assert result["safe"] and result["unexpected_paths"] == []


def test_isolated_featured_diff_allows_managed_section_update_and_existing_order():
    sid = shopify_instance_id("featuredproducts", "test")
    before = {"sections": {sid: {"settings": {"products": ["old"]}}}, "order": ["hero", sid, "footer"], "other": True}
    after = {"sections": {sid: {"settings": {"products": ["new"]}}}, "order": ["hero", sid, "footer"], "other": True}
    assert validate_isolated_featured_diff(before, after, sid)["safe"]


def test_isolated_featured_diff_blocks_any_unrelated_change():
    sid = shopify_instance_id("featuredproducts", "test")
    before = {"sections": {"hero": {"settings": {"title": "old"}}}, "order": ["hero"]}
    after = {"sections": {"hero": {"settings": {"title": "new"}}, sid: {"type": "featured"}}, "order": ["hero", sid]}
    result = validate_isolated_featured_diff(before, after, sid)
    assert not result["safe"] and result["unexpected_paths"]


def test_isolated_featured_diff_blocks_category_and_repeated_managed_order():
    sid = shopify_instance_id("featuredproducts", "test")
    before = {"sections": {"category": {"type": "collection-list"}}, "order": ["category"]}
    after = {"sections": {"category": {"type": "changed"}, sid: {"type": "featured"}}, "order": ["category", sid, sid]}
    result = validate_isolated_featured_diff(before, after, sid)
    assert not result["safe"] and any(path.startswith("order") for path in result["unexpected_paths"])


def test_shopify_instance_id_contract_and_historical_recognition():
    assert is_valid_shopify_instance_id(shopify_instance_id("featured_products", "001"))
    assert shopify_instance_id("Hero", "primary") == shopify_instance_id("hero", "primary")
    assert shopify_instance_id("hero", "primary") != shopify_instance_id("categories", "primary")
    assert legacy_shopify_instance_kind(legacy_featured_id_for_store("001")) == "featured_products"
    assert legacy_shopify_instance_kind("ss_unrelated_section") is None
    assert all(is_valid_shopify_instance_id(value) for value in (
        shopify_instance_id("featuredproducts", "s1"), _managed_id("hero", "primary"),
        _managed_id("categories", "s1"), _managed_id("cat", "collection_a"), _section_id("collection_a")))


def test_isolated_diff_allows_only_exact_legacy_migration():
    store_id = "001"
    legacy = legacy_featured_id_for_store(store_id)
    new = shopify_instance_id("featuredproducts", store_id)
    before = {"sections": {"hero": {"type": "hero"}, legacy: {"type": "featured"}},
              "order": ["hero", legacy]}
    after = {"sections": {"hero": {"type": "hero"}, new: {"type": "featured"}},
             "order": ["hero", new]}
    result = validate_isolated_featured_diff(before, after, new, legacy_id=legacy, store_id=store_id)
    assert result["safe"] and result["legacy_removed"] == legacy and result["new_section_id"] == new
    changed = {"sections": {new: {"type": "featured"}}, "order": [new]}
    assert not validate_isolated_featured_diff(before, changed, new, legacy_id=legacy, store_id="other")["safe"]


def test_storefront_confirmation_requires_remote_json_and_four_human_checks(tmp_path):
    db, svc = service(tmp_path)
    seed_four(db)
    plan = svc.create_plan("s1")
    incomplete = svc.confirm_storefront(plan["plan_id"], checks={"desktop_title": True}, confirmed=True)
    assert incomplete["status"] == "REVIEW_REQUIRED"
    checks = {"desktop_title": True, "four_cards": True, "product_links": True, "mobile_section": True}
    assert svc.confirm_storefront(plan["plan_id"], checks=checks, confirmed=True)["status"] == "REVIEW_REQUIRED"
    with connect(db) as con: con.execute("UPDATE homepage_featured_product_plans SET remote_json_verified=1 WHERE plan_id=?", (plan["plan_id"],))
    assert svc.confirm_storefront(plan["plan_id"], checks=checks, confirmed=True)["status"] == "STOREFRONT_VERIFIED"


def isolated_apply_fixture(tmp_path, monkeypatch):
    import shopsource.homepage_featured_products as feature_module
    db, planner = service(tmp_path)
    seed_four(db)
    plan = planner.create_plan("s1", requested_count=4)
    legacy = legacy_featured_id_for_store("s1")
    raw = '/* Shopify header comment */\n' + json.dumps({"sections": {
        "hero": {"type": "hero", "settings": {"title": "Keep"}},
        legacy: {"type": "featured-products", "settings": {}},
        "merchant": {"type": "rich-text", "settings": {"text": "Preserve"}}},
        "order": ["hero", legacy, "merchant"]}) + "\n"
    snapshot = {"template_status": "READY", "template_filename": "templates/index.json",
        "template": json.loads(raw.split("*/", 1)[1]), "theme_files": {"templates/index.json": raw,
            "sections/featured-products.liquid": "{% schema %}" + json.dumps({"name": "Featured products", "settings": [
                {"type": "product_list", "id": "products", "label": "Products"}]}) + "{% endschema %}"},
        "theme": {"id": "gid://shopify/OnlineStoreTheme/55", "name": "MAIN theme", "role": "MAIN"}}
    preview = planner.build_theme_preview(plan, snapshot)
    assert preview["migration"] == {"from": legacy, "to": preview["section_id"]}
    preview.update(store_id="s1", featured_products_plan_id=plan["plan_id"],
                   theme=snapshot["theme"], template_filename="templates/index.json")
    monkeypatch.setattr(feature_module, "get_connection", lambda *a, **k: {"shop_domain": "sample.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr(feature_module, "get_shopify_token", lambda *a, **k: ("not-a-real-token", "test"))

    class FakeClient:
        def __init__(self): self.raw, self.write_count, self.calls, self.write_files = raw, 0, [], []
        def execute(self, query, variables=None):
            self.calls.append(query)
            if "FeaturedScopes" in query:
                return {"currentAppInstallation": {"accessScopes": [{"handle": "read_themes"}, {"handle": "write_themes"}]}}
            if "themeFilesUpsert" in query:
                self.write_count += 1
                self.write_files.extend(file["filename"] for file in variables["files"])
                self.raw = variables["files"][0]["body"]["value"]
                return {"themeFilesUpsert": {"userErrors": [], "upsertedThemeFiles": [{"filename": variables["files"][0]["filename"]}]}}
            return {"theme": {"id": snapshot["theme"]["id"], "role": "MAIN", "name": "MAIN theme",
                "files": {"nodes": [{"filename": "templates/index.json", "body": {"__typename": "OnlineStoreThemeFileBodyText", "content": self.raw}}]}}}
    client = FakeClient()
    writer = feature_module.FeaturedProductThemeApplyService(db=db, export_dir=tmp_path / "exports")
    import shopsource.featured_theme_backend as backend_module
    from shopsource.featured_theme_backend import FeaturedThemeBackendCoordinator
    monkeypatch.setattr(backend_module, "get_connection", lambda *a, **k: {"shop_domain": "sample.myshopify.com", "api_version": "2026-07"})
    FeaturedThemeBackendCoordinator(db=db, client=client)
    with connect(db) as con:
        con.execute("""INSERT INTO theme_write_backend_evidence
            (store_id,theme_id,backend_name,status,evidence_source,last_attempt_at,last_success_at,updated_at)
            VALUES('s1',?,'ADMIN_GRAPHQL','VERIFIED_ACTIVE','TEST_HISTORICAL_SUCCESS','test','test','test')""",
                    (snapshot["theme"]["id"],))
    return db, plan, preview, client, writer


def test_isolated_feature_apply_confirmation_and_one_file_write_with_comment_backup(tmp_path, monkeypatch):
    db, plan, preview, client, writer = isolated_apply_fixture(tmp_path, monkeypatch)
    refused = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=False, client=client)
    assert refused["status"] == "MANUAL_ACTION_REQUIRED" and client.write_count == 0
    result = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True, allow_live=True, client=client)
    assert result["status"] == "REMOTE_JSON_VERIFIED" and result["product_ids_match"]
    assert "ss_featured_products_" not in client.raw
    assert client.write_count == 1 and client.raw.startswith("/* Shopify header comment */")
    parsed = json.loads(client.raw.split("*/", 1)[1])
    assert parsed["sections"]["merchant"]["settings"]["text"] == "Preserve"
    assert is_valid_shopify_instance_id(preview["section_id"])
    with connect(db) as con:
        state = con.execute("SELECT status,remote_verified,remote_json_verified,storefront_verified FROM homepage_featured_product_plans WHERE plan_id=?", (plan["plan_id"],)).fetchone()
    assert tuple(state) == ("REMOTE_JSON_VERIFIED", 0, 1, 0)
    assert client.write_files == ["templates/index.json"]
    folder = next((tmp_path / "exports" / "theme_backups" / "s1").iterdir())
    assert {"before.raw.json", "proposed.raw.json", "before.parsed.json", "proposed.parsed.json",
            "selected_products.json", "diff.md", "metadata.json"}.issubset({x.name for x in folder.iterdir()})
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["backend_name"] == "ADMIN_GRAPHQL"
    with connect(db) as con:
        backup = con.execute("SELECT backend_name,backend_selection,backend_reason_code FROM homepage_featured_theme_backups WHERE backup_id=?",
                             (result["backup_id"],)).fetchone()
    assert backup["backend_name"] == "ADMIN_GRAPHQL" and backup["backend_selection"] == "AUTO"


def test_isolated_apply_rejects_non_alphanumeric_generated_id_before_write(tmp_path, monkeypatch):
    db, plan, preview, client, writer = isolated_apply_fixture(tmp_path, monkeypatch)
    preview["section_id"] = "ss_featured_products_invalid"
    result = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True, client=client)
    assert result["status"] == "CONFLICT" and client.write_count == 0


def test_isolated_feature_apply_blocks_proposal_drift_and_rollback_checks_merchant_drift(tmp_path, monkeypatch):
    db, plan, preview, client, writer = isolated_apply_fixture(tmp_path, monkeypatch)
    preview["proposed"]["sections"]["hero"]["settings"]["title"] = "changed"
    blocked = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True, allow_live=True, client=client)
    assert blocked["status"] == "CONFLICT" and client.write_count == 0


def test_isolated_feature_rollback_restores_exact_raw_and_refuses_merchant_drift(tmp_path, monkeypatch):
    db, plan, preview, client, writer = isolated_apply_fixture(tmp_path, monkeypatch)
    original_raw = client.raw
    applied = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True, allow_live=True, client=client)
    assert applied["status"] == "REMOTE_JSON_VERIFIED" and client.write_count == 1
    restored = writer.rollback(applied["backup_id"], confirmed=True, allow_live=True, client=client)
    assert restored["status"] == "ROLLED_BACK" and client.raw == original_raw and client.write_count == 2

    merchant_dir = tmp_path / "merchant"
    merchant_dir.mkdir()
    db2, plan2, preview2, client2, writer2 = isolated_apply_fixture(merchant_dir, monkeypatch)
    applied2 = writer2.apply(plan2["plan_id"], preview2, store_id="s1", confirmed=True, allow_live=True, client=client2)
    assert applied2["status"] == "REMOTE_JSON_VERIFIED"
    client2.raw += "\n/* merchant drift */"
    refused = writer2.rollback(applied2["backup_id"], confirmed=True, allow_live=True, client=client2)
    assert refused["status"] == "CONFLICT" and client2.write_count == 1


def test_isolated_feature_apply_rejects_stale_plan_and_raw_semantic_drift(tmp_path, monkeypatch):
    db, plan, preview, client, writer = isolated_apply_fixture(tmp_path, monkeypatch)
    service_for_db = FeaturedProductAssignmentService(db=db)
    service_for_db.create_plan("s1", requested_count=4)
    stale = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True, allow_live=True, client=client)
    assert stale["status"] == "CONFLICT" and client.write_count == 0

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    db2, plan2, preview2, client2, writer2 = isolated_apply_fixture(raw_dir, monkeypatch)
    client2.raw += " "
    raw_drift = writer2.apply(plan2["plan_id"], preview2, store_id="s1", confirmed=True, allow_live=True, client=client2)
    assert raw_drift["status"] == "CONFLICT" and client2.write_count == 0

    semantic_dir = tmp_path / "semantic"
    semantic_dir.mkdir()
    db3, plan3, preview3, client3, writer3 = isolated_apply_fixture(semantic_dir, monkeypatch)
    source = preview3["source_document"]
    source["before_raw_hash"] = hashlib.sha256(client3.raw.encode("utf-8")).hexdigest()
    source["before_semantic_hash"] = "deliberately-stale-semantic-hash"
    semantic_drift = writer3.apply(plan3["plan_id"], preview3, store_id="s1", confirmed=True, allow_live=True, client=client3)
    assert semantic_drift["status"] == "CONFLICT" and client3.write_count == 0


def test_featured_products_requires_real_mapping_and_handle(tmp_path):
    db, svc = service(tmp_path)
    add_product(db, 1, "trunk", handle="", mapping_status="VERIFIED")
    rows = svc.eligible_products("s1")
    assert not rows[0]["eligible"] and "MISSING_REAL_HANDLE" in rows[0]["eligibility_reasons"]


def test_featured_products_requires_valid_retail_price_image_and_active_status(tmp_path):
    db, svc = service(tmp_path)
    add_product(db, 1, "trunk", price=None, image=False, status="DRAFT")
    reasons = svc.eligible_products("s1")[0]["eligibility_reasons"]
    assert {"MISSING_VALID_RETAIL_PRICE", "NEEDS_IMAGE", "NOT_STOREFRONT_ELIGIBLE"}.issubset(reasons)


def test_featured_products_balances_categories(tmp_path):
    db, svc = service(tmp_path); seed_four(db)
    plan = svc.create_plan("s1")
    assert plan["status"] == "READY"
    assert [x["category_key"] for x in plan["items"]] == ["cleanup", "console", "seat", "trunk"]


def test_featured_products_manual_selection_preserves_order(tmp_path):
    db, svc = service(tmp_path); ids = seed_four(db)
    plan = svc.create_plan("s1", mode="MANUAL_SELECTION", manual_product_ids=[ids[2], ids[0], ids[3], ids[1]])
    assert [x["master_product_id"] for x in plan["items"]] == [ids[2], ids[0], ids[3], ids[1]]


def test_featured_products_new_arrivals_uses_real_synced_at(tmp_path):
    db, svc = service(tmp_path); seed_four(db)
    plan = svc.create_plan("s1", mode="NEW_ARRIVALS")
    assert [x["master_product_id"] for x in plan["items"]] == [4, 3, 2, 1]
    assert all(x["selection_reason"] == "recently synced" for x in plan["items"])


def test_assignment_check_requires_four_unique_real_products(tmp_path):
    db, svc = service(tmp_path); seed_four(db)
    plan = svc.create_plan("s1")
    check = svc.checklist(plan, section_visible=True, remote_verified=True)
    assert check["status"] == "REVIEW_REQUIRED"
    assert svc.checklist(plan, section_visible=True, remote_verified=True, storefront_verified=True)["status"] == "ASSIGNMENT_READY"
    plan["items"] = plan["items"][:3]
    assert not svc.checklist(plan, section_visible=True, remote_verified=True)["checks"]["featured_products_count"]


def test_theme_schema_high_confidence_and_unknown_manual_fallback(tmp_path):
    assert discover_featured_product_schema(theme_files())["confidence"] == "HIGH"
    assert discover_featured_product_schema({})["status"] == "MANUAL_ACTION_REQUIRED"
    db, svc = service(tmp_path); seed_four(db); plan = svc.create_plan("s1")
    preview = svc.build_theme_preview(plan, {"theme_files": {}, "template": {"sections": {}, "order": []}})
    assert preview["status"] == "MANUAL_ACTION_REQUIRED" and not preview["write_performed"]


def test_direct_product_preview_preserves_unrelated_sections_and_is_idempotent(tmp_path):
    db, svc = service(tmp_path); seed_four(db); plan = svc.create_plan("s1")
    snapshot = {"theme": {"id": "gid://shopify/OnlineStoreTheme/1"}, "theme_files": theme_files(),
                "template": {"sections": {"merchant": {"type": "rich-text", "settings": {"text": "keep"}}}, "order": ["merchant", "footer"]}}
    first = svc.build_theme_preview(plan, snapshot)
    assert first["status"] == "PREVIEW" and first["proposed"]["sections"]["merchant"] == snapshot["template"]["sections"]["merchant"]
    second = svc.build_theme_preview(plan, {**snapshot, "template": first["proposed"]})
    assert second["action"] == "NO_CHANGE" and second["proposed"]["order"].count(first["section_id"]) == 1


def test_collection_fallback_uses_owned_tag_and_preserves_merchant_tags(tmp_path):
    db, svc = service(tmp_path); seed_four(db); plan = svc.create_plan("s1")
    preview = svc.build_theme_preview(plan, {"theme_files": theme_files("collection"), "template": {"sections": {}, "order": []}})
    assert preview["status"] == "MANUAL_ACTION_REQUIRED"
    assert preview["fallback"]["owned_tag"] == "shopsource:homepage:featured-products"
    assert preview["fallback"]["preserve_merchant_tags"] is True


def test_stale_preview_conflict_and_remote_verify(tmp_path):
    db, svc = service(tmp_path); seed_four(db); plan = svc.create_plan("s1")
    snapshot = {"theme": {"id": "theme"}, "theme_files": theme_files(), "template": {"sections": {}, "order": []}}
    preview = svc.build_theme_preview(plan, snapshot)
    assert svc.verify_remote(plan["plan_id"], preview_hash="stale", remote_section={})["status"] == "CONFLICT"
    ids = [x["shopify_product_id"] for x in plan["items"]]
    result = svc.verify_remote(plan["plan_id"], preview_hash=preview["preview_hash"], remote_section={"visible": True, "product_ids": ids})
    assert result["status"] == "REMOTE_JSON_VERIFIED" and not result["write_performed"]


def test_report_and_assignment_workflow_include_featured_products(tmp_path):
    db, svc = service(tmp_path); seed_four(db); plan = svc.create_plan("s1")
    folder = svc.export_report(plan["plan_id"])
    assert (folder / "featured_products.json").exists() and (folder / "assignment_featured_products.md").exists()
    preview = {"capability": {"status": "NATIVE_THEME_AUTO"}}
    keys = [x["task_key"] for x in homepage_assignment_workflow(preview)]
    assert keys.index("FEATURED_PRODUCTS_PLAN") < keys.index("FEATURED_PRODUCTS_VERIFY") < keys.index("LINK_CHECK")


def test_store_build_stage_order_and_store_completion_tracking(tmp_path):
    assert STAGES.index("COLLECTION_VERIFY") < STAGES.index("FEATURED_PRODUCTS_PLAN")
    assert STAGES.index("FEATURED_PRODUCTS_VERIFY") < STAGES.index("HOMEPAGE_VERIFY")
    assert "FEATURED_PRODUCTS" in DOMAINS
    db = tmp_path / "completion.sqlite3"
    plan = StoreCompletionService(db=db).inspect("s1", {"featured_products": {"remote_verified": True}})
    assert plan["items"]["FEATURED_PRODUCTS"]["status"] == "VERIFIED"


def test_ui_ready_labels_and_no_network_or_write_path():
    source = (Path(__file__).parents[1] / "src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    for label in ("추천 상품 자동 구성", "상품 4개 보기", "다시 선택", "추천 상품 미리보기", "Shopify 적용", "과제 제출용 확인"):
        assert label in source
    module = (Path(__file__).parents[1] / "src/shopsource/homepage_featured_products.py").read_text(encoding="utf-8")
    assert "class ShopifyExistingProductReader" in module
    assert "class FeaturedProductThemeApplyService" in module and "confirmed is not True" in module


def test_protected_store_file_untouched():
    assert (Path(__file__).parents[1] / "stores/001_cabin_tidy.json").exists()


def remote_product(number, *, status="ACTIVE", handle=None, price="59.00", image=True, product_type=""):
    return {"id": f"gid://shopify/Product/{number}", "handle": handle if handle is not None else f"remote-{number}",
            "title": f"Remote product {number}", "status": status, "createdAt": f"2026-02-{number:02d}T00:00:00Z",
            "updatedAt": f"2026-03-{number:02d}T00:00:00Z", "productType": product_type, "tags": [f"tag-{number}"],
            "onlineStoreUrl": None, "featuredMedia": {"image": {"url": f"https://cdn/{number}.jpg", "altText": "item"}} if image else None,
            "variants": {"nodes": [{"price": price}] if price is not None else []}, "variantsCount": {"count": 1},
            "publishedOnCurrentPublication": True}


class FakeCatalogClient:
    calls = 0
    products = []
    scopes = ["read_products"]
    def __init__(self, *args): pass
    def execute(self, query, variables=None):
        type(self).calls += 1
        assert "mutation" not in query.casefold()
        return {"currentAppInstallation": {"accessScopes": [{"handle": x} for x in self.scopes]},
                "products": {"nodes": list(self.products), "pageInfo": {"hasNextPage": False, "endCursor": None}}}


def reader(tmp_path, monkeypatch, products=None, scopes=None):
    db = tmp_path / "reader.sqlite3"; init_db(db)
    FakeCatalogClient.calls = 0; FakeCatalogClient.products = list(products or []); FakeCatalogClient.scopes = list(scopes or ["read_products"])
    monkeypatch.setattr("shopsource.homepage_featured_products.get_connection", lambda *a, **k: {"shop_domain": "fixture.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr("shopsource.homepage_featured_products.get_shopify_token", lambda *a, **k: ("fixture-token", "test"))
    return db, ShopifyExistingProductReader(db=db, client_factory=FakeCatalogClient)


def test_existing_shopify_reader_requires_read_products(tmp_path, monkeypatch):
    _, value = reader(tmp_path, monkeypatch, [remote_product(1)], scopes=["read_themes"])
    result = value.read("s1")
    assert result["status"] == "MISSING_READ_PRODUCTS_SCOPE" and "read_products" in result["reason"]


def test_existing_shopify_reader_is_read_only(tmp_path, monkeypatch):
    _, value = reader(tmp_path, monkeypatch, [remote_product(1)])
    result = value.read("s1")
    assert result["write_performed"] is False and FakeCatalogClient.calls == 1


def test_existing_shopify_eligibility_reasons(tmp_path, monkeypatch):
    products = [remote_product(1), remote_product(2, status="DRAFT"), remote_product(3, handle=""),
                remote_product(4, price=None), remote_product(5, image=False)]
    _, value = reader(tmp_path, monkeypatch, products)
    rows = value.read("s1")["products"]
    assert rows[0]["eligible"] and rows[0]["source_kind"] == "EXISTING_SHOPIFY"
    assert "NOT_STOREFRONT_ELIGIBLE" in rows[1]["eligibility_reasons"]
    assert "MISSING_REAL_HANDLE" in rows[2]["eligibility_reasons"]
    assert "MISSING_VALID_RETAIL_PRICE" in rows[3]["eligibility_reasons"]
    assert "NEEDS_IMAGE" in rows[4]["eligibility_reasons"]


def test_existing_shopify_dedupes_against_managed_mapping(tmp_path, monkeypatch):
    db, remote_reader = reader(tmp_path, monkeypatch, [remote_product(1, handle="product-1")])
    svc = FeaturedProductAssignmentService(db=db); add_product(db, 1, "trunk", remote_id="gid://shopify/Product/1")
    union = svc.candidate_union("s1", reader=remote_reader)
    assert len(union["candidates"]) == 1 and union["candidates"][0]["source_kind"] == "SHOPSOURCE_MANAGED"
    assert union["duplicate_count"] == 1


def test_existing_shopify_preexisting_store_can_reach_four_without_master(tmp_path, monkeypatch):
    db, remote_reader = reader(tmp_path, monkeypatch, [remote_product(i, product_type=f"Type {i}") for i in range(1, 5)])
    svc = FeaturedProductAssignmentService(db=db)
    plan = svc.create_plan("s1", include_existing=True, reader=remote_reader)
    assert plan["status"] == "READY" and len(plan["items"]) == 4
    assert all(x["source_kind"] == "EXISTING_SHOPIFY" and x["master_product_id"] is None for x in plan["items"])
    with connect(db) as con: assert con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


def test_balanced_remote_all_same_category_still_selects_four(tmp_path, monkeypatch):
    db, remote_reader = reader(tmp_path, monkeypatch, [remote_product(i, product_type="Organizer") for i in range(1, 5)])
    plan = FeaturedProductAssignmentService(db=db).create_plan("s1", include_existing=True, reader=remote_reader)
    assert len(plan["items"]) == 4 and {x["category_key"] for x in plan["items"]} == {"Organizer"}


def test_new_arrivals_uses_shopify_created_at_for_remote(tmp_path, monkeypatch):
    db, remote_reader = reader(tmp_path, monkeypatch, [remote_product(i) for i in range(1, 5)])
    plan = FeaturedProductAssignmentService(db=db).create_plan("s1", mode="NEW_ARRIVALS", include_existing=True, reader=remote_reader)
    assert [x["shopify_product_id"].rsplit("/", 1)[-1] for x in plan["items"]] == ["4", "3", "2", "1"]
    assert all(x["selection_reason"] == "newly created" for x in plan["items"])


def test_featured_item_migration_preserves_old_rows(tmp_path):
    db = tmp_path / "migration.sqlite3"; init_db(db)
    with connect(db) as con:
        con.execute("""CREATE TABLE homepage_featured_product_items(plan_id TEXT,position INTEGER,master_product_id INTEGER NOT NULL,
          shopify_product_id TEXT,shopify_handle TEXT,title TEXT,category_key TEXT,image_url TEXT,price REAL,remote_status TEXT,
          selection_reason TEXT,verification_status TEXT,PRIMARY KEY(plan_id,position))""")
        con.execute("INSERT INTO homepage_featured_product_items VALUES('old',1,7,'gid://shopify/Product/7','old','Old','cat','img',9,'ACTIVE','old','OK')")
    FeaturedProductAssignmentService(db=db)
    with connect(db) as con: row = con.execute("SELECT * FROM homepage_featured_product_items").fetchone()
    assert row["master_product_id"] == 7 and row["source_kind"] == "SHOPSOURCE_MANAGED"


def test_featured_remote_cache_and_force_refresh(tmp_path, monkeypatch):
    _, value = reader(tmp_path, monkeypatch, [remote_product(1)])
    assert value.read("s1")["cache_used"] is False
    assert value.read("s1")["cache_used"] is True and FakeCatalogClient.calls == 1
    assert value.read("s1", force=True)["cache_used"] is False and FakeCatalogClient.calls == 2


def test_assignment_check_accepts_existing_shopify_source(tmp_path, monkeypatch):
    db, remote_reader = reader(tmp_path, monkeypatch, [remote_product(i) for i in range(1, 5)])
    svc = FeaturedProductAssignmentService(db=db); plan = svc.create_plan("s1", include_existing=True, reader=remote_reader)
    assert svc.checklist(plan, section_visible=True, remote_verified=True)["status"] == "REVIEW_REQUIRED"


def test_featured_ui_auto_refresh_scope_message_and_diagnostics():
    source = (Path(__file__).parents[1] / "src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert "include_existing=True" in source and "Shopify 상품 다시 읽기" in source
    assert "read_products 권한이 필요합니다" in source and "최종 사용 가능" in source


def test_featured_review_ui_is_dialog_with_cards_images_and_safe_links():
    source = (Path(__file__).parents[1] / "src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert "ui.dialog() as featured_review_dialog" in source
    assert "featured_review_cards" in source and "ui.image(item[\"image_url\"])" in source
    assert "ui.link(\"스토어에서 보기\"" in source and "on_click=show_featured_review" in source
    assert "myshopify\\.com" in source
    render = source.split("def render_featured", 1)[1].split("def select_featured_products", 1)[0]
    assert "selection_reason" not in render and "verification_status" not in render


def test_merchandising_groups_normalize_taxonomy_and_keywords():
    assert normalize_merchandising_group({"category_key": "Automotive Interior Accessories Consoles Organizers Seat Back Organizers"}) == "Seat & Backseat"
    assert normalize_merchandising_group({"title": "Foldable Trunk Cargo Organizer"}) == "Trunk & Cargo"
    assert normalize_merchandising_group({"title": "Backseat Organizer"}) == "Seat & Backseat"
    assert normalize_merchandising_group({"title": "Center Console Storage Organizer"}) == "Console & Small Storage"
    assert normalize_merchandising_group({"title": "Car Trash Can"}) == "Trash & Cleanup"


def test_vehicle_replacement_ranks_below_organizer():
    organizer = {"title": "Universal Trunk Organizer Storage", "product_type": "Cargo Organizer"}
    fitment_part = {"title": "Center Console Lid Replacement Fits 1999-2007 Silverado Sierra OEM", "product_type": "Interior Part"}
    assert merchandising_suitability(organizer) > merchandising_suitability(fitment_part)


def test_reselection_changes_set_deterministically_and_invalidates_preview(tmp_path, monkeypatch):
    products = []
    for number in range(1, 9):
        row = remote_product(number, product_type="Organizer")
        row["title"] = f"Universal organizer storage {number}"
        products.append(row)
    db, remote_reader = reader(tmp_path, monkeypatch, products)
    svc = FeaturedProductAssignmentService(db=db)
    original = svc.create_plan("s1", include_existing=True, reader=remote_reader)
    snapshot = {"theme_files": theme_files(), "template": {"sections": {}, "order": []}}
    preview = svc.build_theme_preview(original, snapshot)
    assert preview["status"] == "PREVIEW"
    first = svc.reselect(original, reader=remote_reader)
    second = svc.reselect(original, reader=remote_reader)
    ids = lambda plan: [x["shopify_product_id"] for x in plan["items"]]
    assert len(first["items"]) == 4 and ids(first) != ids(original)
    assert ids(first) == ids(second)
    assert not set(ids(first)) & set(ids(original))
    with connect(db) as con:
        assert con.execute("SELECT preview_hash FROM homepage_featured_product_plans WHERE plan_id=?", (original["plan_id"],)).fetchone()[0] is None
    assert all(x["remote_status"] == "ACTIVE" and x["price"] > 0 and x["image_url"] for x in first["items"])


def test_reselection_reuses_only_when_alternatives_insufficient(tmp_path):
    db, svc = service(tmp_path); seed_four(db)
    first = svc.create_plan("s1")
    second = svc.reselect(first)
    assert len(second["items"]) == 4 and second["reselection_reused_previous"] is True
    assert {x["shopify_product_id"] for x in second["items"]} == {x["shopify_product_id"] for x in first["items"]}


class StubThemeTransport:
    def __init__(self, name, raw, *, ready=True, write_mode="success"):
        self.backend_name, self.raw = name, raw
        self.ready, self.write_mode = ready, write_mode
        self.write_attempts = 0

    def capability_status(self, _store_id, *, theme_id=None):
        return ThemeBackendCapabilities(self.backend_name, "READY" if self.ready else "UNAVAILABLE",
            "READY" if self.ready else "NOT_READY", {"identity_verified": self.ready,
            "verified_theme_id": theme_id if self.ready else None, "verified_theme_role": "MAIN" if self.ready else None,
            "target_is_live": True if self.ready else None})

    def read_file(self, store_id, shop_domain, theme_id, filename):
        return ThemeFileReadResult(filename, self.raw, hashlib.sha256(self.raw.encode()).hexdigest(),
            self.backend_name, store_id, shop_domain, theme_id, {"verified_theme_role": "MAIN"})

    def write_file(self, request):
        self.write_attempts += 1
        if self.write_mode == "access_denied":
            return ThemeFileWriteResult("ACCESS_DENIED", self.backend_name, request.filename, True,
                                        reason_code="ACCESS_DENIED")
        self.raw = request.content
        if self.write_mode == "semantic_drift":
            parsed = json.loads(self.raw.split("*/", 1)[-1])
            parsed["unrelated_after_write"] = True
            self.raw = "/* Shopify header comment */\n" + json.dumps(parsed) + "\n"
        return ThemeFileWriteResult("WRITE_ATTEMPTED", self.backend_name, request.filename, True)


class StubThemeCoordinator:
    def __init__(self, graph, cli, *, auto="ADMIN_GRAPHQL"):
        self.graph, self.cli, self.auto = graph, cli, auto
        self.selections = []

    def select_backend(self, store_id, theme_id, selection="AUTO", *, client=None):
        self.selections.append(selection)
        chosen_name = self.auto if selection == "AUTO" else selection
        backends = {"ADMIN_GRAPHQL": self.graph, "THEME_ACCESS_CLI": self.cli}
        graph_cap = self.graph.capability_status(store_id, theme_id=theme_id)
        cli_cap = self.cli.capability_status(store_id, theme_id=theme_id)
        backend = backends.get(chosen_name)
        capability = graph_cap if chosen_name == "ADMIN_GRAPHQL" else cli_cap
        if selection == "ADMIN_GRAPHQL_LEGACY":
            backend = None
        if capability is None or not capability.ready:
            backend = None
        return {"backend": backend, "backend_name": chosen_name if backend else None,
                "reason_code": "STUB_SELECTION", "selection": selection, "selection_locked": True,
                "capabilities": {"ADMIN_GRAPHQL": graph_cap, "THEME_ACCESS_CLI": cli_cap}}


def test_featured_apply_requires_explicit_main_live_confirmation(tmp_path, monkeypatch):
    db, plan, preview, client, writer = isolated_apply_fixture(tmp_path, monkeypatch)
    graph = StubThemeTransport("ADMIN_GRAPHQL", client.raw)
    cli = StubThemeTransport("THEME_ACCESS_CLI", client.raw)
    coordinator = StubThemeCoordinator(graph, cli)
    result = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True,
                          backend_coordinator=coordinator)
    assert result["status"] == "MANUAL_ACTION_REQUIRED"
    assert result["backend_reason_code"] == "MAIN_LIVE_CONFIRMATION_REQUIRED"
    assert graph.write_attempts == cli.write_attempts == 0


def test_featured_cli_pre_read_must_match_preview_raw_hash(tmp_path, monkeypatch):
    db, plan, preview, client, writer = isolated_apply_fixture(tmp_path, monkeypatch)
    cli = StubThemeTransport("THEME_ACCESS_CLI", client.raw + " ")
    coordinator = StubThemeCoordinator(StubThemeTransport("ADMIN_GRAPHQL", client.raw), cli, auto="THEME_ACCESS_CLI")
    result = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True, allow_live=True,
                          backend_selection="AUTO", backend_coordinator=coordinator)
    assert result["status"] == "CONFLICT" and result["backend_name"] == "THEME_ACCESS_CLI"
    assert cli.write_attempts == 0


def test_featured_cli_apply_keeps_isolated_diff_guard(tmp_path, monkeypatch):
    db, plan, preview, client, writer = isolated_apply_fixture(tmp_path, monkeypatch)
    preview["proposed"]["sections"]["hero"]["settings"]["title"] = "unrelated mutation"
    cli = StubThemeTransport("THEME_ACCESS_CLI", client.raw)
    coordinator = StubThemeCoordinator(StubThemeTransport("ADMIN_GRAPHQL", client.raw), cli, auto="THEME_ACCESS_CLI")
    result = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True, allow_live=True,
                          backend_coordinator=coordinator)
    assert result["status"] == "CONFLICT" and cli.write_attempts == 0


def test_featured_never_falls_back_after_write_attempt(tmp_path, monkeypatch):
    db, plan, preview, client, writer = isolated_apply_fixture(tmp_path, monkeypatch)
    graph = StubThemeTransport("ADMIN_GRAPHQL", client.raw, write_mode="access_denied")
    cli = StubThemeTransport("THEME_ACCESS_CLI", client.raw)
    coordinator = StubThemeCoordinator(graph, cli)
    result = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True, allow_live=True,
                          backend_coordinator=coordinator)
    assert result["status"] == "FAILED" and result["write_performed"] is True
    assert graph.write_attempts == 1 and cli.write_attempts == 0


def test_featured_cli_apply_semantic_post_verify(tmp_path, monkeypatch):
    db, plan, preview, client, writer = isolated_apply_fixture(tmp_path, monkeypatch)
    cli = StubThemeTransport("THEME_ACCESS_CLI", client.raw, write_mode="semantic_drift")
    coordinator = StubThemeCoordinator(StubThemeTransport("ADMIN_GRAPHQL", client.raw), cli, auto="THEME_ACCESS_CLI")
    result = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True, allow_live=True,
                          backend_coordinator=coordinator)
    assert result["status"] == "VERIFY_FAILED" and result["write_performed"] is True


def test_featured_rollback_uses_original_backend_and_never_switches(tmp_path, monkeypatch):
    db, plan, preview, client, writer = isolated_apply_fixture(tmp_path, monkeypatch)
    original = client.raw
    graph = StubThemeTransport("ADMIN_GRAPHQL", client.raw)
    cli = StubThemeTransport("THEME_ACCESS_CLI", client.raw)
    coordinator = StubThemeCoordinator(graph, cli, auto="THEME_ACCESS_CLI")
    applied = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True, allow_live=True,
                           backend_selection="THEME_ACCESS_CLI", backend_coordinator=coordinator)
    assert applied["status"] == "REMOTE_JSON_VERIFIED" and applied["backend_name"] == "THEME_ACCESS_CLI"
    rolled = writer.rollback(applied["backup_id"], confirmed=True, allow_live=True, backend_coordinator=coordinator)
    assert rolled["status"] == "ROLLED_BACK" and cli.raw == original
    assert coordinator.selections == ["THEME_ACCESS_CLI", "THEME_ACCESS_CLI"]
    assert cli.write_attempts == 2 and graph.write_attempts == 0


def test_legacy_backup_does_not_guess_cli(tmp_path, monkeypatch):
    db, plan, preview, client, writer = isolated_apply_fixture(tmp_path, monkeypatch)
    graph = StubThemeTransport("ADMIN_GRAPHQL", client.raw, ready=False)
    cli = StubThemeTransport("THEME_ACCESS_CLI", client.raw, ready=True)
    coordinator = StubThemeCoordinator(graph, cli, auto="THEME_ACCESS_CLI")
    applied = writer.apply(plan["plan_id"], preview, store_id="s1", confirmed=True, allow_live=True,
                           backend_selection="THEME_ACCESS_CLI", backend_coordinator=coordinator)
    assert applied["status"] == "REMOTE_JSON_VERIFIED"
    with connect(db) as con:
        con.execute("UPDATE homepage_featured_theme_backups SET backend_name=NULL WHERE backup_id=?",
                    (applied["backup_id"],))
    writes_before = cli.write_attempts
    result = writer.rollback(applied["backup_id"], confirmed=True, allow_live=True,
                             backend_coordinator=coordinator)
    assert result["status"] == "MANUAL_ACTION_REQUIRED"
    assert coordinator.selections[-1] == "ADMIN_GRAPHQL"
    assert cli.write_attempts == writes_before


def test_featured_backup_backend_columns_migrate_without_hiding_legacy_rows(tmp_path):
    db = tmp_path / "legacy-backups.sqlite3"
    init_db(db)
    with connect(db) as con:
        con.execute("""CREATE TABLE homepage_featured_theme_backups(
            backup_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,theme_id TEXT NOT NULL,filename TEXT NOT NULL,
            plan_id TEXT NOT NULL,preview_hash TEXT NOT NULL,section_id TEXT NOT NULL,folder TEXT NOT NULL,
            before_raw_hash TEXT NOT NULL,proposed_raw_hash TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL)""")
        con.execute("INSERT INTO homepage_featured_theme_backups VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("HFPB_legacy", "s1", "55", "templates/index.json", "plan", "preview", "section",
                     "folder", "before", "proposed", "BACKED_UP", "then"))
    from shopsource.homepage_featured_products import FeaturedProductThemeApplyService
    FeaturedProductThemeApplyService(db=db, export_dir=tmp_path / "exports")
    with connect(db) as con:
        row = con.execute("SELECT backup_id,status,backend_name,backend_selection,backend_reason_code FROM homepage_featured_theme_backups WHERE backup_id='HFPB_legacy'").fetchone()
    assert tuple(row) == ("HFPB_legacy", "BACKED_UP", None, None, None)
