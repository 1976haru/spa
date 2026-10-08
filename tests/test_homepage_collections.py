from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from shopsource.homepage_collections import (
    HomepageCollectionService, ShopifyThemeReader, build_homepage_plan,
    detect_featured_collection_schemas,
)
from shopsource.shopify_theme_ids import is_valid_shopify_instance_id, legacy_collection_section_id
from shopsource.shopify_collections import save_connection


@pytest.fixture
def tmp_path():
    path = Path.cwd() / "exports" / ".test_scratch" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    return path


def section_file(*, ratio=True):
    settings = [{"type": "collection", "id": "collection", "label": "Collection"}]
    if ratio:
        settings.append({"type": "select", "id": "image_ratio", "label": "Image ratio",
                         "default": "portrait", "options": [{"value": "square", "label": "Square"}, {"value": "portrait", "label": "Portrait"}]})
    settings.append({"type": "range", "id": "products_to_show", "label": "Products to show", "default": 4})
    return "{% schema %}" + json.dumps({"name": "Featured collection", "settings": settings}) + "{% endschema %}"


def fixture_snapshot(*, ratio=True, template=None):
    schemas = detect_featured_collection_schemas({"sections/featured-picks.liquid": section_file(ratio=ratio)})
    return {"status": "CONNECTED", "store_id": "001", "theme": {"id": "gid://shopify/OnlineStoreTheme/1", "name": "Fixture Theme", "role": "MAIN"},
            "template_filename": "templates/index.json", "template": template or {"sections": {"hero": {"type": "hero", "settings": {"title": "Welcome"}}, "footer": {"type": "footer", "settings": {}}}, "order": ["hero", "footer"]}, "schemas": schemas}


def sample_plan():
    return {"plan_id": "plan-fixture", "store_id": "001", "collections": [
        {"collection_key": "seat", "title": "Seat Storage", "handle": "seat-storage", "priority": 1, "estimated_product_count": 8, "warnings": [], "enabled": 1},
        {"collection_key": "trunk", "title": "Trunk Organizers", "handle": "trunk-organizers", "priority": 2, "estimated_product_count": 12, "warnings": [], "enabled": 1},
        {"collection_key": "console", "title": "Console Storage", "handle": "console-storage", "priority": 3, "estimated_product_count": 5, "warnings": [], "enabled": 1},
        {"collection_key": "cargo", "title": "Cargo & Travel", "handle": "cargo-travel", "priority": 4, "estimated_product_count": 3, "warnings": [], "enabled": 1},
    ]}


def make_plan(snapshot, *, db=None):
    plan = sample_plan()
    return build_homepage_plan(snapshot, plan, collection_handles={r["collection_key"]: r["handle"] for r in plan["collections"]}, db=db)


def test_theme_detect_featured_collection_schema():
    found = detect_featured_collection_schemas({"sections/featured-picks.liquid": section_file()})
    assert found[0]["type"] == "featured-picks"
    assert found[0]["collection_field"]["id"] == "collection"
    assert found[0]["ratio_fields"][0]["id"] == "image_ratio"


def test_theme_dry_run_no_write():
    result = make_plan(fixture_snapshot())
    assert result["status"] == "DRY_RUN"
    assert all(op["action"] == "CREATE SECTION" for op in result["operations"] if op.get("collection_key"))
    assert result["current"] != result["proposed"]


def test_theme_backup_before_write(tmp_path):
    result = make_plan(fixture_snapshot())
    service = HomepageCollectionService(db=tmp_path / "test.sqlite3", export_dir=tmp_path / "exports",
                                        clock=lambda: datetime(2026, 10, 3, tzinfo=timezone.utc))
    backup = service.save_safe_patch(result)
    assert backup["mode"] == "MANUAL_PATCH_MODE"
    assert json.loads(Path(backup["before"]).read_text(encoding="utf-8")) == result["current"]
    assert json.loads(Path(backup["proposed"]).read_text(encoding="utf-8")) == result["proposed"]
    assert "No Shopify theme write" in Path(backup["diff"]).read_text(encoding="utf-8")
    assert backup["theme_write_performed"] is False


def test_theme_apply_minimal_diff():
    snapshot = fixture_snapshot()
    result = make_plan(snapshot)
    assert result["proposed"]["sections"]["hero"] == snapshot["template"]["sections"]["hero"]
    assert result["proposed"]["sections"]["footer"] == snapshot["template"]["sections"]["footer"]
    assert result["proposed"]["order"][0] == "hero"
    assert result["proposed"]["order"][-1] == "footer"
    assert any(section_id in result["diff"]["managed_section_ids"] and is_valid_shopify_instance_id(section_id)
               for section_id in result["proposed"]["order"][1:-1])
    assert all("products_to_show" not in row["settings"] for row in result["proposed"]["sections"].values() if row.get("type") == "featured-picks")


def test_theme_repeat_apply_no_duplicate(tmp_path):
    db = tmp_path / "test.sqlite3"
    snapshot = fixture_snapshot()
    service = HomepageCollectionService(db=db, export_dir=tmp_path / "exports")
    first = make_plan(snapshot, db=db)
    service.save_safe_patch(first)
    service.record_applied_state(first)  # Isolated simulation; this does not call Shopify.
    second = make_plan({**snapshot, "template": first["proposed"]}, db=db)
    assert not any(row["action"] == "CREATE SECTION" for row in second["operations"])
    assert len([key for key in second["diff"]["managed_section_ids"] if key in second["proposed"]["sections"]]) == 4


