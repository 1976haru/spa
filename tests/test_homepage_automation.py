from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from shopsource import homepage_automation as hp
from shopsource.homepage_automation import (
    HomepageAutomationService, assignment_banner_check, assignment_category_check,
    build_homepage_plan, build_homepage_preview, category_shortcuts, compose_homepage_preview,
    discover_homepage_sections, generate_hero_image, hero_copy, hero_image_prompt,
    validate_homepage_image,
)
from shopsource.store_build import STAGES


@pytest.fixture
def db(tmp_path):
    return tmp_path / "homepage.sqlite3"


@pytest.fixture
def brand():
    return {"version": 1, "profile": {"brand_name": "Field & Form", "primary_category": "home organization",
        "target_customer": "busy households", "target_country": "United States", "personality": "calm, practical, premium",
        "colors": "deep green, cream", "avoid_styles": "busy scenes"}}


@pytest.fixture
def collections():
    return {"plan_id": "CP_test", "collections": [
        {"collection_key": "entry-storage", "title": "Entry Storage", "handle": "entry-storage", "priority": 1,
         "enabled": True, "estimated_product_count": 20, "image_prompt": "Entry bench with baskets", "image_alt_text": "Entry storage"},
        {"collection_key": "closet", "title": "Closet Organizers", "handle": "closet", "priority": 2,
         "enabled": True, "estimated_product_count": 12, "image_prompt": "Closet organizers", "image_alt_text": "Closet organizers"},
        {"collection_key": "disabled", "title": "Disabled", "handle": "disabled", "priority": 3,
         "enabled": False, "estimated_product_count": 100},
    ]}


def section(name, settings, blocks=None):
    schema = {"name": name, "settings": settings}
    if blocks is not None: schema["blocks"] = blocks
    return "{% schema %}" + json.dumps(schema) + "{% endschema %}"


@pytest.fixture
def snapshot():
    hero = section("Image banner", [
        {"type": "image_picker", "id": "image", "label": "Image"},
        {"type": "text", "id": "heading", "label": "Heading"},
        {"type": "richtext", "id": "text", "label": "Text"},
        {"type": "text", "id": "button_label", "label": "Button label"},
        {"type": "url", "id": "button_link", "label": "Button link"},
    ])
    category = section("Collection list", [{"type": "collection_list", "id": "collections", "label": "Collections"}])
    template = {"sections": {"header": {"type": "header", "settings": {}}, "merchant": {"type": "rich-text", "settings": {"text": "keep"}},
                            "footer": {"type": "footer", "settings": {}}}, "order": ["header", "merchant", "footer"]}
    return {"status": "CONNECTED", "theme": {"id": "theme-1", "name": "Example", "role": "MAIN"},
            "scopes": ["read_themes", "write_themes"], "template_filename": "templates/index.json", "template": template,
            "theme_files": {"templates/index.json": "/* Shopify header comment */\n" + json.dumps(template),
                            "sections/image-banner.liquid": hero, "sections/collection-list.liquid": category}}


def make_plan(db, brand, collections):
    return build_homepage_plan(store_id="demo", brand=brand, collection_plan=collections,
        collection_handles={"entry-storage": "entry-storage-live", "closet": "closet-live"}, db=db)


def test_homepage_plan_from_brand_and_collections(db, brand, collections):
    plan = make_plan(db, brand, collections)
    assert plan["hero"]["headline"] and plan["hero"]["image_prompt"]
    assert [item["collection_key"] for item in plan["categories"]] == ["entry-storage", "closet"]
    assert plan["hero"]["cta_target"] == "/collections/entry-storage-live"


def test_hero_copy_generated_without_fake_claims(brand, collections):
    copy = hero_copy(brand, collections["collections"], {"entry-storage": "entry-storage-live"})
    text = " ".join(str(value) for value in copy.values()).casefold()
    assert not any(word in text for word in ("#1", "guaranteed", "limited time", "best seller", "five stars"))


def test_hero_cta_requires_real_target(brand, collections):
    assert hero_copy(brand, collections["collections"], {})["cta_target"] is None


def test_hero_prompt_no_embedded_text(brand):
    prompt = hero_image_prompt(brand).casefold()
    assert all(term in prompt for term in ("no embedded text", "no logo", "no watermark", "mobile-safe"))


