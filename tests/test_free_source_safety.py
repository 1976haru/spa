import json
from datetime import datetime, timezone

import pytest

from shopsource.db import connect, init_db
from shopsource.production_runner import ProductionEvidenceRunner
from shopsource.source_safety import FreeSourceSafetyService, free_capture_observation


def _db_with_products(path, count=12):
    init_db(path)
    now = "2026-10-05T00:00:00+00:00"
    with connect(path) as con:
        for i in range(1, count + 1):
            status = "PRIMARY" if i <= count - 3 else ("REVIEW" if i == count - 2 else "RESTRICTED" if i == count - 1 else "RESERVE_A")
            raw = {"shopify_selling_price": 19.99, "quantity": 1}
            con.execute("INSERT INTO products(asin,title,price,source_url,source_kind,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?)",
                (f"B{i:09d}", f"Organizer {i}", 8.5, f"https://www.amazon.com/dp/B{i:09d}", "BROWSER_CAPTURE", json.dumps(raw), now, now))
            con.execute("INSERT INTO store_product_decisions(store_id,product_id,fit_score,price_status,risk_status,auto_status,final_status,classified_at) VALUES('001',?,50,'OK','CLEAR',?,?,?)",
                (i, status, status, now))
    return path


def test_capture_availability_requires_explicit_signal_and_price_is_separate():
    assert free_capture_observation({"quantity": 3})["availability"] == "UNKNOWN"
    observed = free_capture_observation({"availability": "https://schema.org/InStock", "price": "12.50"})
    assert observed["availability"] == "IN_STOCK"
    assert observed["source_price"] == 12.5
    assert free_capture_observation({"availability": "OutOfStock"})["price_status"] == "MISSING_PRICE"
    captured = free_capture_observation({"sourceAvailability":"IN_STOCK", "availabilityConfidence":"HIGH",
        "availabilityEvidence":{"kind":"JSON_LD_OFFER_AVAILABILITY","value":"https://schema.org/InStock"},
        "price":12, "_collectedAt":"2026-10-05T12:00:00+00:00"})
    assert captured["availability"] == "IN_STOCK" and captured["qualifies_for_sellability"] is True
    challenge = free_capture_observation({"visible_text":"Robot Check - CAPTCHA"})
    assert challenge["availability"] == "SOURCE_ERROR"
    assert challenge["evidence_kind"] == "HUMAN_ACTION_REQUIRED"


def test_free_default_does_not_require_keepa_and_selects_primary_only(tmp_path, monkeypatch):
    monkeypatch.delenv("KEEPA_API_KEY", raising=False)
    db = _db_with_products(tmp_path / "free.sqlite3", 12)
    free = FreeSourceSafetyService(db)
    preview = free.preview_batch("001", "DRAFT_PILOT")
    assert preview["provider"] == "FREE_LOCAL_SOURCE_CHECK"
    assert preview["master_total"] == 12
    assert preview["source_check_eligible"] == 9
    assert preview["eligible_upload_candidates"] == 0
    assert preview["target_count"] == 9
    assert preview["estimated_tokens"] == 0
    runner = ProductionEvidenceRunner(db=db)
    run = runner.start_or_resume("001")
    evidence = runner._source("001")
    assert evidence["status"] == "WAITING_FOR_INPUT"
    assert evidence["provider"] == "FREE_LOCAL_SOURCE_CHECK"
    assert run["run_id"]


def test_local_import_quantity_never_becomes_stock_and_missing_price_needs_review(tmp_path):
    db = _db_with_products(tmp_path / "evidence.sqlite3", 5)
    with connect(db) as con:
        con.execute("UPDATE products SET price=NULL WHERE id=1")
        con.execute("UPDATE products SET raw_json=? WHERE id=2", (json.dumps({"shopify_selling_price":19.99,"quantity":99}),))
    result = FreeSourceSafetyService(db).inspect_local_batch("001", "DRAFT_PILOT")
    by_id = {item["product_id"]: item for item in result["items"]}
    assert by_id[1]["availability"] == "UNKNOWN"
    assert by_id[1]["price_status"] == "MISSING_PRICE"
    assert by_id[2]["availability"] == "UNKNOWN"
    assert result["status"] == "WAITING_FOR_INPUT"


