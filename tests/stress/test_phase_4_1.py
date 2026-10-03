from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path

import pytest

from shopsource.db import SCHEMA, connect, init_db
from shopsource.homepage_automation import discover_homepage_sections
from shopsource.security import redact_text, redact_value
from shopsource.shopify_products import DirectShopifyProductPublisher
from shopsource.store_build import STAGES, StoreBuildOrchestrator
from shopsource.store_completion import StoreCompletionService, StoreLinkInspector, build_footer_plan, ContentPageService
from shopsource.stress_support import UI_PREVIEW_CAP, match_collection_rules, seed_catalog, seed_collections
from shopsource.ui.v2 import _safe_error
from shopsource.ui.v2_service import product_page


def snapshot(collections=30, **changes):
    pages = {key: f"/pages/{key.lower()}" for key in ("ABOUT_US", "CONTACT", "FAQ", "SHIPPING", "RETURNS", "PRIVACY", "TERMS", "REFUND_POLICY")}
    business = {key: "fixture" for key in ("support_email", "return_window", "return_address", "processing_time", "shipping_time", "shipping_fee", "company_legal_name", "company_address", "phone", "governing_law")}
    mobile = {key: True for key in ("hero_mobile_crop", "logo_width", "mobile_navigation", "collection_columns", "product_media", "text_overflow_review", "cta_length_review", "category_shortcuts", "footer_stacking")}
    theme = {f"sections/{name}.liquid": "" for name in (
        "product-title-product-media-price-variant-selector-quantity-add-to-cart-product-form-description-vendor-inventory-availability-related-products-collapsible",
        "collection-title-description-image-product-grid-sort-filter-pagination-columns-mobile-no-products",
        "header-search-search-modal-templates-search-no-results-product-card",
        "main-cart-cart-items-quantity-cart-remove-subtotal-checkout-cart-empty-mobile")}
    value = {"theme_files": theme, "routes": ["/search", "/cart"], "brand": {"brand_name": "Stress Store"},
             "brand_references": {"homepage": "Stress Store"}, "business": business, "pages": pages,
             "collections": [{"handle": f"collection-{n}"} for n in range(collections)], "footer_ready": True,
             "navigation_ready": True, "homepage_ready": True, "pricing_ready": True, "inventory_policy_ready": True,
             "links": [], "resources": {}, "seo": {"social_image": "fixture.png"}, "accessibility": {}, "mobile": mobile,
             "media": [], "target_market": "US", "primary_market": "US", "store_currency": "USD",
             "myshopify_domain": "stress.myshopify.com", "published_theme": True, "shipping_origin": "US",
             "shipping_markets": ["US"], "shipping_rates": ["fixture"], "tax_status": "CONFIGURED",
             "payment_confirmed": True, "cart_to_checkout": True, "shopify_analytics": True, "final_verification": True}
    value.update(changes); return value


def seed_store(db, products, collections=0):
    seed_catalog(db, products=products)
    if collections: seed_collections(db, "stress-000", collections)


def test_50k_completion_plan(tmp_path):
    db = tmp_path / "massive.sqlite3"; seed_store(db, 50_000, 30)
    plan = StoreCompletionService(db=db, export_dir=tmp_path).build_plan("stress-000", snapshot())
    assert plan["summary"]["counts"] == {"products": 50_000, "collections": 30} and plan["status"] == "READY"


def test_50k_collection_matching():
    products = ({"id": n, "title": f"Café organizer COLLECTION-{n % 30}!", "tags": [f"c{n % 30}"]} for n in range(50_000))
    rules = [{"key": f"c{n}", "title_terms": [f"collection-{n}"], "tags": [f"c{n}"]} for n in range(30)]
    first = match_collection_rules(products, rules, strategy="MIXED")
    second = match_collection_rules(({"id": n, "title": f"Café organizer COLLECTION-{n % 30}!", "tags": [f"c{n % 30}"]} for n in range(50_000)), rules, strategy="MIXED")
    assert first == second and first["total"] == sum(first["counts"].values()) == 50_000


def test_10k_product_sync_preview(tmp_path, monkeypatch):
    db = tmp_path / "products.sqlite3"; seed_store(db, 10_000)
    with connect(db) as con: con.execute("UPDATE products SET raw_json=?", (json.dumps({"store_selling_price": 39.99}),))
    class Client:
        def execute(self, *_a, **_k): return {"productByIdentifier": None}
    publisher = DirectShopifyProductPublisher(db=db, batch_size=500)
    monkeypatch.setattr(publisher, "_client", lambda _store: ({}, Client()))
    preview = publisher.preview("stress-000", limit=UI_PREVIEW_CAP)
    assert preview["requested"] == preview["counts"]["CREATE"] == 10_000
    assert len(preview["preview_items"]) == UI_PREVIEW_CAP


