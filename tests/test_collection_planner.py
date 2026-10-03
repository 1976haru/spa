import json
from pathlib import Path
import time

import pytest

from shopsource.collection_planner import CollectionPlanner, condition_matches
from shopsource.db import connect, init_db, upsert_store, utc_now


CATEGORIES = [
    ("trunk-storage", "Trunk Storage", ["trunk organizer", "trunk storage box"]),
    ("seat-organization", "Seat Organization", ["seat organizer", "backseat organizer"]),
    ("console-storage", "Console Storage", ["console organizer", "center console tray"]),
    ("cargo-travel", "Cargo & Travel", ["cargo organizer", "travel organizer"]),
    ("cup-holders", "Cup Holders", ["cup holder", "cup holder expander"]),
    ("trash-cleanup", "Trash & Cleanup", ["car trash can", "cleaning kit"]),
    ("visor-documents", "Visor & Documents", ["visor organizer", "document holder"]),
]
TITLES = [
    "Heavy Duty Trunk Organizer for SUV",
    "Backseat Organizer with Storage Pockets",
    "Center Console Organizer Tray",
    "Cargo Organizer for Travel",
    "Car Cup Holder Expander",
    "Car Trash Can and Cleanup Kit",
    "Sun Visor Document Holder",
]


def setup_collection_db(tmp_path, *, repeat=3, include_extra=False):
    db = tmp_path / "collections.sqlite3"
    init_db(db)
    profile = {
        "store_id": "fixture", "store_name": "Cabin Tidy", "category": "Automotive Organization",
        "concept": "practical car organization", "brand_voice": {"palette": "warm gray and teal", "style": "calm and useful"},
        "sourcing": {"target_candidates": 100, "recipes": []}, "include_keywords": [],
        "exclude_keywords": [], "risk_rules": [],
    }
    upsert_store(profile, db)
    now = utc_now()
    with connect(db) as con:
        con.execute("""INSERT INTO store_sourcing_plans
            (plan_id,store_id,version,name,total_candidate_target,detail_target,detail_ratio,mode,status,planner_version,settings_json,created_at,updated_at)
            VALUES('SAP_FIXTURE','fixture',1,'Fixture source plan',100,100,1,'balanced','DRAFT','3.0.0','{"max_active_keywords_per_category":15}',?,?)""", (now, now))
        for priority, (key, name, keywords) in enumerate(CATEGORIES, 1):
            cursor = con.execute("""INSERT INTO store_sourcing_categories
                (plan_id,category_key,category_name,weight,quota,priority,enabled,source,created_at,updated_at)
                VALUES('SAP_FIXTURE',?,?,1,?, ?,1,'FIXTURE',?,?)""", (key, name, 100 // len(CATEGORIES), priority, now, now))
            for keyword in keywords:
                con.execute("""INSERT INTO store_sourcing_keywords
                    (category_id,keyword,source,score,enabled,historical_yield,duplicate_rate,pages_used,created_at,updated_at)
                    VALUES(?,?, 'FIXTURE',.8,1,20,.1,0,?,?)""", (cursor.lastrowid, keyword, now, now))
        titles = TITLES * repeat
        if include_extra:
            titles += ["Trunk Seat Organizer Combination", "Generic Automotive Accessory"]
        for index, title in enumerate(titles):
            asin = f"C{index:09d}"
            con.execute("""INSERT INTO products
                (asin,source,source_kind,url,title,brand,price,currency,category,tags_json,overview_json,about_json,
                 images_json,options_json,raw_json,first_seen_at,last_seen_at,archived)
                VALUES(?,?,?,?,?,?,?,'USD','', '[]','[]','[]','[]','{}','{}',?,?,0)""",
                (asin, "fixture", "BROWSER_CAPTURE", f"https://example.invalid/{asin}", title, "Fixture Brand", 25.0, now, now))
            if index % len(TITLES) == 0:
                product_id = con.execute("SELECT id FROM products WHERE asin=?", (asin,)).fetchone()[0]
                con.execute("""INSERT INTO store_product_decisions
                    (store_id,product_id,fit_score,price_status,risk_status,auto_status,final_status,reasons_json,manual_override,memo,classified_at)
                    VALUES('fixture',?,.9,'PRIMARY','OK','PRIMARY','PRIMARY','[]',0,'',?)""", (product_id, now))
    return db


def test_collection_plan_from_sourcing_plan(tmp_path):
    plan = CollectionPlanner(setup_collection_db(tmp_path)).create_plan("fixture")
    assert plan["sourcing_plan_id"] == "SAP_FIXTURE"
    assert plan["collection_count"] >= 4
    assert all(row["source_category_id"] for row in plan["collections"])


def test_collection_title_description_generated(tmp_path):
    plan = CollectionPlanner(setup_collection_db(tmp_path)).create_plan("fixture")
    row = next(item for item in plan["collections"] if item["collection_key"] == "trunk-storage")
    assert row["title"] == "Trunk Organizers"
    assert "<p>" in row["description_html"] and "Cabin Tidy" in row["description_html"]
    assert row["handle"] == "cabin-tidy-trunk-organizers"
    console = next(item for item in plan["collections"] if item["collection_key"] == "console-storage")
    assert console["title"] == "Console Storage"
    seat = next(item for item in plan["collections"] if item["collection_key"] == "seat-organization")
    assert any(rule["value"].casefold() == "seat organizer" for rule in seat["conditions"])
    assert any(rule["value"].casefold() == "backseat organizer" for rule in seat["conditions"])


def test_manual_trunk_rule_supported(tmp_path):
    db = setup_collection_db(tmp_path)
    plan = CollectionPlanner(db).create_plan("fixture")
    trunk = next(item for item in plan["collections"] if item["collection_key"] == "trunk-storage")
    assert any(rule["field"] == "TITLE" and rule["relation"] == "CONTAINS" and rule["value"].casefold() == "trunk" for rule in trunk["conditions"])
    assert condition_matches({"title": "Premium Trunk Organizer", "category": "", "brand": "", "tags": [], "price": 20, "metafields": {}},
                             {"field": "TITLE", "relation": "CONTAINS", "value": "Trunk"})


def test_local_condition_simulator_supports_canonical_fields_and_relations():
    product = {"title": "Trunk Organizer Pro", "category": "Automotive Storage", "brand": "Acme",
               "tags": ["shopsource-fixture-trunk"], "price": 25.0, "metafields": {"fit": "SUV cargo"}}
    cases = [
        ("TITLE", "STARTS_WITH", "Trunk", True), ("TITLE", "ENDS_WITH", "Pro", True),
        ("TITLE", "NOT_CONTAINS", "Cup", True), ("PRODUCT_TYPE", "CONTAINS", "storage", True),
        ("TAG", "EQUALS", "shopsource-fixture-trunk", True), ("TAG", "NOT_EQUALS", "other", True),
        ("VENDOR", "EQUALS", "Acme", True), ("PRICE", "GREATER_THAN", "20", True),
        ("PRICE", "LESS_THAN", "30", True), ("PRICE", "NOT_EQUALS", "30", True),
        ("METAFIELD", "CONTAINS", "SUV", True),
    ]
    for field, relation, value, expected in cases:
        assert condition_matches(product, {"field": field, "relation": relation, "value": value}) is expected


def test_generic_car_rule_rejected(tmp_path):
    db = tmp_path / "generic.sqlite3"
    init_db(db)
    upsert_store({"store_id": "generic", "store_name": "Generic", "category": "Car", "concept": "Car products", "sourcing": {"recipes": []}, "include_keywords": [], "exclude_keywords": [], "risk_rules": []}, db)
    now = utc_now()
    with connect(db) as con:
        con.execute("""INSERT INTO store_sourcing_plans(plan_id,store_id,version,name,total_candidate_target,detail_target,detail_ratio,mode,status,planner_version,settings_json,created_at,updated_at)
            VALUES('SAP_G','generic',1,'generic',10,10,1,'balanced','DRAFT','3.0.0','{"max_active_keywords_per_category":15}',?,?)""", (now, now))
        category_id = con.execute("""INSERT INTO store_sourcing_categories(plan_id,category_key,category_name,weight,quota,priority,enabled,source,created_at,updated_at)
            VALUES('SAP_G','car-items','Car Products',1,10,1,1,'FIXTURE',?,?)""", (now, now)).lastrowid
        con.execute("INSERT INTO store_sourcing_keywords(category_id,keyword,source,score,enabled,created_at,updated_at) VALUES(?,?,?,.8,1,?,?)", (category_id, "car accessories", "FIXTURE", now, now))
    plan = CollectionPlanner(db).create_plan("generic", settings={"include_empty": True})
    assert not any(rule["value"].casefold() == "car" for row in plan["collections"] for rule in row["conditions"])
    assert plan["warnings"] or not plan["collections"]


def test_collection_local_match_count(tmp_path):
    plan = CollectionPlanner(setup_collection_db(tmp_path)).create_plan("fixture")
    trunk = next(row for row in plan["collections"] if row["collection_key"] == "trunk-storage")
    assert trunk["estimated_product_count"] == 3
    assert len(trunk["sample_products"]) == 3


def test_collection_overlap_warning(tmp_path):
    plan = CollectionPlanner(setup_collection_db(tmp_path, include_extra=True)).create_plan("fixture", settings={"min_products": 1, "max_overlap_warning": .2})
    assert any(any(warning.startswith("EXTREME_OVERLAP") for warning in row["warnings"]) for row in plan["collections"])


def test_collection_broad_coverage_warning(tmp_path):
    db = setup_collection_db(tmp_path)
    with connect(db) as con:
        con.execute("UPDATE products SET title='Trunk organizer for everyday car storage'")
    plan = CollectionPlanner(db).create_plan("fixture", settings={"include_empty": True, "min_products": 0})
    trunk = next(row for row in plan["collections"] if row["collection_key"] == "trunk-storage")
    assert "BROAD_COLLECTION_OVER_70_PERCENT" in trunk["warnings"]
    assert plan["unmatched_percentage"] == 0


def test_collection_zero_match_warning(tmp_path):
    db = setup_collection_db(tmp_path)
    with connect(db) as con:
        con.execute("UPDATE store_sourcing_keywords SET keyword='unicorn widget' WHERE keyword='document holder'")
    plan = CollectionPlanner(db).create_plan("fixture")
    assert any(row.get("reason") == "ZERO_MATCHES" for row in plan["warnings"])


def test_collection_image_prompt_generated(tmp_path):
    plan = CollectionPlanner(setup_collection_db(tmp_path)).create_plan("fixture")
    for row in plan["collections"]:
        assert "Photorealistic ecommerce lifestyle" in row["image_prompt"]
        assert "no logo" in row["image_prompt"].casefold()
        assert row["image_alt_text"]


def test_tag_preference_keeps_title_fallback_and_local_estimates(tmp_path):
    plan = CollectionPlanner(setup_collection_db(tmp_path)).create_plan("fixture", settings={"rule_strategy": "TAG_PREFERRED"})
    trunk = next(row for row in plan["collections"] if row["collection_key"] == "trunk-storage")
    assert trunk["rule_strategy"] == "MIXED"
    assert any(rule["field"] == "TAG" and rule["value"].startswith("shopsource-") for rule in trunk["conditions"])
    assert any(rule["field"] == "TITLE" for rule in trunk["conditions"])


def test_collection_plan_versioned(tmp_path):
    planner = CollectionPlanner(setup_collection_db(tmp_path))
    first = planner.create_plan("fixture")
    second = planner.create_plan("fixture")
    assert (first["version"], second["version"]) == (1, 2)
    assert second["diff"]["unchanged"]
    assert all(row["shopify_sync_status"] == "NOT_SYNCED" for row in second["collections"])
    reloaded = planner.get_plan(second["plan_id"])
    assert reloaded["collection_count"] == second["collection_count"]
    assert reloaded["master_product_count"] == second["master_product_count"]


def test_collection_export_json(tmp_path):
    planner = CollectionPlanner(setup_collection_db(tmp_path))
    plan = planner.create_plan("fixture")
    result = planner.export(plan["plan_id"], tmp_path / "exports")
    payload = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
    assert payload["collections"] and payload["plan_id"] == plan["plan_id"]


def test_collection_export_md(tmp_path):
    planner = CollectionPlanner(setup_collection_db(tmp_path))
    plan = planner.create_plan("fixture")
    result = planner.export(plan["plan_id"], tmp_path / "exports")
    text = Path(result["markdown"]).read_text(encoding="utf-8")
    assert "# Collection plan" in text and "Image prompt:" in text and "TITLE CONTAINS" in text


def test_no_shopify_write_in_planner():
    source = (Path(__file__).parents[1] / "src/shopsource/collection_planner.py").read_text(encoding="utf-8")
    assert "requests." not in source and "httpx." not in source
    assert '"shopify_collection_id": None' in source


def test_no_amazon_network_in_planner():
    source = (Path(__file__).parents[1] / "src/shopsource/collection_planner.py").read_text(encoding="utf-8")
    assert "amazon.com" not in source and "requests.get" not in source


def test_collection_planner_ui_is_local_and_collection_actions_present():
    source = (Path(__file__).parents[1] / "src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert '"/collections"' in source and '"컬렉션 자동화"' in source
    assert 'ui.button("컬렉션 자동 설계"' in source
    assert 'ui.button("Shopify에 컬렉션 생성 (Phase 3.3)", on_click=None).props("disable outline")' in source


def test_1000_products_preview_fast(tmp_path):
    db = setup_collection_db(tmp_path, repeat=143)
    start = time.perf_counter()
    plan = CollectionPlanner(db).create_plan("fixture")
    elapsed = time.perf_counter() - start
    assert plan["master_product_count"] == 1001
    assert elapsed < 3.0