def test_hero_asset_opt_in_required_for_paid_generation(tmp_path):
    class Paid:
        provider_name = "OPENAI_IMAGES"
        def generate(self, *args, **kwargs): raise AssertionError("must gate before provider call")
    with pytest.raises(RuntimeError, match="opt-in"):
        generate_hero_image("demo", {"plan_id": "p", "hero": {"image_prompt": "prompt"}}, provider=Paid(), output_dir=tmp_path)


def test_hero_image_validation(tmp_path):
    path = tmp_path / "wide.png"
    Image.new("RGB", (1600, 700), "white").save(path)
    assert validate_homepage_image(path)["valid"]
    small = tmp_path / "small.png"
    Image.new("RGB", (300, 200), "white").save(small)
    assert not validate_homepage_image(small)["valid"]


def test_category_shortcuts_from_collection_plan(db, brand, collections):
    result = category_shortcuts(collections, collection_handles={"entry-storage": "live-1", "closet": "live-2"})
    assert len(result["items"]) == 2
    assert result["items"][0]["target"] == "/collections/live-1"


def test_missing_collection_skipped_not_wrongly_mapped(collections):
    result = category_shortcuts(collections, collection_handles={"entry-storage": "live-1"})
    missing = next(item for item in result["items"] if item["collection_key"] == "closet")
    assert missing["status"] == "SKIP_REMOTE" and missing["target"] is None


def test_category_duplicate_target_warning(collections):
    result = category_shortcuts(collections, collection_handles={"entry-storage": "same", "closet": "same"})
    assert any(item["code"] == "DUPLICATE_TARGET" for item in result["warnings"])


def test_category_duplicate_asset_warning(collections):
    image = {"path": "same.png", "approval_status": "APPROVED"}
    result = category_shortcuts(collections, collection_handles={"entry-storage": "one", "closet": "two"},
                                collection_assets={"entry-storage": image, "closet": image})
    assert any(item["code"] == "DUPLICATE_ASSET" for item in result["warnings"])


def test_existing_collection_image_reused(collections):
    result = category_shortcuts(collections, collection_handles={"entry-storage": "one"},
                                collection_assets={"entry-storage": {"path": "approved.webp", "approval_status": "APPROVED"}})
    assert result["items"][0]["image_source"] == "COLLECTION_IMAGE_REUSE"


def test_theme_detect_hero_high_confidence(snapshot):
    assert discover_homepage_sections(snapshot["theme_files"])["hero_status"] == "HERO_SUPPORTED_HIGH_CONFIDENCE"


def test_theme_detect_collection_list_high_confidence(snapshot):
    assert discover_homepage_sections(snapshot["theme_files"])["category_status"] == "CATEGORY_SUPPORTED_HIGH_CONFIDENCE"


def test_theme_detect_multicolumn_fallback(db, brand, collections, snapshot):
    snapshot["theme_files"]["sections/multicolumn.liquid"] = section("Multicolumn", [], [{"type": "column", "settings": [
        {"type": "text", "id": "title", "label": "Title"}, {"type": "url", "id": "link", "label": "Link"},
        {"type": "image_picker", "id": "image", "label": "Image"}]}])
    snapshot["theme_files"].pop("sections/collection-list.liquid")
    plan = make_plan(db, brand, collections)
    preview = build_homepage_preview(plan, snapshot, db=db)
    assert preview["discovery"]["category"] == "CATEGORY_SUPPORTED_REVIEW_REQUIRED"
    category = next(action for action in preview["actions"] if action.get("kind") == "CATEGORY")
    assert category["action"] == "CREATE_SECTION"
    category_section = next(v for k, v in preview["proposed"]["sections"].items() if k.startswith("ss_categories_"))
    assert all("/collections/" in block["settings"]["link"] for block in category_section["blocks"].values())


def test_theme_unknown_manual_fallback(db, brand, collections):
    plan = make_plan(db, brand, collections)
    snap = {"theme": {"id": "t", "role": "MAIN"}, "template_filename": None, "template": None, "theme_files": {}}
    preview = build_homepage_preview(plan, snap, db=db)
    assert preview["status"] == "MANUAL_ACTION_REQUIRED"


def test_homepage_preserves_unrelated_sections(db, brand, collections, snapshot):
    plan = make_plan(db, brand, collections)
    plan["hero"]["image_url"] = "https://cdn.example/hero.png"
    preview = build_homepage_preview(plan, snapshot, db=db)
    assert preview["proposed"]["sections"]["merchant"] == snapshot["template"]["sections"]["merchant"]
    assert preview["proposed"]["sections"]["footer"] == snapshot["template"]["sections"]["footer"]
    assert preview["proposed"]["order"][0] == "header"