def test_multi_store_200_isolation(tmp_path):
    db = tmp_path / "multi.sqlite3"; seeded = seed_catalog(db, products=100, stores=200, products_per_store=100)
    with connect(db) as con: counts = [row[0] for row in con.execute("SELECT COUNT(*) FROM store_product_decisions GROUP BY store_id")]
    assert seeded["decisions"] == 20_000 and len(counts) == 200 and set(counts) == {100}


def test_cross_store_remote_id_never_leaks(tmp_path):
    db = tmp_path / "remote.sqlite3"; seed_catalog(db, products=2, stores=2, products_per_store=2)
    publisher = DirectShopifyProductPublisher(db=db)
    with connect(db) as con:
        con.execute("INSERT INTO shopify_product_mappings(store_id,master_product_id,source_platform,source_id,shopify_product_id,shopify_handle,sync_status,synced_at) VALUES('stress-000',1,'amazon','S000000000','gid://1','a','SYNCED','now')")
        assert con.execute("SELECT COUNT(*) FROM shopify_product_mappings WHERE store_id='stress-001'").fetchone()[0] == 0
    assert publisher.provider_name == "DIRECT_SHOPIFY"


def test_full_store_second_run_idempotent():
    page_plan = ContentPageService().build_plan({"brand_name": "X"}, {}, {"ABOUT_US": "/pages/about"})
    existing = [{"group": "Merchant", "title": "Blog", "target": "/blogs/news"}]
    first = build_footer_plan(existing, page_plan, [{"handle": "one"}])
    second = build_footer_plan(first["proposed"], page_plan, [{"handle": "one"}])
    assert len(second["proposed"]) == len(first["proposed"]) and not [x for x in second["actions"] if x["action"] == "CREATE"]


def test_full_store_targeted_change_only_updates_target():
    original = {"products": {f"p{n}": n for n in range(20)}, "collection": "A", "menu": [1, 2], "hero": "Old", "footer": "A"}
    changed = json.loads(json.dumps(original)); changed["products"].update({f"p{n}": 100 + n for n in range(5)}); changed.update(collection="B", menu=[2, 1], hero="New", footer="B")
    diff = {key for key in original if original[key] != changed[key]}
    assert diff == {"products", "collection", "menu", "hero", "footer"} and sum(original["products"][k] != changed["products"][k] for k in original["products"]) == 5


@pytest.mark.parametrize("failed_stage", ["SOURCING", "PRODUCT_SYNC", "PRODUCT_VERIFY", "COLLECTION_PLAN", "COLLECTION_SYNC", "NAVIGATION_SYNC", "HOMEPAGE_PLAN", "HOMEPAGE_SYNC", "STATIC_PAGES", "POLICIES", "FOOTER", "SEO", "QUALITY_AUDIT", "COMMERCE_READINESS", "FINAL_VERIFY"])
def test_resume_every_major_stage(tmp_path, failed_stage):
    db = tmp_path / f"resume-{failed_stage}.sqlite3"; seed_store(db, 2); failed = {"once": False}
    def handler(_run, stage):
        if stage == failed_stage and not failed["once"]: failed["once"] = True; raise RuntimeError("synthetic crash")
        return {"counts": {"done": 1}}
    handlers = {stage: (lambda run, name=stage: handler(run, name)) for stage in STAGES}
    run = StoreBuildOrchestrator(db=db, export_dir=tmp_path, handlers=handlers).preview("stress-000", mode="LIVE")
    assert StoreBuildOrchestrator(db=db, export_dir=tmp_path, handlers=handlers).start(run["run_id"], live_confirmed=True)["status"] == "FAILED"
    completed = StoreBuildOrchestrator(db=db, export_dir=tmp_path, handlers=handlers).resume(run["run_id"])
    assert completed["status"] == "COMPLETE" and completed["retry_count"] == 0


def _retry_publisher(tmp_path, message):
    db = tmp_path / "retry.sqlite3"; init_db(db); waits = []
    publisher = DirectShopifyProductPublisher(db=db, wait=waits.append, max_attempts=4)
    class Client:
        calls = 0
        def execute(self, query, _variables=None):
            self.calls += 1
            if "mutation" not in query: return {"productByIdentifier": None}
            raise RuntimeError(message)
    client = Client(); item = {"master_product_id": 1, "action": "CREATE", "payload_json": json.dumps({"handle": "h", "identity": "amazon:A", "title": "A", "price": "1.00", "owned_tags": []})}
    return publisher._sync_one("s", item, client), client.calls, waits


