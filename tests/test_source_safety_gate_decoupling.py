import json
from datetime import datetime, timezone

from shopsource.db import connect, init_db
from shopsource.production_runner import ProductionEvidenceRunner
from shopsource.source_safety import FreeSourceSafetyService


def _catalog(path, count=12):
    init_db(path)
    now = datetime.now(timezone.utc).isoformat()
    statuses = ["PRIMARY"] * count
    if count >= 4:
        statuses[-3:] = ["REVIEW_REQUIRED", "RESTRICTED", "REJECT_FOR_STORE"]
    with connect(path) as con:
        for i, status in enumerate(statuses, 1):
            asin = f"B{i:09d}"
            url = f"https://www.amazon.com/dp/{asin}"
            # Deliberately omit retail price, description, media, and collection data.
            con.execute("""INSERT INTO products(asin,title,source_url,source_kind,raw_json,first_seen_at,last_seen_at)
                VALUES(?,?,?,'BROWSER_CAPTURE','{}',?,?)""", (asin, "", url, now, now))
            con.execute("""INSERT INTO store_product_decisions(store_id,product_id,fit_score,price_status,risk_status,
                auto_status,final_status,classified_at) VALUES('001',?,0,'UNKNOWN','CLEAR',?,?,?)""",
                (i, status, status, now))
    return path


def test_source_check_selector_is_decoupled_from_draft_requirements(tmp_path):
    db = _catalog(tmp_path / "decoupled.sqlite3", 8)
    free = FreeSourceSafetyService(db)
    eligible, excluded = free.source_check_candidates("001")
    assert len(eligible) == 5
    assert all(row["candidate_set"] == "SOURCE_CHECK_ELIGIBLE" for row in eligible)
    assert all(not row.get("selling_price") and not row.get("description_html") for row in eligible)
    assert excluded["REVIEW_REQUIRED"] == excluded["RESTRICTED"] == excluded["REJECT_FOR_STORE"] == 1
    assert free.draft_upload_candidates("001") == []


def test_missing_source_identity_is_excluded_with_reason(tmp_path):
    db = _catalog(tmp_path / "missing.sqlite3", 3)
    with connect(db) as con:
        con.execute("UPDATE products SET asin='' WHERE id=1")
        con.execute("UPDATE products SET source_url='' WHERE id=2")
    rows, excluded = FreeSourceSafetyService(db).source_check_candidates("001")
    assert len(rows) == 1
    assert excluded["MISSING_ASIN"] == 1
    assert excluded["MISSING_SOURCE_URL"] == 1


def test_source_check_pilot_is_max_ten_and_never_shopify_upload(tmp_path, monkeypatch):
    db = _catalog(tmp_path / "pilot.sqlite3", 15)
    runner = ProductionEvidenceRunner(db=db)
    run = runner.start_or_resume("001")
    preview = runner.free_source_safety_preflight(run["run_id"], "SOURCE_CHECK_PILOT")
    assert preview["target_count"] == 10
    calls = []
    monkeypatch.setattr("shopsource.shopify_products.DirectShopifyProductPublisher.sync", lambda *a, **k: calls.append("sync"))
    result = runner.run_free_source_safety_check(run["run_id"], "SOURCE_CHECK_PILOT", confirmed=True)
    assert result["status"] == "WAITING_FOR_INPUT"
    assert calls == []
    assert result["production_run"]["gates"][2]["status"] != "VERIFIED"


def test_explicit_source_pass_verifies_g2_without_content_or_selling_price(tmp_path):
    db = _catalog(tmp_path / "g2pass.sqlite3", 3)
    runner = ProductionEvidenceRunner(db=db)
    run = runner.start_or_resume("001")
    ids = runner.free_source_safety_preflight(run["run_id"])["target_product_ids"]
    now = datetime.now(timezone.utc).isoformat()
    observations = {pid: {"availability": "IN_STOCK", "availability_confidence": "HIGH",
        "source_price": 9.25, "source_currency": "USD", "evidence_kind": "VISIBLE_AVAILABILITY_TEXT",
        "qualifies_for_sellability": True, "evidence": {"normalized": "in stock"}, "observed_at": now}
        for pid in ids}
    result = runner.run_free_source_safety_check(run["run_id"], observations=observations, confirmed=True)
    g2 = next(g for g in result["production_run"]["gates"] if g["gate_key"] == "SOURCE_SAFETY")
    assert g2["status"] == "VERIFIED"
    assert result["production_run"]["gates"][3]["gate_key"] == "PRODUCT_CONTENT"
    assert FreeSourceSafetyService(db).draft_upload_candidates("001") == []
    assert FreeSourceSafetyService(db).full_source_status("001")["verified"]


