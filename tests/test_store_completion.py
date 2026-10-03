from __future__ import annotations

import json
from pathlib import Path

import pytest

from shopsource.db import connect, init_db
from shopsource.store_build import COMPLETION_STAGES, STAGES, StoreBuildOrchestrator
from shopsource.store_completion import (
    DOMAINS, ContentPageService, StoreCompletionService, StoreLinkInspector,
    build_footer_plan, build_seo_plan, inspect_accessibility, inspect_cart,
    inspect_collection_template, inspect_commerce, inspect_media, inspect_mobile,
    inspect_product_template, inspect_search,
)


def theme_files():
    names = (
        "product-title product-media price variant-selector quantity add-to-cart product-form description vendor inventory availability related-products collapsible",
        "collection-title collection-description collection-image product-grid sort filter pagination columns-mobile no-products",
        "header-search search-modal templates/search no-results product-card predictive-search",
        "main-cart cart-items quantity cart-remove subtotal checkout cart-empty mobile",
    )
    return {f"sections/{name}.liquid": "{% schema %}" + json.dumps({"name": name}) + "{% endschema %}" for name in names}


def complete_snapshot():
    pages = {key: f"/pages/{key.lower()}" for key in ("ABOUT_US", "CONTACT", "FAQ", "SHIPPING", "RETURNS", "PRIVACY", "TERMS", "REFUND_POLICY")}
    return {
        "theme_files": theme_files(), "routes": ["/search", "/cart"], "brand": {"brand_name": "Cabin Tidy"},
        "brand_references": {"homepage": "Cabin Tidy storage", "about": "About Cabin Tidy"},
        "business": {"support_email": "support@example.test", "return_window": "configured", "return_address": "configured",
                     "processing_time": "configured", "shipping_time": "configured", "shipping_fee": "configured",
                     "company_legal_name": "configured", "company_address": "configured", "phone": "configured", "governing_law": "configured"},
        "pages": pages, "collections": [{"handle": f"c{i}"} for i in range(8)], "footer_links": [],
        "links": [{"key": "shop", "kind": "collection", "target": "/collections/c0", "exists": True}],
        "resources": {"collection": {"/collections/c0"}}, "navigation_ready": True, "homepage_ready": True,
        "footer_ready": True, "pricing_ready": True, "inventory_policy_ready": True,
        "seo": {"title": "Cabin Tidy", "social_image": "hero.png"}, "seo_suggestions": {},
        "accessibility": {"images": [{"reference": "hero", "alt": "Cabin storage"}], "controls": []},
        "mobile": {key: True for key in ("hero_mobile_crop", "logo_width", "mobile_navigation", "collection_columns",
                                           "product_media", "text_overflow_review", "cta_length_review", "category_shortcuts", "footer_stacking")},
        "media": [{"id": "hero", "reference": "hero.png", "alt": "Cabin storage", "rights_status": "RIGHTS_CONFIRMED"}],
        "target_market": "US", "primary_market": "US", "store_currency": "USD", "presentment_currencies": ["USD"],
        "myshopify_domain": "fixture.myshopify.com", "ssl_state": "ACTIVE", "published_theme": True,
        "shipping_origin": "US fixture", "shipping_markets": ["US"], "shipping_rates": ["fixture-rate"],
        "shipping_content_config_match": True, "tax_status": "CONFIGURED", "payment_confirmed": True,
        "payment_methods": ["fixture"], "cart_to_checkout": True, "launch_blocked": False,
        "shopify_analytics": True, "final_verification": True,
    }