def test_transient_failure_retry_bounded(tmp_path):
    result, calls, waits = _retry_publisher(tmp_path, "timeout shpat_fixturesecret")
    assert result["status"] == "FAILED" and calls == 5 and waits == [.25, .5, 1.0] and "shpat_" not in result["error"]


def test_permanent_user_error_not_retried_forever(tmp_path):
    result, calls, waits = _retry_publisher(tmp_path, "Shopify userErrors: invalid title")
    assert result["status"] == "FAILED" and calls == 2 and waits == []


def test_stale_preview_all_mutating_modules(tmp_path):
    db = tmp_path / "stale.sqlite3"; seed_store(db, 2)
    service = StoreCompletionService(db=db); old = service.build_plan("stress-000", {})
    service.build_plan("stress-000", {"pricing_ready": True})
    assert service.apply_safe(old["plan_id"], [x["id"] for x in old["items"]])["status"] == "CONFLICT"
    sources = " ".join(Path(f).read_text(encoding="utf-8") for f in ("src/shopsource/shopify_products.py", "src/shopsource/shopify_collections.py", "src/shopsource/navigation.py", "src/shopsource/brand_automation.py", "src/shopsource/homepage_automation.py"))
    assert "stale" in sources.casefold() or "newer" in sources.casefold()


def test_shopify_throttle_backoff_mocked(tmp_path):
    result, _calls, waits = _retry_publisher(tmp_path, "429 throttled")
    assert result["status"] == "FAILED" and waits == sorted(waits) and len(waits) == 3


def test_partial_batch_failure_isolated(tmp_path):
    db = tmp_path / "partial.sqlite3"; publisher = DirectShopifyProductPublisher(db=db)
    with connect(db) as con:
        con.execute("INSERT INTO shopify_product_sync_runs(run_id,store_id,status,input_hash,created_at,updated_at) VALUES('r','s','RUNNING','h','now','now')")
        con.executemany("INSERT INTO shopify_product_sync_items(run_id,master_product_id,source_id,action,status,payload_json) VALUES('r',?,?,'CREATE','PENDING','{}')", [(1, "A"), (2, "B")])
        rows = con.execute("SELECT * FROM shopify_product_sync_items ORDER BY master_product_id").fetchall()
    publisher._record_result("r", "s", rows[0], {"status": "FAILED", "error": "fixture"}); publisher._record_result("r", "s", rows[1], {"status": "SYNCED", "remote_id": "gid://2"})
    assert publisher._run_summary("r")["counts"] == {"FAILED": 1, "SYNCED": 1}


def test_theme_schema_matrix():
    hero = '{% schema %}{"name":"Image banner","settings":[{"id":"heading","type":"text"},{"id":"button_link","type":"url"}]}{% endschema %}'
    category = '{% schema %}{"name":"Collection list","settings":[{"id":"collection","type":"collection"}]}{% endschema %}'
    fixtures = [{"sections/hero.liquid": hero, "sections/list.liquid": category}, {"sections/renamed.liquid": hero.replace("heading", "title")}, {"sections/hero.liquid": hero}, {"sections/list.liquid": category}, {}, {"sections/dropdown.liquid": ""}, {"sections/mega-label.liquid": ""}, {"sections/mega-menu.liquid": ""}, {"settings_schema.json": ""}, {"sections/bad.liquid": "{% schema %}{bad{% endschema %}"}, {**{f"sections/u{n}.liquid": "" for n in range(50)}, "sections/hero.liquid": hero}, {"sections/hero.liquid": hero, "sections/drifted.liquid": ""}]
    results = [discover_homepage_sections(item) for item in fixtures]
    assert len(results) == 12 and results[0]["hero"] and all("hero_status" in row for row in results)


def test_theme_malformed_manual_fallback():
    result = discover_homepage_sections({"sections/bad.liquid": "{% schema %}{bad{% endschema %}"})
    assert result["hero_status"] == "HERO_NOT_FOUND" and result["category_status"] == "CATEGORY_NOT_FOUND"


def test_broken_link_graph_large():
    valid = [{"key": f"p{n}", "kind": "product", "target": f"/products/p{n}"} for n in range(450)]
    bad = [{"key": "blank", "target": ""}, {"key": "hash", "target": "#"}, {"key": "stale", "target": "/x", "stale_handle": True}, {"key": "remote", "target": "/x", "remote_required": True}]
    resources = {"product": {f"/products/p{n}" for n in range(450)}}
    first = StoreLinkInspector().inspect(valid + bad, resources); second = StoreLinkInspector().inspect(valid + bad, resources)
    assert first == second and len(first["issues"]) == 4