def test_collection_preview_migrates_only_selected_legacy_instance_ids(tmp_path):
    db = tmp_path / "migration.sqlite3"
    snapshot = fixture_snapshot()
    first = make_plan(snapshot, db=db)
    legacy_template = json.loads(json.dumps(first["proposed"]))
    old_ids = []
    for row in first["operations"]:
        if row.get("collection_key") and row.get("section_id") in legacy_template["sections"]:
            old_id, new_id = legacy_collection_section_id(row["collection_key"]), row["section_id"]
            legacy_template["sections"][old_id] = legacy_template["sections"].pop(new_id)
            legacy_template["order"] = [old_id if item == new_id else item for item in legacy_template["order"]]
            old_ids.append(old_id)
    second = make_plan({**snapshot, "template": legacy_template}, db=db)
    assert old_ids and all(old not in second["proposed"]["sections"] and old not in second["proposed"]["order"] for old in old_ids)
    assert not any(operation["action"] == "CONFLICT" for operation in second["operations"])


def test_theme_drift_conflict(tmp_path):
    db = tmp_path / "test.sqlite3"
    snapshot = fixture_snapshot()
    service = HomepageCollectionService(db=db, export_dir=tmp_path / "exports")
    first = make_plan(snapshot, db=db)
    service.save_safe_patch(first)
    service.record_applied_state(first)  # Isolated simulation; this does not call Shopify.
    changed = json.loads(json.dumps(first["proposed"]))
    managed_id = next(key for key in changed["sections"] if key not in snapshot["template"]["sections"])
    changed["sections"][managed_id]["settings"]["collection"] = "manually-changed"
    result = make_plan({**snapshot, "template": changed}, db=db)
    assert result["status"] == "CONFLICT"
    assert any(row["action"] == "CONFLICT" for row in result["operations"])


def test_theme_rollback(tmp_path):
    result = make_plan(fixture_snapshot())
    service = HomepageCollectionService(db=tmp_path / "test.sqlite3", export_dir=tmp_path / "exports")
    backup = service.save_safe_patch(result)
    rollback = service.rollback_plan(backup["backup_id"])
    assert rollback["action"] == "MANUAL_ROLLBACK_PLAN"
    assert rollback["before"] == result["current"]
    assert rollback["theme_write_performed"] is False


def test_theme_unsupported_falls_back_to_manual_plan():
    snapshot = fixture_snapshot()
    snapshot["schemas"] = []
    result = make_plan(snapshot)
    assert result["status"] == "MANUAL_PATCH_MODE"
    assert result["manual_patch_mode"] is True
    assert result["proposed"] == result["current"]


def test_square_ratio_when_supported():
    result = make_plan(fixture_snapshot(ratio=True))
    sections = [row for row in result["proposed"]["sections"].values() if row.get("type") == "featured-picks"]
    assert all(row["settings"]["image_ratio"] == "square" for row in sections)


def test_square_ratio_fallback_when_unsupported():
    result = make_plan(fixture_snapshot(ratio=False))
    sections = [row for row in result["proposed"]["sections"].values() if row.get("type") == "featured-picks"]
    assert all("image_ratio" not in row["settings"] for row in sections)
    assert result["warnings"]


def test_existing_unrelated_sections_unchanged():
    original = fixture_snapshot()
    result = make_plan(original)
    assert result["proposed"]["sections"]["hero"] == original["template"]["sections"]["hero"]
    assert result["proposed"]["sections"]["footer"] == original["template"]["sections"]["footer"]


def test_no_live_theme_write_in_tests(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite3"
    save_connection("001", "fixture.myshopify.com", db=db)
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "fixture-token")

    class MockThemeClient:
        def __init__(self): self.calls = []
        def __call__(self, domain, token, version): return self
        def execute(self, query, variables=None):
            self.calls.append(query)
            if "currentAppInstallation" in query:
                return {"currentAppInstallation": {"accessScopes": [{"handle": "read_themes"}]}}
            if "ShopSourceThemes" in query:
                return {"themes": {"nodes": [{"id": "theme-1", "name": "Fixture Theme", "role": "MAIN"}]}}
            if "ShopSourceThemeFiles" in query:
                template_raw = "/* Shopify header comment */\n" + json.dumps({"sections": {}, "order": []})
                return {"theme": {"files": {"nodes": [
                    {"filename": "templates/index.json", "body": {"content": template_raw}},
                    {"filename": "sections/featured-picks.liquid", "body": {"content": section_file()}},
                ], "userErrors": []}}}
            raise AssertionError(query)
    mock = MockThemeClient()
    discovery = ShopifyThemeReader(db=db, client_factory=mock).discover("001")
    planned = build_homepage_plan(discovery, sample_plan(), collection_handles={r["collection_key"]: r["handle"] for r in sample_plan()["collections"]}, db=db)
    service = HomepageCollectionService(db=db, export_dir=tmp_path / "exports")
    service.save_safe_patch(planned)
    assert discovery["theme"]["role"] == "MAIN"
    assert discovery["template_status"] == "READY"
    assert discovery["template"] == {"sections": {}, "order": []}
    assert discovery["template_document"]["had_leading_comment"] is True
    assert discovery["template_document"]["raw_hash"]
    assert discovery["theme_files"]["templates/index.json"].startswith("/* Shopify header comment */")
    assert all("mutation" not in query.casefold() for query in mock.calls)
    assert not any("themeFilesUpsert" in query for query in mock.calls)
