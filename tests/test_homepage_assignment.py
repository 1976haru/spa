from shopsource.homepage_assignment import (
    HomepageAssignmentService, discover_assignment_capability,
    homepage_assignment_workflow, prepare_banner, prepare_category_shortcuts,
)


def fixtures(mode="COLLECTION_LIST"):
    collections = [{"collection_key": f"c{i}", "title": f"Category {i}", "priority": i, "enabled": True} for i in range(6)]
    mappings = {f"c{i}": {"collection_key": f"c{i}", "handle": f"category-{i}", "remote_id": f"gid://{i}", "status": "VERIFIED"} for i in range(6)}
    images = {f"c{i}": {"path": f"c{i}.jpg", "approval_status": "APPROVED", "alt_text": f"Category {i}"} for i in range(6)}
    home = {"hero": {"enabled": True, "headline": "Organize your drive", "body": "Practical storage.",
                            "cta_label": "Shop now", "cta_target": "/collections/category-0", "alt_text": "Organized trunk"},
            "unrelated_sections": [{"id": "merchant-newsletter", "settings": {"title": "Keep me"}}]}
    theme = {"sections": [{"mode": mode}]}
    asset = {"asset_id": "hero1", "path": "hero.jpg", "provider": "MANUAL", "approval_status": "APPROVED"}
    return home, {"collections": collections}, mappings, images, theme, asset


def test_banner_assignment_beginner_one_click():
    values = fixtures(); preview = HomepageAssignmentService().build(homepage=values[0], collection_plan=values[1], mappings=values[2], approved_images=values[3], theme=values[4], hero_asset=values[5])
    assert preview["sequence"][0] == "HERO" and preview["theme_write_requires_confirmation"]


def test_banner_manual_asset_supported():
    home, _, mappings, _, _, asset = fixtures()
    result = prepare_banner(home["hero"], real_targets={"/collections/category-0"}, asset=asset)
    assert result["hero"]["asset_kind"] == "MANUAL_ASSET" and result["status"] == "READY"


def test_banner_precomposed_text_asset_warns_mobile_accessibility():
    home, _, _, _, _, asset = fixtures(); asset["precomposed_text"] = True
    result = prepare_banner(home["hero"], real_targets={"/collections/category-0"}, asset=asset)
    assert {"MOBILE_CROP_REVIEW", "ACCESSIBILITY_TEXT_DUPLICATION_REVIEW"}.issubset(result["warnings"])


def test_banner_cta_requires_real_collection_target():
    home = fixtures()[0]
    assert "CTA_REQUIRES_REAL_TARGET" in prepare_banner(home["hero"], real_targets=set())["warnings"]


def test_homepage_auto_complete_preserves_unrelated_sections():
    v = fixtures(); result = HomepageAssignmentService().build(homepage=v[0], collection_plan=v[1], mappings=v[2], approved_images=v[3], theme=v[4], hero_asset=v[5])
    assert result["proposed"]["unrelated_sections"] == v[0]["unrelated_sections"]


def test_category_assignment_from_collection_plan():
    _, plan, mappings, images, theme, _ = fixtures()
    assert len(prepare_category_shortcuts(plan, mappings, images, theme)["items"]) == 6


def test_category_shortcut_each_target_is_correct_collection():
    _, plan, mappings, images, theme, _ = fixtures()
    assert all(x["target_correct"] for x in prepare_category_shortcuts(plan, mappings, images, theme)["items"])


def test_category_shortcut_wrong_duplicate_target_blocked():
    _, plan, mappings, images, theme, _ = fixtures(); mappings["c1"]["handle"] = "category-0"
    result = prepare_category_shortcuts(plan, mappings, images, theme)
    assert result["status"] == "BLOCKED"


def test_category_shortcut_missing_remote_collection_skipped():
    _, plan, mappings, images, theme, _ = fixtures(); mappings["c0"].pop("remote_id")
    result = prepare_category_shortcuts(plan, mappings, images, theme)
    assert result["skipped"][0]["reason"] == "MISSING_REMOTE_COLLECTION"


def test_category_shortcut_reuses_approved_collection_images():
    _, plan, mappings, images, theme, _ = fixtures()
    assert prepare_category_shortcuts(plan, mappings, images, theme)["items"][0]["image_source"] == "APPROVED_COLLECTION_IMAGE_REUSE"


def test_category_shortcut_native_collection_list_preferred():
    assert discover_assignment_capability({"sections": [{"mode": "MULTICOLUMN"}, {"mode": "COLLECTION_LIST"}]})["category_mode"] == "COLLECTION_LIST"


def test_category_shortcut_multicolumn_fallback():
    assert discover_assignment_capability({"sections": [{"mode": "MULTICOLUMN"}]}) == {"status": "NATIVE_THEME_REVIEW_REQUIRED", "category_mode": "MULTICOLUMN", "reason": "Multicolumn fallback의 링크와 이미지를 검토해야 합니다."}


def test_external_page_builder_never_gui_automated():
    v = fixtures(); v[4]["builder"] = "PageFly"
    preview = HomepageAssignmentService().build(homepage=v[0], collection_plan=v[1], mappings=v[2], approved_images=v[3], theme=v[4], hero_asset=v[5])
    assert preview["external_gui_automation"] is False
    assert not any(x["task_key"] == "THEME_WRITE" for x in homepage_assignment_workflow(preview))


def test_external_page_builder_manual_fallback_truthful():
    capability = discover_assignment_capability({"builder": "PageFly"})
    assert capability["status"] == "EXTERNAL_PAGE_BUILDER_MANUAL" and "자동 클릭하지 않습니다" in capability["reason"]


def test_assignment_ready_summary():
    v = fixtures(); preview = HomepageAssignmentService().build(homepage=v[0], collection_plan=v[1], mappings=v[2], approved_images=v[3], theme=v[4], hero_asset=v[5])
    result = HomepageAssignmentService().checklist(preview, applied=True, verified=True, desktop_checked=True, mobile_checked=True)
    assert result["status"] == "ASSIGNMENT_READY" and all(result["checks"].values())
