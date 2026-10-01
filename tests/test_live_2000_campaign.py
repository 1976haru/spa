import json
from pathlib import Path

import pytest

from shopsource.capture.campaign import CampaignService, LIVE_2000_NAME
from shopsource.capture.service import CaptureService
from shopsource.db import connect, init_db, upsert_store, utc_now


def setup_campaign(tmp_path, target=3):
    db = tmp_path / "live.sqlite3"
    init_db(db)
    profile = json.loads((Path(__file__).parents[1] / "stores" / "001_cabin_tidy.json").read_text(encoding="utf-8"))
    upsert_store(profile, db)
    service = CampaignService(db)
    campaign = service.create_live_2000("001", target)
    service.action(campaign["campaign_id"], "START")
    return db, service, campaign["campaign_id"]


def products(start, count):
    return [{"asin": f"L{value:09}", "title": f"trunk organizer {value}",
             "url": f"https://www.amazon.com/dp/L{value:09}", "price": 45,
             "images": ["https://m.media-amazon.com/images/I/test.jpg"]}
            for value in range(start, start + count)]


def capture_page(db, service, campaign_id, items, next_url="https://www.amazon.com/s?k=trunk+organizer&page=2", keyword="trunk organizer"):
    result = CaptureService(db).capture_search({"store_id":"001", "keyword":keyword,
        "search_url":"https://www.amazon.com/s?k=trunk+organizer", "products":items})
    return service.record_search_capture(campaign_id, result["run_id"], next_url)


def test_live_2000_preset(tmp_path):
    db = tmp_path / "preset.sqlite3"; init_db(db)
    upsert_store({"store_id":"001", "store_name":"Cabin Tidy", "category":"auto"}, db)
    row = CampaignService(db).create_live_2000("001")
    assert (row["name"], row["candidate_target"], row["detail_target"], row["search_delay_seconds"]) == (LIVE_2000_NAME, 2000, 2000, 8)


def test_campaign_unique_candidate_target(tmp_path):
    db, service, cid = setup_campaign(tmp_path, 3)
    row = capture_page(db, service, cid, products(1, 3))
    assert row["unique_candidates"] == 3 and row["status"] == "DETAILING"


def test_campaign_duplicate_does_not_increment_unique(tmp_path):
    db, service, cid = setup_campaign(tmp_path, 3)
    capture_page(db, service, cid, products(1, 2))
    row = capture_page(db, service, cid, [*products(1, 1), *products(3, 1)])
    assert row["unique_candidates"] == 3 and row["duplicates"] == 1


def test_campaign_stops_search_at_2000(tmp_path):
    db, service, cid = setup_campaign(tmp_path, 2)
    row = capture_page(db, service, cid, products(1, 3))
    assert row["unique_candidates"] == 2 and service.next_search(cid) is None


def test_search_next_link_from_dom_contract():
    source = (Path(__file__).parents[1] / "browser_extension/shopsource_capture/content_search.js").read_text(encoding="utf-8")
    assert "s-pagination-next" in source and "node.href" in source and "page=" not in source.replace("page_number", "")


def test_search_no_next_marks_keyword_exhausted(tmp_path):
    db, service, cid = setup_campaign(tmp_path, 5)
    row = capture_page(db, service, cid, products(1, 1), next_url=None)
    assert row["keywords"][0]["exhausted"] == 1


def test_zero_new_pages_rotate_keyword(tmp_path):
    db, service, cid = setup_campaign(tmp_path, 5)
    capture_page(db, service, cid, products(1, 1))
    capture_page(db, service, cid, products(1, 1))
    row = capture_page(db, service, cid, products(1, 1))
    assert row["keywords"][0]["exhausted"] == 1
    assert row["search_instruction"]["keyword"] != "trunk organizer"


def test_campaign_resume_same_id_and_survives_ui_restart(tmp_path):
    db, service, cid = setup_campaign(tmp_path)
    service.action(cid, "PAUSE")
    assert CampaignService(db).create_live_2000("001")["campaign_id"] == cid
    assert CampaignService(db).action(cid, "RESUME")["campaign_id"] == cid