def test_validation_batch_is_explicit_primary_only_and_limited_to_200(tmp_path):
    db = _db_with_products(tmp_path / "batch.sqlite3", 205)
    free = FreeSourceSafetyService(db)
    primary_ids = [int(row["master_product_id"]) for row in free.primary_candidates("001")]
    assert len(primary_ids) == 202
    now = datetime.now(timezone.utc).isoformat()
    with connect(db) as con:
        con.execute("INSERT INTO source_safety_release_batches(batch_id,store_id,batch_kind,status,target_count,checked_count,created_at,updated_at) VALUES('pilot-pass','001','SOURCE_CHECK_PILOT','VERIFIED',10,10,?,?)", (now, now))
        con.executemany("INSERT INTO source_safety_release_items(batch_id,product_id,status) VALUES('pilot-pass',?,'VERIFIED')", [(pid,) for pid in primary_ids[:10]])
    preview = free.preview_batch("001", "VALIDATION_BATCH", selected_product_ids=primary_ids[10:160])
    assert preview["target_count"] == 150
    with pytest.raises(ValueError, match="100-200"):
        free.preview_batch("001", "VALIDATION_BATCH", selected_product_ids=primary_ids[10:109])
    with pytest.raises(ValueError, match="100-200"):
        free.preview_batch("001", "VALIDATION_BATCH", selected_product_ids=list(range(10000, 10201)))


def test_draft_free_check_stays_waiting_without_fresh_browser_capture(tmp_path):
    db = _db_with_products(tmp_path / "run.sqlite3", 5)
    runner = ProductionEvidenceRunner(db=db)
    run = runner.start_or_resume("001")
    result = runner.run_free_source_safety_check(run["run_id"], confirmed=True)
    assert result["status"] == "WAITING_FOR_INPUT"
    assert result["production_run"]["gates"][2]["status"] != "VERIFIED"
    with connect(db) as con:
        batch = con.execute("SELECT status FROM source_safety_release_batches").fetchone()
        assert batch["status"] == "WAITING_FOR_INPUT"


def test_free_check_reuses_existing_user_operated_browser_capture_queue(tmp_path):
    db = _db_with_products(tmp_path / "browser-queue.sqlite3", 5)
    with connect(db) as con:
        con.execute("INSERT INTO stores(store_id,store_name,category,profile_json,created_at,updated_at) VALUES('001','Cabin Tidy','Car Organization','{}','2026-10-05','2026-10-05')")
    runner = ProductionEvidenceRunner(db=db)
    run = runner.start_or_resume("001")
    result = runner.prepare_free_browser_capture_batch(run["run_id"], confirmed=True)
    assert result["provider"] == "FREE_LOCAL_SOURCE_CHECK"
    assert result["queued"] == result["target_count"] == 2
    with connect(db) as con:
        batch = con.execute("SELECT auto_import_master FROM browser_batch_runs WHERE run_id=?",
                            (result["browser_batch_run_id"],)).fetchone()
        assert batch["auto_import_master"] == 0
        assert con.execute("SELECT COUNT(*) FROM source_product_snapshots").fetchone()[0] == 0
    pending = runner.apply_free_browser_capture_results(run["run_id"], result["release_batch_id"], confirmed=True)
    assert pending["status"] == "WAITING_FOR_INPUT" and pending["missing_capture_count"] == 2


def test_free_observation_requires_fresh_explicit_price_and_availability(tmp_path):
    db = _db_with_products(tmp_path / "observed.sqlite3", 4)
    runner = ProductionEvidenceRunner(db=db)
    run = runner.start_or_resume("001")
    ids = runner.free_source_safety_preflight(run["run_id"])["target_product_ids"]
    now = datetime.now(timezone.utc).isoformat()
    observations = {product_id: {"availability": "IN_STOCK", "availability_confidence": "HIGH",
        "source_price": 9.25, "source_currency": "USD", "evidence_kind": "VISIBLE_AVAILABILITY_TEXT",
        "qualifies_for_sellability": True, "evidence": {"normalized": "in stock"}, "observed_at": now} for product_id in ids}
    result = runner.run_free_source_safety_check(run["run_id"], observations=observations, confirmed=True)
    assert result["status"] == "COMPLETE"
    assert result["production_run"]["gates"][2]["evidence"]["provider"] == "FREE_LOCAL_SOURCE_CHECK"


def test_validation_limits_and_captcha_human_gate(tmp_path):
    db = _db_with_products(tmp_path / "captcha.sqlite3", 105)
    runner = ProductionEvidenceRunner(db=db)
    run = runner.start_or_resume("001")
    ids = FreeSourceSafetyService(db).preview_batch("001", "SOURCE_CHECK_PILOT")["target_product_ids"]
    obs = {product_id: {"availability":"UNKNOWN", "source_price": 10, "evidence":{"page_text":"Robot Check"}}
           for product_id in ids}
    result = runner.run_free_source_safety_check(run["run_id"], "SOURCE_CHECK_PILOT",
        observations=obs, confirmed=True)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["production_run"]["gates"][2]["status"] != "VERIFIED"
    with connect(db) as con:
        item = con.execute("SELECT availability,reason FROM source_safety_release_items WHERE batch_id=? LIMIT 1",
                           (result["batch_id"],)).fetchone()
    assert item["availability"] == "SOURCE_ERROR"
    assert "HUMAN_ACTION_REQUIRED" in item["reason"]