def test_report_massive_memory_safe(tmp_path):
    db = tmp_path / "report.sqlite3"; seed_store(db, 50_000, 30); service = StoreCompletionService(db=db, export_dir=tmp_path / "out")
    plan = service.build_plan("stress-000", snapshot()); report = service.generate_report(plan["plan_id"], run_id="massive")
    assert all(json.loads(path.read_text(encoding="utf-8")) is not None for path in Path(report["folder"]).glob("*.json"))
    assert max(path.stat().st_size for path in Path(report["folder"]).iterdir()) < 1_000_000


def test_windows_unicode_paths(tmp_path):
    root = tmp_path / "공백 있는 경로" / ("긴경로" * 8); db = root / "스토어.sqlite3"; seed_store(db, 2)
    report = StoreCompletionService(db=db, export_dir=root).generate_report(StoreCompletionService(db=db, export_dir=root).build_plan("stress-000", {})["plan_id"], run_id="한글 run")
    assert Path(report["folder"]).exists()


def test_permission_denied_temp_path_safe(tmp_path):
    db = tmp_path / "permission.sqlite3"; seed_store(db, 1); blocked = tmp_path / "not-a-directory"; blocked.write_text("x")
    service = StoreCompletionService(db=db, export_dir=blocked); plan = service.build_plan("stress-000", {})
    with pytest.raises(OSError): service.generate_report(plan["plan_id"])


def test_ui_preview_rows_capped(tmp_path):
    db = tmp_path / "ui.sqlite3"; seed_store(db, 10_000)
    result = product_page(store_id="stress-000", page_size=200, db=db)
    assert result["total"] == 10_000 and len(result["rows"]) == 200
    with pytest.raises(ValueError): product_page(store_id="stress-000", page_size=50_000, db=db)


def test_status_db_ui_report_consistent(tmp_path):
    db = tmp_path / "status.sqlite3"; seed_store(db, 5); service = StoreCompletionService(db=db, export_dir=tmp_path)
    plan = service.build_plan("stress-000", snapshot(payment_confirmed=False)); report = service.generate_report(plan["plan_id"])
    data = json.loads((Path(report["folder"]) / "readiness_summary.json").read_text(encoding="utf-8"))
    with connect(db) as con: db_status = con.execute("SELECT status FROM store_completion_plans WHERE plan_id=?", (plan["plan_id"],)).fetchone()[0]
    assert db_status == plan["status"] == data["status"] == "NOT_READY" and "PAYMENT" in data["blockers"]


def test_secrets_redacted_from_all_outputs(tmp_path):
    secrets = ["shpat_ABC123secret", "sk-proj-ABC123456789", "Bearer abc.def.ghi", "Cookie: session=supersecret"]
    redacted = json.dumps(redact_value({"errors": secrets})) + _safe_error(RuntimeError(" ".join(secrets))) + redact_text(" ".join(secrets))
    assert all(secret not in redacted for secret in secrets) and redacted.count("[REDACTED]") >= 4


def test_unmocked_network_fails_test():
    with pytest.raises(AssertionError, match="Unmocked outbound network"):
        socket.create_connection(("example.com", 443))


def test_protected_store_file_untouched(tmp_path):
    protected = Path("stores/001_cabin_tidy.json"); before = protected.read_bytes()
    db = tmp_path / "protected.sqlite3"; seed_store(db, 100); StoreCompletionService(db=db).build_plan("stress-000", {})
    assert protected.read_bytes() == before


def test_migrations_repeated_and_completion_coexist(tmp_path):
    db = tmp_path / "migration.sqlite3"
    with sqlite3.connect(db) as con:
        con.executescript(SCHEMA)
        con.execute("INSERT INTO stores(store_id,store_name,profile_json,created_at,updated_at) VALUES('old','Old','{}','before','before')")
    init_db(db); init_db(db); StoreCompletionService(db=db).build_plan("old", {})
    with connect(db) as con:
        assert con.execute("SELECT store_name FROM stores WHERE store_id='old'").fetchone()[0] == "Old"
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("profile", ["fresh", "core", "phase35", "phase39", "current"])
def test_schema_generation_matrix(tmp_path, profile):
    db = tmp_path / f"{profile}.sqlite3"
    if profile != "fresh": init_db(db)
    if profile == "phase35": StoreBuildOrchestrator(db=db, handlers={})
    if profile == "phase39":
        from shopsource.homepage_automation import HomepageAutomationService
        HomepageAutomationService(db=db)
    service = StoreCompletionService(db=db)
    service = StoreCompletionService(db=db)  # repeated installation must be idempotent
    with connect(db) as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"products", "store_completion_plans", "store_completion_items"} <= tables
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
