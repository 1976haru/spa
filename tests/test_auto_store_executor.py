import json
from pathlib import Path
import pytest

from shopsource.capture.campaign import CampaignService
from shopsource.capture.service import CaptureService
from shopsource.db import connect, init_db, upsert_store
from shopsource.sourcing.planner import CategoryPlanner


def setup_auto(tmp_path, target=30):
    db = tmp_path / "auto.sqlite3"
    init_db(db)
    profile = json.loads((Path(__file__).parents[1] / "stores" / "001_cabin_tidy.json").read_text(encoding="utf-8"))
    profile["store_id"] = "fixture"
    upsert_store(profile, db)
    plan = CategoryPlanner(db).create_plan("fixture", target, advanced={
        "max_active_keywords_per_category": 3, "max_pages_per_keyword": 1,
        "stale_pages": 2, "max_unique_candidates_per_keyword": 12,
    })
    return db, plan


def test_auto_store_campaign_is_persisted_and_start_is_explicit(tmp_path):
    db, plan = setup_auto(tmp_path)
    service = CampaignService(db)
    campaign = service.create_auto_store(plan["plan_id"])
    assert campaign["campaign_type"] == "AUTO_STORE"
    assert campaign["status"] == "DRAFT"
    assert campaign["campaign_id"].startswith("AC_")
    assert service.create_auto_store(plan["plan_id"])["campaign_id"] == campaign["campaign_id"]
    assert service.active("fixture", "LIVE_2000") is None
    assert service.active("fixture", "AUTO_STORE")["campaign_id"] == campaign["campaign_id"]
    assert service.action(campaign["campaign_id"], "START")["status"] == "RUNNING_SEARCH"


def test_auto_store_search_worker_contract_and_caps_are_durable(tmp_path):
    db, plan = setup_auto(tmp_path)
    service = CampaignService(db)
    campaign = service.create_auto_store(plan["plan_id"])
    campaign = service.action(campaign["campaign_id"], "START")
    instruction = service.next_search(campaign["campaign_id"])
    keyword = instruction["keyword"]
    result = CaptureService(db).capture_search({
        "store_id": "fixture", "keyword": keyword,
        "search_url": f"https://www.amazon.com/s?k={keyword.replace(' ', '+')}&page=1",
        "page_number": 1,
        "products": [{"asin": f"T{index:09}", "title": f"fixture item {index}", "url": f"https://www.amazon.com/dp/T{index:09}"}
                     for index in range(1, 20)],
    })
    updated = service.record_search_capture(campaign["campaign_id"], result["run_id"], "https://www.amazon.com/s?k=test&page=2")
    assert updated["unique_candidates"] <= 12
    chosen = next(row for row in updated["keywords"] if row["keyword"] == keyword)
    assert chosen["exhausted"] == 1 and chosen["exhaustion_reason"] in {"MAX_PAGES", "MAX_UNIQUE"}
    assert updated["campaign_type"] == "AUTO_STORE"
    assert updated["search_worker_status"] == "CONNECTED"
    with connect(db) as con:
        assert con.execute("SELECT COUNT(DISTINCT asin) FROM sourcing_campaign_candidates WHERE campaign_id=?", (campaign["campaign_id"],)).fetchone()[0] == updated["unique_candidates"]


def test_auto_store_extension_accepts_ac_campaign_and_explicit_start_only():
    root = Path(__file__).parents[1] / "browser_extension" / "shopsource_capture"
    bridge = (root / "content_local_bridge.js").read_text(encoding="utf-8")
    search = (root / "content_search.js").read_text(encoding="utf-8")
    background = (root / "background.js").read_text(encoding="utf-8")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert "(?:LC|AC)_" in bridge
    assert "RUNNING_SEARCH" in background
    assert "shopsource-campaign-capture" in search
    assert manifest["version"] == "0.1.7"


@pytest.mark.parametrize("target", [2000, 10000, 50000])
def test_auto_store_plan_and_campaign_scale_without_live_network(tmp_path, target):
    db = tmp_path / f"scale-{target}.sqlite3"
    init_db(db)
    profile = json.loads((Path(__file__).parents[1] / "stores" / "001_cabin_tidy.json").read_text(encoding="utf-8"))
    profile["store_id"] = "fixture"
    upsert_store(profile, db)
    plan = CategoryPlanner(db).create_plan("fixture", target)
    campaign = CampaignService(db).create_auto_store(plan["plan_id"])
    quotas = {row["category_id"]: row["category_quota"] for row in campaign["keywords"]}
    assert sum(quotas.values()) == target
    assert campaign["candidate_target"] == target
    assert campaign["unique_candidates"] == 0