def test_campaign_detail_target_2000(tmp_path):
    db = tmp_path / "detail.sqlite3"; init_db(db)
    upsert_store({"store_id":"001", "store_name":"Cabin Tidy", "category":"auto"}, db)
    assert CampaignService(db).create_live_2000("001")["detail_target"] == 2000


def test_campaign_does_not_redetail_completed(tmp_path):
    db, service, cid = setup_campaign(tmp_path, 1)
    row = capture_page(db, service, cid, products(1, 1))
    batch_id = row["batch_run_id"]
    with connect(db) as con:
        con.execute("UPDATE browser_batch_items SET state='MASTER_IMPORTED' WHERE batch_run_id=?", (batch_id,))
    service._ensure_detail_batch(cid)
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM browser_batch_items WHERE batch_run_id=?", (batch_id,)).fetchone()[0] == 1


def test_spark_counts_recorded_separately_and_shopify_optional(tmp_path):
    _db, service, cid = setup_campaign(tmp_path)
    row = service.record_outcome(cid, spark_total=2000, spark_included=1800, spark_excluded=200)
    assert (row["spark_total"], row["spark_included"], row["spark_excluded"], row["shopify_uploaded_count"]) == (2000, 1800, 200, None)
    with pytest.raises(ValueError): service.record_outcome(cid, spark_total=10, spark_included=8, spark_excluded=1)


def test_assignment_report_counts(tmp_path, monkeypatch):
    _db, service, cid = setup_campaign(tmp_path)
    from shopsource.capture import campaign as module
    monkeypatch.setattr(module, "EXPORT_DIR", tmp_path / "exports")
    result = service.report(cid, "abc123")
    folder = Path(result["folder"])
    assert (folder / "assignment_summary.md").is_file()
    assert json.loads((folder / "assignment_summary.json").read_text(encoding="utf-8"))["commit"] == "abc123"
    assert (folder / "sourcing_counts.csv").is_file()


def test_campaign_package_uses_campaign_asins(tmp_path):
    db, service, cid = setup_campaign(tmp_path, 2)
    with connect(db) as con:
        now = utc_now()
        for index in (1, 2):
            asin = f"L{index:09}"
            payload = {"asin":asin, "title":f"Product {index}", "brand":"Fixture", "price":45, "images":[]}
            product_id = con.execute("INSERT INTO products(asin,title,price,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                (asin, payload["title"], 45, json.dumps(payload), now, now)).lastrowid
            con.execute("INSERT INTO store_product_decisions(store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at) VALUES(?,?,?,?,?,?,?)",
                ("001", product_id, "PRIMARY", "SAFE", "PRIMARY", "PRIMARY", now))
            con.execute("INSERT INTO sourcing_campaign_candidates(campaign_id,asin,capture_run_id,state,created_at,updated_at) VALUES(?,?,?,'MASTER_IMPORTED',?,?)",
                (cid, asin, f"BC_fixture_{index}", now, now))
    result = service.create_package(cid, out_root=tmp_path / "packages")
    files = sorted(Path(result["folder"]).glob("*.json"))
    assert result["product_count"] == 2
    assert {json.loads(path.read_text(encoding="utf-8"))["asin"] for path in files} == {"L000000001", "L000000002"}


def test_preflight_backup_created(tmp_path, monkeypatch):
    _db, service, cid = setup_campaign(tmp_path)
    from shopsource.capture import campaign as module
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    result = service.preflight(cid)
    assert result["database_backup_created"] and Path(result["database_backup"]).is_file()


def test_single_search_and_detail_worker_contract_and_no_captcha_bypass():
    root = Path(__file__).parents[1]
    background = (root / "browser_extension/shopsource_capture/background.js").read_text(encoding="utf-8")
    assert "searchWorkerTabs" in background and "chrome.tabs.update(prior" in background
    assert "workerTabs" in background and "chrome.tabs.update(worker.tabId" in background
    combined = background + (root / "browser_extension/shopsource_capture/content_search.js").read_text(encoding="utf-8")
    for forbidden in ("proxy rotation", "captcha bypass", "cookie extraction", "stealth plugin"):
        assert forbidden not in combined.lower()


def test_live_2000_does_not_render_all_products():
    source = (Path(__file__).parents[1] / "src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert "rows[:50]" in source
