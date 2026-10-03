from shopsource.capture.campaign import CampaignService
from shopsource.db import connect, init_db, upsert_store
from shopsource.sourcing.planner import CategoryPlanner


def profile(**overrides):
    value = {"store_id": "test", "store_name": "Cabin Tidy", "category": "auto interior",
             "concept": "car organization", "include_keywords": ["trunk organizer", "seat storage"],
             "exclude_keywords": ["motorcycle accessory"],
             "risk_rules": [{"terms": ["battery"]}], "sourcing": {"recipes": []}}
    value.update(overrides)
    return value


def setup(tmp_path, store=None):
    db = tmp_path / "planner.sqlite3"
    init_db(db)
    upsert_store(store or profile(), db)
    return db


def test_auto_plan_created_from_store_profile(tmp_path):
    db = setup(tmp_path)
    plan = CategoryPlanner(db).create_plan("test", 1000)
    assert plan["status"] == "DRAFT"
    assert plan["planner_version"] == "3.0.0"
    assert plan["categories"]
    assert all(category["keywords"] for category in plan["categories"])


def test_auto_plan_requires_no_manual_keyword_input(tmp_path):
    plan = CategoryPlanner(setup(tmp_path)).create_plan("test", 100)
    assert plan["keyword_pool_total"] > 0


def test_category_quotas_sum_to_total_target(tmp_path):
    plan = CategoryPlanner(setup(tmp_path)).create_plan("test", 10003)
    assert plan["total_quota"] == 10003


def test_keyword_pool_dedupes_case_insensitive(tmp_path):
    store = profile(sourcing={"recipes": ["Trunk Organizer", "trunk organizer", "Seat storage"]})
    plan = CategoryPlanner(setup(tmp_path, store)).create_plan("test", 100)
    for category in plan["categories"]:
        normalized = [keyword["keyword"].casefold() for keyword in category["keywords"]]
        assert len(normalized) == len(set(normalized))


def test_active_keyword_cap(tmp_path):
    plan = CategoryPlanner(setup(tmp_path)).create_plan("test", 1000)
    cap = plan["settings"]["max_active_keywords_per_category"]
    assert cap == 15
    assert all(sum(keyword["active_by_default"] for keyword in c["keywords"]) <= cap for c in plan["categories"])


def test_planning_modes_change_default_depth(tmp_path):
    planner = CategoryPlanner(setup(tmp_path))
    fast = planner.create_plan("test", 500, mode="fast")
    deep = planner.create_plan("test", 500, mode="deep")
    assert fast["settings"]["max_active_keywords_per_category"] < deep["settings"]["max_active_keywords_per_category"]
    assert fast["settings"]["max_pages_per_keyword"] < deep["settings"]["max_pages_per_keyword"]


def test_keyword_page_cap_defaults(tmp_path):
    plan = CategoryPlanner(setup(tmp_path)).create_plan("test", 10)
    assert plan["settings"]["max_pages_per_keyword"] == 5


def test_stale_page_default(tmp_path):
    plan = CategoryPlanner(setup(tmp_path)).create_plan("test", 10)
    assert plan["settings"]["stale_pages"] == 2


def test_keyword_score_uses_historical_yield_when_available(tmp_path):
    db = setup(tmp_path)
    with connect(db) as con:
        con.execute("INSERT INTO keyword_validation_results(store_id,keyword,checked_at,candidate_yield,price_fit,quality_fit,risk_rate,master_duplicate_rate) VALUES(?,?,?,?,?,?,?,?)",
                    ("test", "trunk organizer", "2026-01-01T00:00:00+00:00", 200, .9, .95, .01, .02))
    plan = CategoryPlanner(db).create_plan("test", 1000)
    keyword = next(k for c in plan["categories"] for k in c["keywords"] if k["keyword"].casefold() == "trunk organizer")
    assert keyword["historical_yield"] == 200
    assert keyword["score"] > 0.5


def test_low_score_keyword_preserved_not_deleted(tmp_path):
    db = setup(tmp_path, profile(include_keywords=["battery only"], exclude_keywords=["battery only"], risk_rules=[]))
    plan = CategoryPlanner(db).create_plan("test", 30)
    matches = [k for c in plan["categories"] for k in c["keywords"] if k["keyword"] == "battery only"]
    assert matches and not matches[0]["enabled"]


def test_plan_version_snapshot(tmp_path):
    db = setup(tmp_path)
    planner = CategoryPlanner(db)
    first = planner.create_plan("test", 100)
    second = planner.create_plan("test", 200)
    assert (first["version"], second["version"]) == (1, 2)
    assert first["total_candidate_target"] == 100
    assert second["total_candidate_target"] == 200


def test_plan_supports_1000_keywords(tmp_path):
    categories = [f"Category {index}" for index in range(10)]
    db = setup(tmp_path, profile(sourcing_categories=categories))
    plan = CategoryPlanner(db).create_plan("test", 50000)
    assert len(plan["categories"]) == 10
    assert plan["keyword_pool_total"] >= 1000


def test_plan_supports_50000_target(tmp_path):
    plan = CategoryPlanner(setup(tmp_path)).create_plan("test", 50000)
    assert plan["total_quota"] == 50000


def test_detail_ratio_default_100_percent(tmp_path):
    plan = CategoryPlanner(setup(tmp_path)).create_plan("test", 501)
    assert plan["detail_target"] == 501


def test_detail_ratio_advanced(tmp_path):
    plan = CategoryPlanner(setup(tmp_path)).create_plan("test", 500, detail_ratio=.25)
    assert plan["detail_target"] == 125


def test_detailing_ui_marks_search_complete(tmp_path):
    db = setup(tmp_path)
    service = CampaignService(db)
    with connect(db) as con:
        con.execute("""INSERT INTO sourcing_campaigns(campaign_id,store_id,name,candidate_target,detail_target,status,unique_candidates,created_at,updated_at)
            VALUES('LC_TEST','test','synthetic',2000,2000,'DETAILING',2000,'now','now')""")
    assert service.get("LC_TEST")["search_stage_complete"] is True


def test_detailing_ui_shows_detail_worker_not_search_error():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert 'if campaign.get("search_stage_complete")' in source
    assert "상세 Worker:" in source
    assert "not campaign.get(\"search_stage_complete\") and campaign.get(\"last_search_error\")" in source


def test_current_live_campaign_not_mutated_by_planner(tmp_path):
    db = setup(tmp_path)
    with connect(db) as con:
        con.execute("""INSERT INTO sourcing_campaigns(campaign_id,store_id,name,candidate_target,detail_target,status,unique_candidates,created_at,updated_at)
            VALUES('LC_aab4075226b7c41c933f','test','synthetic live',2000,2000,'DETAILING',2000,'before','before')""")
        before = dict(con.execute("SELECT * FROM sourcing_campaigns WHERE campaign_id='LC_aab4075226b7c41c933f'").fetchone())
    CategoryPlanner(db).create_plan("test", 1000)
    with connect(db) as con:
        after = dict(con.execute("SELECT * FROM sourcing_campaigns WHERE campaign_id='LC_aab4075226b7c41c933f'").fetchone())
    assert after == before
