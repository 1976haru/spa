from __future__ import annotations

import json

import pytest

from shopsource.db import connect, init_db, upsert_store
from shopsource.brand_automation import _install as install_brand_schema
from shopsource.collection_planner import CollectionPlanner
from shopsource.homepage_assignment import discover_assignment_capability
from shopsource.homepage_automation import hero_image_prompt, normalize_text_value
from shopsource.homepage_collections import ShopifyThemeReader
from shopsource.homepage_featured_products import ShopifyExistingProductReader
from shopsource.shopify_theme_ids import legacy_featured_id_for_store, is_valid_shopify_instance_id
from shopsource.homepage_session import HOMEPAGE_BUTTON_CONTRACTS, HomepagePrerequisiteService, validate_homepage_contract
from shopsource.shopify_collections import _install_schema as install_shopify_schema


def _section(name, settings, blocks=None):
    schema = {"name": name, "settings": settings}
    if blocks is not None: schema["blocks"] = blocks
    return "{% schema %}" + json.dumps(schema) + "{% endschema %}"


def _theme_files():
    return {
        "sections/image-banner.liquid": _section("Image banner", [
            {"type": "image_picker", "id": "image", "label": "Image"},
            {"type": "text", "id": "heading", "label": "Heading"},
            {"type": "richtext", "id": "text", "label": "Text"},
            {"type": "text", "id": "button_label", "label": "Button label"},
            {"type": "url", "id": "button_link", "label": "Button link"},
        ]),
        "sections/collection-list.liquid": _section("Collection list", [
            {"type": "collection_list", "id": "collections", "label": "Collections"}]),
        "sections/featured-products.liquid": _section("Featured products", [
            {"type": "product_list", "id": "products", "label": "Products"}]),
        "sections/featured-collection.liquid": _section("Featured collection", [
            {"type": "collection", "id": "collection", "label": "Collection"}]),
    }


def _products():
    titles = ["Universal Trunk Organizer Cargo Storage", "Backseat Organizer for Travel",
              "Car Trash Can with Lid", "Cup Holder Organizer Tray",
              "Center Console Lid Replacement Fits 1999-2007 Silverado Sierra OEM",
              "Seat Back Storage Organizer", "Visor Document Holder", "Foldable Cargo Organizer"]
    rows = []
    for number, title in enumerate(titles, 1):
        rows.append({"id": f"gid://shopify/Product/{number}", "handle": f"garage-item-{number}", "title": title,
            "status": "ACTIVE", "productType": "Car Organizer" if number != 5 else "Automotive Replacement Part",
            "tags": ["storage"], "createdAt": f"2026-10-{number:02d}T00:00:00Z", "updatedAt": "2026-10-08T00:00:00Z",
            "onlineStoreUrl": None, "featuredMedia": {"image": {"url": f"https://cdn.example/{number}.jpg", "altText": title}},
            "variants": {"nodes": [{"price": "85.00"}]}, "variantsCount": {"count": 1},
            "publishedOnCurrentPublication": True})
    return rows