def test_repeat_apply_no_duplicate_hero(db, brand, collections, snapshot):
    plan = make_plan(db, brand, collections)
    first = build_homepage_preview(plan, snapshot, db=db)
    second = build_homepage_preview(plan, snapshot, db=db)
    hero_ids = [key for key in second["proposed"]["sections"] if key.startswith("ss_hero_")]
    assert len(hero_ids) == 1
    assert len([a for a in second["actions"] if a.get("kind") == "HERO" and a["action"] == "CREATE_SECTION"]) == 1


def test_repeat_apply_no_duplicate_category_section(db, brand, collections, snapshot):
    plan = make_plan(db, brand, collections)
    preview = build_homepage_preview(plan, snapshot, db=db)
    category_ids = [key for key in preview["proposed"]["sections"] if key.startswith("ss_categories_")]
    assert len(category_ids) == 1


def test_homepage_preview_invalidated_on_change(db, brand, collections, snapshot):
    plan = make_plan(db, brand, collections)
    first = build_homepage_preview(plan, snapshot, db=db)
    plan["hero"]["headline"] = "Changed headline"
    second = build_homepage_preview(plan, snapshot, db=db)
    result = HomepageAutomationService(db=db).apply(first["preview_id"], confirmed=True)
    assert result["status"] == "CONFLICT"
    assert first["source_hash"] != second["source_hash"]


def test_homepage_apply_preview_no_write(db, brand, collections, snapshot):
    plan = make_plan(db, brand, collections)
    preview = build_homepage_preview(plan, snapshot, db=db)
    assert preview["status"] == "PREVIEW"
    assert preview["proposed"] != preview["current"]


def test_homepage_apply_manual_for_review_required_mapping(db, brand, collections, snapshot):
    snapshot["theme_files"].pop("sections/image-banner.liquid")
    plan = make_plan(db, brand, collections)
    preview = build_homepage_preview(plan, snapshot, db=db)
    assert preview["status"] == "MANUAL_ACTION_REQUIRED"


class FakeThemeClient:
    def __init__(self, current):
        self.current = current
        self.current_raw = "/* Shopify header comment */\n" + json.dumps(current)
        self.writes = 0
    def execute(self, query, variables=None):
        if "Scopes" in query: return {"currentAppInstallation": {"accessScopes": [{"handle": "read_themes"}, {"handle": "write_themes"}]}}
        if "themeFilesUpsert" in query:
            self.writes += 1
            self.current_raw = variables["files"][0]["body"]["value"]
            from shopsource.shopify_theme_json import parse_shopify_json_document
            self.current = parse_shopify_json_document(self.current_raw).parsed
            return {"themeFilesUpsert": {"userErrors": []}}
        return {"theme": {"id": "theme-1", "role": "MAIN", "files": {"nodes": [{"filename": "templates/index.json", "body": {"content": self.current_raw}}], "userErrors": []}}}


