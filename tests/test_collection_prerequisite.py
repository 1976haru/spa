import hashlib
import socket

import pytest

from shopsource.automation import AutomationTaskError, WorkflowAutomationService, collection_prerequisite_workflow
from shopsource.brand_automation import brand_profile_from_store
from shopsource.collection_planner import CollectionPlanner
from shopsource.collection_prerequisite import CollectionPrerequisiteService, prepare_homepage_prerequisites
from shopsource.db import connect, init_db, upsert_store
from shopsource.homepage_automation import build_homepage_plan
from shopsource.prompt_assets import PromptAssetService


def _store_db(tmp_path, *, category="Car Organization", with_category=True):
    db = tmp_path / "prerequisite.sqlite3"
    init_db(db)
    profile = {"store_id": "prereq-fixture", "store_name": "Cabin Tidy", "category": category,
               "concept": "practical vehicle storage" if category else "", "sourcing_categories": ["Trunk Storage"] if with_category else []}
    upsert_store(profile, db)
    return db


def _brand():
    return {"store_id": "prereq-fixture", "version": 1, "profile": {"brand_name": "Cabin Tidy",
        "primary_category": "Car Organization", "target_customer": "Drivers", "target_country": "US",
        "personality": ["practical"], "colors": {"primary": "navy"}}}


def test_prompt_generation_reuses_existing_collection_plan(tmp_path):
    db = _store_db(tmp_path)
    first = CollectionPrerequisiteService(db).ensure_collection_plan("prereq-fixture")
    assert first.status == "READY"
    second = CollectionPrerequisiteService(db).ensure_collection_plan("prereq-fixture")
    assert second.status == "READY" and second.plan["plan_id"] == first.plan["plan_id"]
    assert second.source == "AUTO_PREREQUISITE"


def test_prompt_generation_auto_creates_missing_collection_plan(tmp_path):
    db = _store_db(tmp_path)
    result = CollectionPrerequisiteService(db).ensure_collection_plan("prereq-fixture")
    assert result.status == "READY" and result.plan["collections"]
    assert result.source == "AUTO_PREREQUISITE" and result.created_at
    assert result.plan["settings"]["source"] == "AUTO_PREREQUISITE"


def test_prompt_generation_no_shopify_write_when_auto_creating_plan(tmp_path, monkeypatch):
    db = _store_db(tmp_path)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    result = CollectionPrerequisiteService(db).ensure_collection_plan("prereq-fixture")
    assert result.status == "READY"
    assert all(item.get("shopify_collection_id") is None for item in result.plan["collections"])


def test_prompt_generation_partial_without_collection_prerequisites(tmp_path):
    db = _store_db(tmp_path, category="", with_category=False)
    result = CollectionPrerequisiteService(db).ensure_collection_plan("prereq-fixture")
    assert result.status == "WAITING_FOR_INPUT"
    prepared = prepare_homepage_prerequisites("prereq-fixture", db=db,
        store={"store_name": "Cabin Tidy"}, brand_profile=_brand())
    assert prepared["homepage_plan"] and prepared["collection_plan"] is None
    partial = prepared["prompt_set"]
    kinds = {item["asset_type"] for item in partial["assets"]}
    assert {"HERO_BANNER", "HERO_MOBILE_CROP_GUIDE", "HERO_ALT_TEXT", "HEADER_SUPPORT",
            "ABOUT_BRAND", "CONTACT_SUPPORT", "BRAND_REUSE_GUIDANCE"} <= kinds
    assert partial["groups"]["collection"]["status"] == "WAITING_FOR_COLLECTION_PLAN"
    assert partial["groups"]["category_shortcut"]["status"] == "WAITING_FOR_COLLECTION_PLAN"


def test_hero_prompts_available_without_collection_plan():
    result = PromptAssetService().build(brand=_brand(), collection_plan=None)
    assert {"HERO_BANNER", "HERO_MOBILE_CROP_GUIDE", "HERO_ALT_TEXT"} <= {x["asset_type"] for x in result["assets"]}


def test_collection_prompts_added_after_plan_created(tmp_path):
    db = _store_db(tmp_path)
    result = CollectionPrerequisiteService(db).ensure_collection_plan("prereq-fixture")
    prompts = PromptAssetService().build(brand=_brand(), collection_plan=result.plan)
    assert prompts["summary"]["collections"] == len(result.plan["collections"])
    assert prompts["summary"]["categories"] == len(result.plan["collections"])
    assert prompts["groups"]["category_shortcut"]["status"] == "READY"