@pytest.fixture
def completion_env(tmp_path):
    db = tmp_path / "completion.sqlite3"; init_db(db)
    with connect(db) as con:
        con.execute("INSERT INTO stores(store_id,store_name,profile_json,created_at,updated_at) VALUES('s1','Cabin Tidy','{}','now','now')")
        for n in range(1000):
            cur = con.execute("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                              (f"B{n:09d}", f"Synthetic product {n}", "{}", "now", "now"))
            con.execute("INSERT INTO store_product_decisions(store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at) VALUES(?,?,?,?,?,?,?)",
                        ("s1", cur.lastrowid, "IN_RANGE", "SAFE", "PRIMARY", "PRIMARY", "now"))
        con.executescript("""
        CREATE TABLE brand_profiles(profile_id TEXT,store_id TEXT);
        CREATE TABLE collection_plans(plan_id TEXT,store_id TEXT);
        CREATE TABLE collection_plan_items(plan_id TEXT,collection_key TEXT);
        CREATE TABLE navigation_plans(plan_id TEXT,store_id TEXT);
        CREATE TABLE store_homepage_plans(plan_id TEXT,store_id TEXT);
        CREATE TABLE shopify_product_sync_runs(run_id TEXT,store_id TEXT);
        INSERT INTO brand_profiles VALUES('bp1','s1'); INSERT INTO collection_plans VALUES('cp1','s1');
        INSERT INTO navigation_plans VALUES('np1','s1'); INSERT INTO store_homepage_plans VALUES('hp1','s1');
        INSERT INTO shopify_product_sync_runs VALUES('ps1','s1');
        """)
        for n in range(8): con.execute("INSERT INTO collection_plan_items VALUES('cp1',?)", (f"c{n}",))
    return db, StoreCompletionService(db=db, export_dir=tmp_path / "exports")


def test_completion_plan_includes_all_domains(completion_env):
    _, service = completion_env; plan = service.build_plan("s1", complete_snapshot())
    assert {x["area"] for x in plan["items"]} == set(DOMAINS)
    assert all(x["status"] in {"VERIFIED", "MANUAL_ACTION_REQUIRED", "BLOCKED"} for x in plan["items"])


def test_completion_plan_reuses_existing_phase_outputs(completion_env):
    _, service = completion_env; plan = service.build_plan("s1", complete_snapshot())
    assert plan["references"] == {"brand_profile_id": "bp1", "collection_plan_id": "cp1", "navigation_plan_id": "np1", "homepage_plan_id": "hp1", "product_sync_run_id": "ps1"}
    assert next(x for x in plan["items"] if x["area"] == "BRAND")["details"].get("profile_blob") is None


def test_required_blocker_prevents_ready(completion_env):
    _, service = completion_env; snapshot = complete_snapshot(); snapshot["shipping_rates"] = []
    assert service.build_plan("s1", snapshot)["status"] == "NOT_READY"


def test_score_cannot_override_blocker(completion_env):
    _, service = completion_env; snapshot = complete_snapshot(); snapshot["payment_confirmed"] = False
    plan = service.build_plan("s1", snapshot)
    assert plan["readiness_score"] > 80 and plan["status"] == "NOT_READY"


def test_product_template_detects_core_sections():
    result = inspect_product_template(theme_files()); assert result["status"] == "PRESENT" and all(result["checks"].values())


def test_product_template_does_not_generate_fake_reviews():
    result = inspect_product_template({}); assert result["prohibited_content_generated"] is False
    assert not any(word in json.dumps(result).casefold() for word in ("fake review", "fake rating", "countdown", "scarcity", "guarantee"))


def test_collection_template_integrity():
    assert inspect_collection_template(theme_files(), [{"kind": "collection", "exists": True}])["status"] == "PRESENT"


def test_search_basic_required_predictive_optional():
    files = theme_files()
    key = next(k for k in files if "predictive-search" in k)
    value = files.pop(key).replace("predictive-search", "")
    files[key.replace("predictive-search", "")] = value
    result = inspect_search(files, ["/search"]); assert result["status"] == "VERIFIED" and not result["predictive_search_required"]


def test_cart_core_controls_detected():
    result = inspect_cart(theme_files(), ["/cart"]); assert result["status"] == "VERIFIED" and not result["checkout_performed"]


def test_footer_plan_preserves_existing_links():
    existing = [{"group": "Merchant", "title": "Journal", "target": "/blogs/news"}]
    pages = ContentPageService().build_plan({"brand_name": "Cabin Tidy"}, {}, {"ABOUT_US": "/pages/about"})
    result = build_footer_plan(existing, pages, []); assert result["merchant_links_preserved"] and existing[0] in result["proposed"]


def test_static_page_unknown_business_facts_not_invented():
    plan = ContentPageService().build_plan({"brand_name": "Cabin Tidy"})
    assert plan["unknown_business_fields"] and all(p["content"]["known_facts"] == {} for p in plan["pages"])


def test_contact_page_no_fake_phone_address():
    page = next(x for x in ContentPageService().build_plan({})["pages"] if x["page_type"] == "CONTACT")
    assert "phone" not in page["content"]["known_facts"] and "company_address" not in page["content"]["known_facts"]


def test_faq_unknown_answer_requires_input():
    page = next(x for x in ContentPageService().build_plan({})["pages"] if x["page_type"] == "FAQ")
    assert all(x["answer_status"] == "NEEDS_BUSINESS_INPUT" for x in page["content"]["topics"])


def test_shipping_content_config_conflict_blocks():
    snapshot = complete_snapshot(); snapshot["shipping_content_config_match"] = False
    assert inspect_commerce(snapshot)["shipping"]["status"] == "BLOCKED"


def test_policy_requires_user_review(completion_env):
    _, service = completion_env; snapshot = complete_snapshot(); snapshot["pages"].pop("PRIVACY")
    policy = next(x for x in service.build_plan("s1", snapshot)["items"] if x["area"] == "POLICIES")
    assert policy["status"] == "BLOCKED" and policy["details"]["legal_certified"] is False


def test_seo_preserves_unmanaged_metadata():
    plan = build_seo_plan({"title": "Merchant title"}, {"title": "Generated title"})
    assert plan["proposed"]["title"] == "Merchant title" and plan["actions"][0]["action"] == "PRESERVE_UNMANAGED"


def test_accessibility_does_not_claim_wcag_compliance():
    result = inspect_accessibility({}); assert "do not establish WCAG compliance" in result["disclaimer"] and result["manual_audit"] == "MANUAL_AUDIT_REQUIRED"


def test_mobile_readiness_plan():
    result = inspect_mobile(complete_snapshot()["mobile"]); assert result["status"] == "VERIFIED" and result["browser_device_test"] == "DEFERRED_TO_PHASE_4_1"


def test_media_rights_unknown_flagged():
    result = inspect_media([{"id": "x", "reference": "x.jpg", "alt": "x"}]); assert result["assets"][0]["rights_status"] == "RIGHTS_REVIEW_REQUIRED"


def test_broken_link_placeholder_detected():
    result = StoreLinkInspector().inspect([{"title": "Bad", "target": "#"}]); assert result["status"] == "BLOCKED" and result["issues"][0]["code"] == "PLACEHOLDER"


def test_market_target_mismatch_warning():
    snapshot = complete_snapshot(); snapshot["primary_market"] = "KR"
    assert inspect_commerce(snapshot)["markets"]["status"] == "NEEDS_REVIEW"


def test_domain_myshopify_not_false_blocked():
    result = inspect_commerce(complete_snapshot())["domain"]
    assert result["status"] == "VERIFIED" and result["custom_domain_recommended"]


def test_shipping_missing_target_rate_blocks():
    snapshot = complete_snapshot(); snapshot["shipping_rates"] = []
    assert inspect_commerce(snapshot)["shipping"]["status"] == "BLOCKED"


def test_tax_unknown_manual_review():
    snapshot = complete_snapshot(); snapshot.pop("tax_status")
    assert inspect_commerce(snapshot)["tax"]["status"] == "MANUAL_ACTION_REQUIRED"


def test_payment_requires_confirmation():
    snapshot = complete_snapshot(); snapshot["payment_confirmed"] = False
    assert inspect_commerce(snapshot)["payment"]["status"] == "BLOCKED"


def test_checkout_not_ready_without_shipping_payment():
    snapshot = complete_snapshot(); snapshot["shipping_rates"] = []; snapshot["payment_confirmed"] = False
    assert inspect_commerce(snapshot)["checkout"]["status"] == "BLOCKED"


def test_analytics_optional(completion_env):
    _, service = completion_env; snapshot = complete_snapshot(); snapshot["shopify_analytics"] = False
    item = next(x for x in service.build_plan("s1", snapshot)["items"] if x["area"] == "ANALYTICS")
    assert item["status"] == "MANUAL_ACTION_REQUIRED" and not item["details"]["external_tracking_required"]


def test_brand_name_consistency_detects_stale_placeholder(completion_env):
    _, service = completion_env; snapshot = complete_snapshot(); snapshot["brand_references"]["homepage"] = "Old Placeholder deals"
    item = next(x for x in service.build_plan("s1", snapshot)["items"] if x["area"] == "BRAND")
    assert item["details"]["diff"] and not item["details"]["auto_overwrite"]


def test_apply_safe_never_runs_manual_required(completion_env):
    _, service = completion_env; plan = service.build_plan("s1", {})
    manual = next(x for x in plan["items"] if x["automation_mode"] == "MANUAL_REQUIRED")
    result = service.apply_safe(plan["plan_id"], [manual["id"]]); assert result["refused"] and result["remote_writes"] == 0


def test_store_build_completion_stage_order():
    expected = ("PRODUCT_TEMPLATE", "COLLECTION_TEMPLATE", "STATIC_PAGES", "POLICIES", "FOOTER", "SEO", "SEARCH", "CART", "QUALITY_AUDIT", "COMMERCE_READINESS", "FINAL_VERIFY", "LAUNCH_READINESS")
    assert all(STAGES.index(a) < STAGES.index(b) for a, b in zip(expected, expected[1:]))
    assert COMPLETION_STAGES[0] == "PLAN" and COMPLETION_STAGES[-1] == "COMPLETE"


def test_store_build_resume_after_manual_gate(completion_env):
    db, _ = completion_env; calls = []
    handlers = {stage: (lambda run, name=stage: calls.append(name) or {"counts": {}}) for stage in STAGES}
    handlers["POLICIES"] = lambda _run: {"status": "MANUAL_ACTION_REQUIRED", "manual_gate": "COMPLETION_REVIEW"}
    service = StoreBuildOrchestrator(db=db, handlers=handlers); run = service.preview("s1", mode="LIVE")
    assert service.start(run["run_id"], live_confirmed=True)["stage"] == "POLICIES"
    assert service.resume(run["run_id"], manual_confirmation="manual_action_confirmed")["status"] == "COMPLETE_WITH_WARNINGS"


def test_report_contains_blockers_and_manual_actions(completion_env):
    _, service = completion_env; plan = service.build_plan("s1", {})
    report = service.generate_report(plan["plan_id"], run_id="fixture")
    assert {"readiness_summary.json", "readiness_summary.md", "blockers.json", "manual_actions.md", "warnings.json"} <= set(report["files"])
    assert json.loads((Path(report["folder"]) / "blockers.json").read_text(encoding="utf-8"))


def test_no_real_network_in_tests(completion_env, monkeypatch):
    _, service = completion_env
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("network called"))
    plan = service.build_plan("s1", complete_snapshot()); assert service.verify(plan["plan_id"])["ready"]


def test_protected_store_file_untouched(completion_env):
    _, service = completion_env; protected = Path("stores/001_cabin_tidy.json")
    before = protected.read_bytes() if protected.exists() else None
    service.build_plan("s1", complete_snapshot())
    after = protected.read_bytes() if protected.exists() else None
    assert before == after