class FakeReadOnlyShopifyGraphQL:
    products = _products()
    writes = 0
    calls = []
    scopes = {"read_products", "read_themes"}

    def __init__(self, *args): pass

    def execute(self, query, variables=None):
        self.calls.append(query)
        if "mutation" in query.casefold():
            self.writes += 1
            raise AssertionError("Golden Path must never invoke a Shopify mutation")
        if "products(first" in query:
            return {"currentAppInstallation": {"accessScopes": [{"handle": scope} for scope in sorted(self.scopes)]},
                    "products": {"nodes": list(self.products), "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        if "currentAppInstallation" in query:
            return {"currentAppInstallation": {"accessScopes": [{"handle": scope} for scope in sorted(self.scopes)]}}
        if "themes(first" in query:
            return {"themes": {"nodes": [{"id": "gid://shopify/OnlineStoreTheme/1", "name": "Fixture", "role": "MAIN"}]}}
        if "theme(id:" in query:
            legacy_featured = legacy_featured_id_for_store("001")
            template_raw = "/* Shopify header comment: Cabin Tidy homepage template */\n" + json.dumps({
                "sections": {"header": {"type": "header", "settings": {}},
                             legacy_featured: {"type": "featured-products", "settings": {}},
                             "merchant": {"type": "rich-text", "settings": {"text": "Preserve"}},
                             "footer": {"type": "footer", "settings": {}}},
                "order": ["header", legacy_featured, "merchant", "footer"]})
            files = [{"filename": "templates/index.json", "body": {"content": template_raw}}]
            files.extend({"filename": name, "body": {"content": raw}} for name, raw in _theme_files().items())
            return {"theme": {"id": "gid://shopify/OnlineStoreTheme/1", "name": "Fixture", "role": "MAIN",
                              "files": {"nodes": files, "userErrors": []}}}
        raise AssertionError(f"Unexpected read query: {query}")


@pytest.fixture
def golden_service(tmp_path, monkeypatch):
    db = tmp_path / "golden.sqlite3"
    init_db(db)
    upsert_store({"store_id": "001", "store_name": "Cabin Tidy", "category": "Automotive Organization",
                  "sourcing_categories": ["Trunk Storage", "Backseat Organization", "Car Cleanup", "Cup Holder Storage"]}, db)
    install_brand_schema(db)
    now = "2026-10-08T00:00:00+00:00"
    brand = {"store_id": "001", "brand_name": "Cabin Tidy", "primary_category": ["car organization", "travel storage"],
             "target_customer": {"audience": "US drivers", "use_case": "commuting"}, "target_country": "United States",
             "personality": ["practical", {"tone": "trustworthy"}], "brand_keywords": ["organized", "useful"],
             "colors": {"primary": "navy", "accent": "sand"}, "avoid_styles": ["busy scenes", {"avoid": ["logos", "watermarks"]}]}
    with connect(db) as con:
        con.execute("INSERT INTO brand_profiles(store_id,version,profile_json,source_hash,approval_status,created_at,updated_at) VALUES(?,1,?,?, 'DRAFT',?,?)",
                    ("001", json.dumps(brand), "fixture-brand", now, now))
    collection = CollectionPlanner(db).create_plan("001", settings={"include_empty": True, "min_products": 0})
    install_shopify_schema(db)
    with connect(db) as con:
        for row in collection["collections"]:
            key = row["collection_key"]
            con.execute("""INSERT INTO shopify_collection_mappings(store_id,collection_key,handle,shopify_collection_id,
                last_synced_hash,last_synced_at,published_ids_json,image_url) VALUES(?,?,?,?,?,?,?,?)""",
                ("001", key, f"live-{key}", f"gid://shopify/Collection/{key}", "verified", now, "[]", None))
    FakeReadOnlyShopifyGraphQL.writes = 0
    FakeReadOnlyShopifyGraphQL.calls = []
    FakeReadOnlyShopifyGraphQL.products = _products()
    FakeReadOnlyShopifyGraphQL.scopes = {"read_products", "read_themes"}
    for module in ("shopsource.homepage_collections", "shopsource.homepage_featured_products"):
        monkeypatch.setattr(f"{module}.get_connection", lambda *a, **k: {"shop_domain": "fixture.myshopify.com", "api_version": "2026-07"})
        monkeypatch.setattr(f"{module}.get_shopify_token", lambda *a, **k: ("fixture-token", "test"))
    product_reader = ShopifyExistingProductReader(db=db, client_factory=FakeReadOnlyShopifyGraphQL)
    theme_reader = ShopifyThemeReader(db=db, client_factory=FakeReadOnlyShopifyGraphQL)
    return HomepagePrerequisiteService(db=db, product_reader=product_reader, theme_reader=theme_reader), db


def test_homepage_text_normalization_handles_nested_values_and_cycles():
    nested = ["  clean  ", {"avoid": ["logos", None]}, 17, True]
    nested.append(nested)
    normalized = normalize_text_value(nested)
    assert normalized == "clean, avoid: logos, 17, true"
    prompt = hero_image_prompt({"profile": {"primary_category": ["garage", "organization"],
        "colors": {"primary": "navy", "accent": "sand"}, "avoid_styles": ["logos", "watermarks"]}})
    assert "garage, organization" in prompt and "Avoid: logos, watermarks" in prompt


def test_homepage_golden_path_fresh_session_to_assignment_review(golden_service):
    service, db = golden_service
    FakeReadOnlyShopifyGraphQL.calls = []
    design = service.resolve("001", "DESIGN", state={})
    assert design.error is None and design.homepage_plan and design.collection_plan
    assert design.statuses["theme"] == "CONNECTED"
    assert design.theme_snapshot["template_status"] == "READY"
    assert design.theme_snapshot["template_document"]["had_leading_comment"] is True
    assert design.theme_snapshot["template_document"]["raw_hash"]
    featured = design.featured_product_plan
    assert featured["status"] == "READY" and len(featured["items"]) == 4
    assert all("replacement" not in item["title"].casefold() for item in featured["items"])
    # The dialog is a serialization of these four durable plan rows, not a second selector.
    review_cards = [{key: item.get(key) for key in ("position", "title", "image_url", "price", "remote_status", "shopify_handle", "selection_reason", "verification_status")}
                    for item in featured["items"]]
    assert len(review_cards) == 4 and all(card["image_url"] and card["price"] and card["shopify_handle"] for card in review_cards)
    state = {"featured_products_plan": featured, "snapshot": design.theme_snapshot,
             "snapshot_store_id": "001", "snapshot_read_at": "2026-10-08T00:00:00+00:00"}
    featured_preview = service.resolve("001", "FEATURED_PREVIEW", state=state)
    assert featured_preview.error is None
    assert featured_preview.canonical_preview["status"] == "PREVIEW"
    assert featured_preview.canonical_preview["capabilities"]["featured_products"] == "AUTO"
    assert featured_preview.canonical_preview["capabilities"]["categories"] == "AUTO"
    fp = featured_preview.canonical_preview["featured_products_preview"]
    assert fp["status"] == "PREVIEW"
    assert fp["migration"]["from"] == legacy_featured_id_for_store("001")
    assert is_valid_shopify_instance_id(fp["section_id"])
    assert fp["migration"]["from"] not in featured_preview.canonical_preview["proposed"]["sections"]
    assert fp["migration"]["from"] not in featured_preview.canonical_preview["proposed"]["order"]
    assert featured_preview.canonical_preview["proposed"]["order"].count(fp["section_id"]) == 1
    product_section = featured_preview.canonical_preview["proposed"]["sections"][fp["section_id"]]
    assert len(product_section["settings"]["products"]) == 4
    full = service.resolve("001", "FULL_PREVIEW", state={**state, "snapshot": featured_preview.theme_snapshot})
    assert full.canonical_preview["status"] == "PREVIEW"
    assert full.canonical_preview["proposed"]["sections"][fp["section_id"]] == product_section
    checklist = service.resolve("001", "ASSIGNMENT_CHECK", state={**state, "snapshot": full.theme_snapshot})
    assert checklist.assignment_check["status"] == "REVIEW_REQUIRED"
    assert checklist.assignment_check["checks"]["featured_products_visible_in_proposal"] is True
    assert checklist.assignment_check["checks"]["featured_products_4_of_4"] is True
    assert checklist.assignment_check["write_status"] == "NOT_RUN"
    assert FakeReadOnlyShopifyGraphQL.writes == 0
    assert all("mutation" not in query.casefold() for query in FakeReadOnlyShopifyGraphQL.calls)
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM store_homepage_previews WHERE store_id='001'").fetchone()[0] >= 2


def test_homepage_golden_path_no_cached_snapshot_auto_reads_theme(golden_service):
    service, _ = golden_service
    FakeReadOnlyShopifyGraphQL.calls = []
    result = service.resolve("001", "FULL_PREVIEW", state={"featured_products_plan": None})
    assert result.error is None and result.theme_snapshot["status"] == "CONNECTED"
    assert any("themes(first" in query for query in FakeReadOnlyShopifyGraphQL.calls)
    assert result.canonical_preview["featured_products_preview"]["status"] == "PREVIEW"


def test_featured_preview_is_independent_of_manual_category_capability(golden_service, monkeypatch):
    import sys
    fixtures = sys.modules[__name__]
    service, _ = golden_service
    original = fixtures._theme_files
    monkeypatch.setattr(fixtures, "_theme_files", lambda: {
        name: raw for name, raw in original().items() if name != "sections/collection-list.liquid"
    })
    featured = service.resolve("001", "FEATURED_PREVIEW", state={}, force_theme=True)
    assert featured.error is None
    assert featured.canonical_preview["status"] == "PREVIEW"
    assert featured.canonical_preview["capabilities"]["featured_products"] == "AUTO"
    assert featured.canonical_preview["capabilities"]["categories"] == "MANUAL"
    assert featured.canonical_preview["manual_reasons"]
    full = service.resolve("001", "FULL_PREVIEW", state={}, force_theme=True)
    assert full.canonical_preview["status"] == "PARTIAL_PREVIEW"
    assert full.canonical_preview["capabilities"]["categories"] == "MANUAL"
    assert full.canonical_preview["proposed"]["sections"]["merchant"] == full.canonical_preview["current"]["sections"]["merchant"]
    assert full.canonical_preview["featured_products_preview"]["status"] == "PREVIEW"
    assert FakeReadOnlyShopifyGraphQL.writes == 0


def test_homepage_golden_path_read_products_scope_failure_is_actionable(golden_service):
    service, _ = golden_service
    FakeReadOnlyShopifyGraphQL.scopes = {"read_themes"}
    # Existing authenticated GraphQL product reader checks scope before selection.
    result = service.resolve("001", "FULL_PREVIEW", state={})
    assert result.error and "read_products" in result.error["message"]
    assert result.featured_product_plan["status"] == "MANUAL_ACTION_REQUIRED"
    assert FakeReadOnlyShopifyGraphQL.writes == 0


def test_homepage_golden_path_read_themes_scope_failure_is_actionable(golden_service):
    service, _ = golden_service
    FakeReadOnlyShopifyGraphQL.scopes = {"read_products"}
    result = service.resolve("001", "FULL_PREVIEW", state={})
    assert result.error and "read_themes" in result.error["message"]
    assert result.theme_snapshot["status"] == "MISSING_READ_SCOPE"
    assert FakeReadOnlyShopifyGraphQL.writes == 0


def test_homepage_upstream_change_invalidates_downstream_preview(golden_service):
    service, db = golden_service
    initial = service.resolve("001", "FULL_PREVIEW", state={})
    preview_id = initial.canonical_preview["preview_id"]
    fresh_products = _products()
    fresh_products[-1]["title"] = "New organizer candidate"
    FakeReadOnlyShopifyGraphQL.products = fresh_products
    next_plan = service.resolve("001", "FEATURED_PLAN", state={"featured_products_plan": initial.featured_product_plan})
    # Feature selection has a separate selector; changing it makes the old canonical proposal stale.
    from shopsource.homepage_featured_products import FeaturedProductAssignmentService
    FeaturedProductAssignmentService(db=db).reselect(initial.featured_product_plan, reader=service.product_reader)
    with connect(db) as con:
        row = con.execute("SELECT status FROM store_homepage_previews WHERE preview_id=?", (preview_id,)).fetchone()
    assert row["status"] == "STALE"
    from shopsource.homepage_automation import HomepageAutomationService
    assert HomepageAutomationService(db=db).apply(preview_id, confirmed=True)["status"] == "CONFLICT"
    assert next_plan.featured_product_plan is not None


def test_homepage_button_contracts_have_visible_effect_and_no_silent_review_button():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert "on_click=show_featured_review" in source and "featured_review_dialog.open()" in source
    assert "resolve_homepage_context(\"FEATURED_PREVIEW\")" in source
    assert "resolve_homepage_context(\"FULL_PREVIEW\")" in source
    assert "resolve_homepage_context(\"ASSIGNMENT_CHECK\")" in source
    assert "현재 진행 상태" in source and "Shopify write: NOT RUN" in source
    assert "다시 선택" in source and "service.reselect(previous)" in source
    assert "on_click=rollback_homepage" in source and "on_click=apply_confirm" in source
    expected = {"홈페이지 자동 설계", "홈페이지 자동 완성", "메인 배너 자동 만들기", "카테고리 바로가기 자동 만들기",
                "추천 상품 자동 구성", "상품 4개 보기", "다시 선택", "Shopify 상품 다시 읽기",
                "추천 상품 미리보기", "홈페이지 미리보기", "과제 제출용 확인", "Shopify 적용", "롤백"}
    assert expected.issubset(HOMEPAGE_BUTTON_CONTRACTS)
    assert all(HOMEPAGE_BUTTON_CONTRACTS[name].get("visible_effect") for name in expected)
    assert all(HOMEPAGE_BUTTON_CONTRACTS[name]["write"] is False for name in expected - {"Shopify 적용", "롤백"})


def test_homepage_golden_path_external_builder_stops_with_manual_fallback(golden_service):
    service, _ = golden_service
    reader = service.theme_reader
    class PageFlyReader:
        def discover(self, store_id):
            snapshot = reader.discover(store_id)
            snapshot["page_builder"] = "PageFly"
            return snapshot
    service.theme_reader = PageFlyReader()
    result = service.resolve("001", "FULL_PREVIEW", state={})
    assert result.canonical_preview["status"] == "MANUAL_ACTION_REQUIRED"
    assert "자동 클릭하지 않습니다" in result.error["message"]
    assert result.canonical_preview["proposed"] == result.canonical_preview["current"]
    assert FakeReadOnlyShopifyGraphQL.writes == 0


def test_homepage_contract_errors_are_contextual_not_attribute_errors():
    validation = validate_homepage_contract("homepage_plan", {"hero": [], "categories": "wrong"})
    assert validation["status"] == "INVALID_DATA" and validation["paths"] == ["homepage_plan.hero"]
    assert "AttributeError" not in " ".join(validation["paths"])


def test_external_page_builder_fallback_is_explicit_and_never_gui_automated():
    result = discover_assignment_capability({"builder": "PageFly"})
    assert result["status"] == "EXTERNAL_PAGE_BUILDER_MANUAL"
    assert "자동 클릭하지 않습니다" in result["reason"]
