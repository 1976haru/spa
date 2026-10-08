import json
from pathlib import Path

from shopsource.db import connect, init_db
from shopsource.homepage_assignment import HomepageAssignmentService, homepage_assignment_workflow
from shopsource.homepage_featured_products import (
    FeaturedProductAssignmentService, discover_featured_product_schema,
)
from shopsource.store_build import STAGES
from shopsource.store_completion import DOMAINS, StoreCompletionService


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
    assert check["status"] == "ASSIGNMENT_READY" and all(check["checks"].values())
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
    assert result["status"] == "VERIFIED" and not result["write_performed"]


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
    assert "ShopifyGraphQLClient" not in module and "mutation " not in module


def test_protected_store_file_untouched():
    assert (Path(__file__).parents[1] / "stores/001_cabin_tidy.json").exists()