def test_homepage_design_auto_resolves_collection_plan(tmp_path):
    db = _store_db(tmp_path)
    prepared = prepare_homepage_prerequisites("prereq-fixture", db=db, store={"store_name": "Cabin Tidy"}, brand_profile=_brand())
    assert prepared["collection_plan"] and prepared["homepage_plan"]["collection_plan_id"] == prepared["collection_plan"]["plan_id"]
    assert prepared["statuses"] == {"brand_profile": "READY", "collection_plan": "READY", "homepage_plan": "READY", "prompt_set": "READY"}


def test_homepage_auto_complete_auto_resolves_collection_plan(tmp_path):
    db = _store_db(tmp_path)
    ensured = CollectionPrerequisiteService(db).ensure_collection_plan("prereq-fixture")
    homepage = build_homepage_plan(store_id="prereq-fixture", brand=_brand(), collection_plan=ensured.plan, db=db)
    assert homepage["collection_plan_id"] == ensured.plan["plan_id"]
    assert homepage["status"] == "DRAFT"


def test_auto_created_plan_idempotent(tmp_path):
    db = _store_db(tmp_path)
    service = CollectionPrerequisiteService(db)
    first = service.ensure_collection_plan("prereq-fixture")
    second = service.ensure_collection_plan("prereq-fixture")
    with connect(db) as con:
        count = con.execute("SELECT COUNT(*) FROM store_collection_plans WHERE store_id='prereq-fixture'").fetchone()[0]
    assert first.plan["plan_id"] == second.plan["plan_id"] and count == 1


def test_invalid_plan_not_silently_used(tmp_path):
    db = _store_db(tmp_path)
    original = CollectionPrerequisiteService(db).ensure_collection_plan("prereq-fixture").plan
    with connect(db) as con:
        con.execute("UPDATE store_collection_definitions SET title='' WHERE plan_id=?", (original["plan_id"],))
    repaired = CollectionPrerequisiteService(db).ensure_collection_plan("prereq-fixture")
    assert repaired.status == "READY" and repaired.plan["plan_id"] != original["plan_id"]
    assert all(item["title"] for item in repaired.plan["collections"])


def test_queue_ensure_collection_plan_resume(tmp_path):
    db = _store_db(tmp_path)
    prerequisite = CollectionPrerequisiteService(db)
    calls = {"ensure": 0}
    def ensure(_task):
        calls["ensure"] += 1
        result = prerequisite.ensure_collection_plan("prereq-fixture")
        return {"plan_id": result.plan["plan_id"]}
    queue = WorkflowAutomationService(tmp_path / "queue.sqlite3", handlers={"ENSURE_COLLECTION_PLAN": ensure})
    tasks = collection_prerequisite_workflow("prereq-fixture") + [{"task_key": "HUMAN_GATE", "requires_confirmation": True}]
    run_id = queue.create_run("prereq-fixture", "HOME", tasks)["run_id"]
    assert queue.run(run_id)["status"] == "WAITING_FOR_CONFIRMATION"
    assert calls["ensure"] == 1
    assert queue.confirm(run_id, "HUMAN_GATE")["status"] == "SUCCEEDED"
    assert calls["ensure"] == 1


def test_queue_ensure_waits_for_input_instead_of_failing(tmp_path):
    db = _store_db(tmp_path, category="", with_category=False)
    prerequisite = CollectionPrerequisiteService(db)
    def ensure(_task):
        result = prerequisite.ensure_collection_plan("prereq-fixture")
        if result.status != "READY":
            raise AutomationTaskError("BUSINESS_INPUT", result.reason)
        return {"plan_id": result.plan["plan_id"]}
    queue = WorkflowAutomationService(tmp_path / "waiting.sqlite3", handlers={"ENSURE_COLLECTION_PLAN": ensure})
    run_id = queue.create_run("prereq-fixture", "HOME", collection_prerequisite_workflow("prereq-fixture"))["run_id"]
    status = queue.run(run_id)
    assert status["status"] == "WAITING_FOR_INPUT"
    assert queue.tasks(run_id)["rows"][0]["status"] == "WAITING_FOR_INPUT"


def test_protected_store_file_untouched(tmp_path):
    path = __import__("pathlib").Path("stores/001_cabin_tidy.json")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    db = _store_db(tmp_path)
    CollectionPrerequisiteService(db).ensure_collection_plan("prereq-fixture")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_no_real_network_in_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected network")))
    db = _store_db(tmp_path)
    result = CollectionPrerequisiteService(db).ensure_collection_plan("prereq-fixture")
    assert result.status == "READY"