def test_unresolved_unknown_keeps_full_g2_waiting(tmp_path):
    db = _catalog(tmp_path / "unknown.sqlite3", 3)
    runner = ProductionEvidenceRunner(db=db)
    run = runner.start_or_resume("001")
    ids = runner.free_source_safety_preflight(run["run_id"])["target_product_ids"]
    now = datetime.now(timezone.utc).isoformat()
    observations = {pid: {"availability": "UNKNOWN", "source_price": 8.5,
        "evidence_kind": "NO_EXPLICIT_AVAILABILITY_EVIDENCE", "observed_at": now} for pid in ids}
    result = runner.run_free_source_safety_check(run["run_id"], observations=observations, confirmed=True)
    assert result["production_run"]["gates"][2]["status"] == "WAITING_FOR_INPUT"
    assert not FreeSourceSafetyService(db).full_source_status("001")["verified"]


def test_partial_capture_records_only_observed_product_and_keeps_missing_pending(tmp_path):
    db = _catalog(tmp_path / "partial.sqlite3", 3)
    runner = ProductionEvidenceRunner(db=db)
    run = runner.start_or_resume("001")
    ids = runner.free_source_safety_preflight(run["run_id"])["target_product_ids"]
    now = datetime.now(timezone.utc).isoformat()
    observation = {ids[0]: {"availability": "IN_STOCK", "availability_confidence": "HIGH",
        "source_price": 9.25, "source_currency": "USD", "evidence_kind": "VISIBLE_AVAILABILITY_TEXT",
        "qualifies_for_sellability": True, "evidence": {"normalized": "in stock"}, "observed_at": now}}
    result = runner.run_free_source_safety_check(run["run_id"], observations=observation, confirmed=True)
    assert result["pending"] == len(ids) - 1
    with connect(db) as con:
        snapshots = con.execute("SELECT product_id FROM source_product_snapshots").fetchall()
        pending = con.execute("SELECT COUNT(*) FROM source_safety_release_items WHERE status='PENDING'").fetchone()[0]
    assert [row[0] for row in snapshots] == [ids[0]]
    assert pending == len(ids) - 1
    assert result["production_run"]["gates"][2]["status"] != "VERIFIED"


def test_validation_batch_uses_unprocessed_source_candidates_and_caps_at_200(tmp_path):
    db = _catalog(tmp_path / "validation.sqlite3", 215)
    free = FreeSourceSafetyService(db)
    eligible = free.source_check_eligible("001")
    assert len(eligible) == 212
    pilot = free.preview_batch("001", "SOURCE_CHECK_PILOT")
    assert pilot["target_count"] == 10
    try:
        free.preview_batch("001", "SOURCE_VALIDATION_BATCH", selected_product_ids=[row["master_product_id"] for row in eligible[:100]])
        assert False, "validation batch must wait for pilot"
    except ValueError as exc:
        assert "Pilot" in str(exc)
    now = datetime.now(timezone.utc).isoformat()
    with connect(db) as con:
        con.execute("INSERT INTO source_safety_release_batches(batch_id,store_id,batch_kind,status,target_count,checked_count,created_at,updated_at) VALUES('pilot','001','SOURCE_CHECK_PILOT','VERIFIED',10,10,?,?)", (now, now))
        con.executemany("INSERT INTO source_safety_release_items(batch_id,product_id,status) VALUES('pilot',?,'VERIFIED')", [(pid,) for pid in pilot["target_product_ids"]])
    rest = free.unprocessed_source_candidates("001")
    selected = [row["master_product_id"] for row in rest[:200]]
    validation = free.preview_batch("001", "SOURCE_VALIDATION_BATCH", selected_product_ids=selected)
    assert validation["target_count"] == 200
    assert not set(pilot["target_product_ids"]) & set(validation["target_product_ids"])