def test_homepage_backup_before_write(monkeypatch, tmp_path, db, brand, collections, snapshot):
    monkeypatch.setattr(hp, "get_connection", lambda *a, **k: {"shop_domain": "test.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr(hp, "get_shopify_token", lambda *a, **k: ("fake-token", None))
    plan = make_plan(db, brand, collections)
    plan["hero"].update(image_url="https://cdn.example/hero.png", image_asset_id="asset", asset_approved=True,
                         theme_image_ref="shopify://shop_images/hero.png", theme_image_ref_confirmed=True)
    preview = build_homepage_preview(plan, snapshot, db=db)
    client = FakeThemeClient(snapshot["template"])
    service = HomepageAutomationService(db=db, export_dir=tmp_path)
    result = service.apply(preview["preview_id"], confirmed=True, approved_assets=True, client=client)
    assert result["status"] == "VERIFIED" and client.writes == 1, result
    backup = Path(tmp_path / "theme_backups" / "demo")
    before_raw_path = next(backup.rglob("before.raw.json"))
    proposed_raw_path = next(backup.rglob("proposed.raw.json"))
    assert before_raw_path.read_text(encoding="utf-8").startswith("/* Shopify header comment */\n")
    assert proposed_raw_path.read_text(encoding="utf-8").startswith("/* Shopify header comment */\n")
    assert list(backup.rglob("before.parsed.json")) and list(backup.rglob("proposed.parsed.json"))
    assert client.current_raw.startswith("/* Shopify header comment */\n")
    assert service.verify(preview["preview_id"], client=client)["status"] == "VERIFIED"


def test_homepage_verify_after_write(monkeypatch, tmp_path, db, brand, collections, snapshot):
    test_homepage_backup_before_write(monkeypatch, tmp_path, db, brand, collections, snapshot)


def test_homepage_rollback(monkeypatch, tmp_path, db, brand, collections, snapshot):
    monkeypatch.setattr(hp, "get_connection", lambda *a, **k: {"shop_domain": "test.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr(hp, "get_shopify_token", lambda *a, **k: ("fake-token", None))
    plan = make_plan(db, brand, collections); plan["hero"].update(image_url="https://cdn.example/hero.png", image_asset_id="asset", asset_approved=True,
        theme_image_ref="shopify://shop_images/hero.png", theme_image_ref_confirmed=True)
    preview = build_homepage_preview(plan, snapshot, db=db); client = FakeThemeClient(snapshot["template"])
    service = HomepageAutomationService(db=db, export_dir=tmp_path)
    result = service.apply(preview["preview_id"], confirmed=True, approved_assets=True, client=client)
    backup_folder = Path(tmp_path / "theme_backups" / "demo")
    before_raw = next(backup_folder.rglob("before.raw.json")).read_text(encoding="utf-8")
    rollback = service.rollback(result["backup_id"], confirmed=True, client=client)
    assert rollback["status"] == "VERIFIED" and rollback["rollback_mode"] == "EXACT_RAW_ROLLBACK"
    assert client.current_raw == before_raw


def test_legacy_rollback_still_supported(monkeypatch, tmp_path, db, brand, collections, snapshot):
    monkeypatch.setattr(hp, "get_connection", lambda *a, **k: {"shop_domain": "test.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr(hp, "get_shopify_token", lambda *a, **k: ("fake-token", None))
    plan = make_plan(db, brand, collections)
    plan["hero"].update(image_url="https://cdn.example/hero.png", image_asset_id="asset", asset_approved=True,
        theme_image_ref="shopify://shop_images/hero.png", theme_image_ref_confirmed=True)
    preview = build_homepage_preview(plan, snapshot, db=db)
    client = FakeThemeClient(snapshot["template"])
    service = HomepageAutomationService(db=db, export_dir=tmp_path)
    applied = service.apply(preview["preview_id"], confirmed=True, approved_assets=True, client=client)
    backup_dir = next((tmp_path / "theme_backups" / "demo").iterdir())
    (backup_dir / "before.raw.json").unlink()
    (backup_dir / "proposed.raw.json").unlink()
    rollback = service.rollback(applied["backup_id"], confirmed=True, client=client)
    assert rollback["status"] == "VERIFIED"
    assert rollback["rollback_mode"] == "LEGACY_PARSED_ROLLBACK"


def test_apply_drift_check_uses_raw_and_semantic_hash(monkeypatch, tmp_path, db, brand, collections, snapshot):
    monkeypatch.setattr(hp, "get_connection", lambda *a, **k: {"shop_domain": "test.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr(hp, "get_shopify_token", lambda *a, **k: ("fake-token", None))
    plan = make_plan(db, brand, collections)
    plan["hero"].update(image_url="https://cdn.example/hero.png", asset_approved=True,
        theme_image_ref="shopify://shop_images/hero.png", theme_image_ref_confirmed=True)
    preview = build_homepage_preview(plan, snapshot, db=db)
    client = FakeThemeClient(snapshot["template"])
    client.current_raw = "/* Shopify header comment changed without semantic JSON change */\n" + json.dumps(snapshot["template"])
    result = HomepageAutomationService(db=db, export_dir=tmp_path).apply(
        preview["preview_id"], confirmed=True, approved_assets=True, client=client)
    assert result["status"] == "CONFLICT"
    assert "raw or semantic" in result["reason"]
    assert client.writes == 0


def test_repeat_apply_no_change_after_verified_write(monkeypatch, tmp_path, db, brand, collections, snapshot):
    monkeypatch.setattr(hp, "get_connection", lambda *a, **k: {"shop_domain": "test.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr(hp, "get_shopify_token", lambda *a, **k: ("fake-token", None))
    plan = make_plan(db, brand, collections)
    plan["hero"].update(image_url="https://cdn.example/hero.png", image_asset_id="asset", asset_approved=True,
                         theme_image_ref="shopify://shop_images/hero.png", theme_image_ref_confirmed=True)
    client = FakeThemeClient(snapshot["template"]); service = HomepageAutomationService(db=db, export_dir=tmp_path)
    first = build_homepage_preview(plan, snapshot, db=db)
    assert service.apply(first["preview_id"], confirmed=True, approved_assets=True, client=client)["status"] == "VERIFIED"
    next_snapshot = {**snapshot, "template": client.current,
                     "theme_files": {**snapshot["theme_files"], "templates/index.json": client.current_raw}}
    second = build_homepage_preview(plan, next_snapshot, db=db)
    assert all(a["action"] == "NO_CHANGE" for a in second["actions"] if a.get("kind") in {"HERO", "CATEGORY"})
    writes = client.writes
    assert service.apply(second["preview_id"], confirmed=True, approved_assets=True, client=client)["status"] == "VERIFIED"
    assert client.writes == writes


def test_homepage_apply_remote_drift_conflict(monkeypatch, tmp_path, db, brand, collections, snapshot):
    monkeypatch.setattr(hp, "get_connection", lambda *a, **k: {"shop_domain": "test.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr(hp, "get_shopify_token", lambda *a, **k: ("fake-token", None))
    plan = make_plan(db, brand, collections); plan["hero"].update(image_url="https://cdn.example/hero.png", asset_approved=True,
        theme_image_ref="shopify://shop_images/hero.png", theme_image_ref_confirmed=True)
    preview = build_homepage_preview(plan, snapshot, db=db)
    client = FakeThemeClient({"sections": {}, "order": ["manual drift"]})
    result = HomepageAutomationService(db=db, export_dir=tmp_path).apply(preview["preview_id"], confirmed=True, approved_assets=True, client=client)
    assert result["status"] == "CONFLICT" and client.writes == 0


def test_assignment_banner_mode(db, brand, collections):
    result = assignment_banner_check(make_plan(db, brand, collections))
    assert result["headline"] and result["cta_link"] and "Hero image missing" in result["warnings"]


def test_assignment_category_shortcut_mode(db, brand, collections):
    result = assignment_category_check(make_plan(db, brand, collections))
    assert len(result["items"]) == 2 and result["valid"]


def test_store_build_homepage_stage_order():
    stages = list(STAGES)
    assert stages.index("NAVIGATION_VERIFY") < stages.index("HOMEPAGE_PLAN") < stages.index("HOMEPAGE_VERIFY") < stages.index("BRAND_APPLY") < stages.index("FINAL_VERIFY")
    assert stages.index("HERO_ASSET_PREVIEW") < stages.index("CATEGORY_SHORTCUT_PLAN") < stages.index("HOMEPAGE_SYNC_PREVIEW")


def test_no_real_network_in_tests(monkeypatch, db, brand, collections, snapshot):
    def blocked(*args, **kwargs): raise AssertionError("network must not be called by a preview")
    monkeypatch.setattr(hp, "ShopifyGraphQLClient", blocked)
    plan = make_plan(db, brand, collections)
    assert build_homepage_preview(plan, snapshot, db=db)["status"] == "PREVIEW"


def test_protected_store_file_untouched(db, brand, collections):
    plan = make_plan(db, brand, collections)
    assert plan["store_id"] == "demo"
    assert not (Path(__file__).parents[1] / "stores" / "001_cabin_tidy.json").is_relative_to(Path(db).parent)


def test_homepage_featured_plan_composes_without_reordering_merchants(db, brand, collections, snapshot):
    plan = make_plan(db, brand, collections)
    preview = compose_homepage_preview(plan, snapshot, collections,
        collection_handles={"entry-storage": "entry-storage-live", "closet": "closet-live"}, db=db)
    assert preview["current"]["order"] == ["header", "merchant", "footer"]
    assert preview["proposed"]["sections"]["merchant"] == snapshot["template"]["sections"]["merchant"]


def test_unmanaged_hero_causes_conflict(db, brand, collections, snapshot):
    plan = make_plan(db, brand, collections)
    snapshot["template"]["sections"]["merchant-hero"] = {"type": "image-banner", "settings": {}}
    preview = build_homepage_preview(plan, snapshot, db=db)
    assert preview["status"] == "CONFLICT"
