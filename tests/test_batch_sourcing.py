import json
from pathlib import Path

import pytest
from shopsource.capture.batch import BatchSourcingService
from shopsource.capture.service import CaptureService
from shopsource.db import connect, init_db, upsert_store
from shopsource.capture.validation import canonical_product_url


def setup_db(tmp_path):
    db = tmp_path / "batch.sqlite3"
    init_db(db)
    profile = json.loads((Path(__file__).parents[1] / "stores" / "001_cabin_tidy.json").read_text(encoding="utf-8"))
    upsert_store(profile, db)
    return db


def search_products(count=5):
    return [{"asin": f"BATCH0000{i}", "title": f"trunk organizer {i}",
             "url": f"https://www.amazon.com/dp/BATCH0000{i}", "price": 45.0,
             "images": ["https://m.media-amazon.com/images/I/synthetic.jpg"]} for i in range(1, count + 1)]


def detail(asin):
    return {"asin": asin, "title": "Trunk organizer", "url": f"https://www.amazon.com/dp/{asin}",
            "brand": "Synthetic", "price": 45.0, "images": ["https://m.media-amazon.com/images/I/synthetic.jpg"],
            "category": "Automotive", "overview": ["Synthetic detail"], "aboutThis": [], "rating": 4.5,
            "reviewCount": 40, "options": {}, "quantity": None, "tags": [],
            "_sourceUrl": f"https://www.amazon.com/dp/{asin}", "_listPage": 1, "_collectedAt": "2026-09-30T00:00:00Z"}


def prepared_batch(tmp_path, auto_import=True):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    queue = BatchSourcingService(db)
    batch = queue.create("001", "trunk organizer", 5, auto_import)
    queue.action(batch["run_id"], "RESUME")
    capture_run = capture.capture_search({"store_id": "001", "keyword": "trunk organizer",
        "search_url": "https://www.amazon.com/s?k=trunk+organizer", "products": search_products()})
    queue.add_search_capture(batch["run_id"], capture_run["run_id"])
    return db, capture, queue, batch["run_id"]


def test_batch_capture_queue_detail_auto_master_and_classify(tmp_path):
    db, capture, queue, run_id = prepared_batch(tmp_path)
    run = queue.get(run_id)
    assert run["status"] == "RUNNING" and run["detail_pending"] == 5
    item = queue.next_item(run_id)
    assert item["asin"] == "BATCH00001" and item["url"].endswith(item["asin"])
    result = capture.capture_detail({"store_id": "001", "batch_run_id": run_id, "product": detail(item["asin"])})
    assert result["batch"]["master_imported"] == 1
    with connect(db) as con:
        product_count = con.execute("SELECT COUNT(*) FROM products WHERE source_kind='BROWSER_CAPTURE'").fetchone()[0]
        decision = con.execute("SELECT final_status FROM store_product_decisions WHERE product_id=(SELECT id FROM products WHERE asin=?) AND store_id='001'", (item["asin"],)).fetchone()
    assert product_count == 1 and decision is not None
    assert queue.get(run_id)["primary_count"] == 1


def test_batch_lifecycle_pause_resume_cancel_captcha_and_retry(tmp_path):
    db, _capture, queue, run_id = prepared_batch(tmp_path, auto_import=False)
    item = queue.next_item(run_id)
    paused = queue.record_detail(run_id, item["asin"], "CAPTCHA")
    assert paused["status"] == "PAUSED_NEEDS_USER"
    resumed = queue.action(run_id, "RESUME")
    assert resumed["status"] == "RUNNING"
    item = queue.next_item(run_id)
    failed = queue.record_detail(run_id, item["asin"], "FAILED", "synthetic timeout")
    assert failed["detail_pending"] == 5
    queue.action(run_id, "PAUSE")
    assert queue.get(run_id)["status"] == "PAUSED"
    queue.action(run_id, "CANCEL")
    assert queue.get(run_id)["status"] == "CANCELLED"
    assert queue.get(run_id)["events"]


def test_missing_search_title_is_queued_but_not_importable(tmp_path):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    queue = BatchSourcingService(db)
    run = queue.create("001", "trunk organizer", 2)
    queue.action(run["run_id"], "RESUME")
    captured = capture.capture_search({"store_id":"001","keyword":"trunk organizer","search_url":"https://www.amazon.com/s?k=trunk",
        "products":[{"asin":"BATCH00001","title":"","url":"https://www.amazon.com/dp/BATCH00001"}]})
    result = queue.add_search_capture(run["run_id"], captured["run_id"])
    assert result["detail_pending"] == 1
    item = queue.next_item(run["run_id"])
    assert item and item["asin"] == "BATCH00001"


def test_batch_target_limited_and_duplicate_asin_unique(tmp_path):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    queue = BatchSourcingService(db)
    run = queue.create("001", "trunk organizer", 2, False)
    queue.action(run["run_id"], "RESUME")
    payload = {"store_id":"001","keyword":"trunk organizer","search_url":"https://www.amazon.com/s?k=trunk","products":search_products(5)}
    captured = capture.capture_search(payload)
    queue.add_search_capture(run["run_id"], captured["run_id"])
    queue.add_search_capture(run["run_id"], captured["run_id"])
    with connect(db) as con:
        count = con.execute("SELECT COUNT(*) FROM browser_batch_items WHERE batch_run_id=?",(run["run_id"],)).fetchone()[0]
    assert count == 2


def test_loopback_bridge_pairing_and_batch_next_item(tmp_path):
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("FastAPI optional dependency is unavailable")
    from shopsource.capture.bridge import install_capture_routes
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    token = capture.create_pairing_code()
    app = FastAPI()
    install_capture_routes(app, capture)
    client = TestClient(app)
    assert client.post("/api/capture/batches", json={"store_id":"001","keyword":"trunk organizer"}).status_code == 401
    headers = {"X-ShopSource-Pairing": token}
    created = client.post("/api/capture/batches", headers=headers, json={"store_id":"001","keyword":"trunk organizer","target_candidates":1}).json()
    queue = BatchSourcingService(db)
    queue.action(created["run_id"], "RESUME")
    captured = client.post("/api/capture/search-results", headers=headers, json={"store_id":"001","keyword":"trunk organizer",
        "search_url":"https://www.amazon.com/s?k=trunk+organizer","products":search_products(1)}).json()
    assert captured["batch"]["run_id"] == created["run_id"]
    next_item = client.post("/api/capture/heartbeat", headers=headers, json={"event":"NEXT_ITEM","batch_run_id":created["run_id"]}).json()
    assert next_item["item"]["asin"] == "BATCH00001"
    assert client.get(f"/api/capture/batches/{created['run_id']}", headers=headers).json()["detail_pending"] == 1


def test_extension_batch_marker_and_all_rendered_dom_policy():
    extension = Path(__file__).parents[1] / "browser_extension" / "shopsource_capture"
    search = (extension / "content_search.js").read_text(encoding="utf-8")
    detail_script = (extension / "content_product.js").read_text(encoding="utf-8")
    manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    assert "getBoundingClientRect" not in search
    assert "data-component-type=\"s-search-result\"" in search and "[data-asin]" in search
    assert "h2 a[aria-label]" in search and "sponsored" in search
    assert "shopsource_capture" in detail_script and "autoCapturePromise" in detail_script
    assert "tabs" in manifest["permissions"]
    assert manifest["version"] == "0.1.6"
    assert not set(manifest["permissions"]).intersection({"cookies", "webRequest", "history", "downloads", "proxy", "nativeMessaging"})
    assert "document.cookie" not in search + detail_script
    assert "localStorage" not in search + detail_script and "sessionStorage" not in search + detail_script
    assert "https://www.amazon.com/dp/${asin}" in search
    assert "waitForProductReadiness(15000, 500)" in detail_script
    assert "Product detail DOM did not become ready" in detail_script


def test_canonical_product_url():
    assert canonical_product_url("b0h8sfr4gt") == "https://www.amazon.com/dp/B0H8SFR4GT"
    with pytest.raises(ValueError, match="ASIN"):
        canonical_product_url("bad")


@pytest.mark.parametrize("tracking_url", [
    "https://www.amazon.com/sspa/click?ie=UTF8&asin=B0H8SFR4GT",
    "https://www.amazon.com/gp/slredirect/picassoRedirect.html?asin=B0H8SFR4GT",
])
def test_sponsored_tracking_url_becomes_canonical(tmp_path, tracking_url):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    result = capture.capture_search({"store_id": "001", "keyword": "trunk organizer",
        "search_url": "https://www.amazon.com/s?k=trunk+organizer", "products": [{
            "asin": "B0H8SFR4GT", "title": "", "url": tracking_url, "sponsored": True
        }]})
    with connect(db) as con:
        import json as json_module
        row = con.execute("SELECT search_payload_json FROM browser_capture_candidates WHERE run_id=?", (result["run_id"],)).fetchone()
    payload = json_module.loads(row["search_payload_json"])
    assert payload["url"] == "https://www.amazon.com/dp/B0H8SFR4GT"
    assert payload["_sourceUrl"] == "https://www.amazon.com/s?k=trunk+organizer"
    assert payload["title"] == ""


def test_search_tracking_url_becomes_canonical(tmp_path):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    captured = capture.capture_search({"store_id": "001", "keyword": "trunk organizer",
        "search_url": "https://www.amazon.com/s?k=trunk", "products": [{
            "asin": "B0GFD1WBP9", "title": "Organizer", "url": "https://www.amazon.com/gp/slredirect/ref=abc", "sponsored": False
        }]})
    row = capture.list_candidates("001")[0]
    assert row["run_id"] == captured["run_id"]
    assert row["search_payload"]["url"] == "https://www.amazon.com/dp/B0GFD1WBP9"


def test_four_sponsored_tracking_candidates_queue_as_canonical_products(tmp_path):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    batches = BatchSourcingService(db)
    asins = ["B0H8SFR4GT", "B0GFD1WBP9", "B0F7QTD5SV", "B09YXYSSLL"]
    products = [{"asin": asin, "title": "", "url": f"https://www.amazon.com/sspa/click?ref={index}", "sponsored": True}
                for index, asin in enumerate(asins)]
    captured = capture.capture_search({"store_id": "001", "keyword": "trunk organizer",
        "search_url": "https://www.amazon.com/s?k=trunk", "products": products})
    run = batches.create("001", "trunk organizer", 4, False)
    batches.action(run["run_id"], "RESUME")
    batches.add_search_capture(run["run_id"], captured["run_id"])
    urls = []
    for index, asin in enumerate(asins):
        with connect(db) as con:
            con.execute("UPDATE browser_batch_runs SET checkpoint_json=json_set(checkpoint_json,'$.next_open_after','2000-01-01T00:00:00+00:00') WHERE run_id=?", (run["run_id"],))
        item = batches.next_item(run["run_id"])
        assert item and item["asin"] == asin
        urls.append(item["url"])
        capture.capture_detail({"store_id": "001", "batch_run_id": run["run_id"], "product": detail(asin)})
    assert urls == [f"https://www.amazon.com/dp/{asin}" for asin in asins]


def test_product_readiness_timeout_contract():
    script = (Path(__file__).parents[1] / "browser_extension" / "shopsource_capture" / "content_product.js").read_text(encoding="utf-8")
    assert "waitForProductReadiness(15000, 500)" in script
    assert "Product detail DOM did not become ready" in script
    assert "if (isCaptcha()) throw new Error('CAPTCHA_DETECTED')" in script


def test_candidate_shortage_waits_for_manual_next_page_capture(tmp_path):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    queue = BatchSourcingService(db)
    batch = queue.create("001", "trunk organizer", 2, auto_import_master=False)
    queue.action(batch["run_id"], "RESUME")
    for index in (1, 2):
        asin = f"PAGE00000{index}"
        captured = capture.capture_search({"store_id":"001","keyword":"trunk organizer","search_url":f"https://www.amazon.com/s?k=trunk&page={index}",
            "products":[{"asin":asin,"title":"Organizer","url":f"https://www.amazon.com/dp/{asin}"}]})
        queue.add_search_capture(batch["run_id"], captured["run_id"])
        item = queue.next_item(batch["run_id"])
        capture.capture_detail({"store_id":"001","batch_run_id":batch["run_id"],"product":detail(item["asin"])})
        result = queue.get(batch["run_id"])
        if index == 1:
            assert result["status"] == "PAUSED_NEEDS_USER"
            assert "another Amazon search page" in result["error"]
        else:
            assert result["status"] == "DONE"


def test_resume_does_not_duplicate_open_tab_and_recovers_stale_checkpoint(tmp_path):
    db, _capture, queue, run_id = prepared_batch(tmp_path, auto_import=False)
    first = queue.next_item(run_id)
    queue.action(run_id, "PAUSE")
    queue.action(run_id, "RESUME")
    assert queue.next_item(run_id) is None
    with connect(db) as con:
        con.execute("UPDATE browser_batch_items SET updated_at='2020-01-01T00:00:00+00:00' WHERE batch_run_id=? AND asin=?", (run_id, first["asin"]))
    recovered = queue.next_item(run_id)
    assert recovered and recovered["asin"] == first["asin"]
    with connect(db) as con:
        retries = con.execute("SELECT retry_count FROM browser_batch_items WHERE batch_run_id=? AND asin=?", (run_id, first["asin"])).fetchone()["retry_count"]
    assert retries == 1


def _existing_five_candidate_state(tmp_path):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    batches = BatchSourcingService(db)
    captured = capture.capture_search({"store_id": "001", "keyword": "trunk organizer",
        "search_url": "https://www.amazon.com/s?k=trunk+organizer", "products": search_products(5)})
    # Candidate A is already detail-complete but has not yet entered MASTER.
    capture.capture_detail({"store_id": "001", "product": detail("BATCH00001")})
    # Reproduce the operator-visible RUNNING-but-empty batch from the real incident.
    batch = batches.create("001", "trunk organizer", 5, True)
    batches.action(batch["run_id"], "RESUME")
    return db, capture, batches, batch["run_id"], captured["run_id"]


def test_reuse_active_batch_and_running_empty_reconcile(tmp_path):
    db, _capture, batches, run_id, _capture_run_id = _existing_five_candidate_state(tmp_path)
    reused = batches.ensure_active_batch("001", "trunk organizer", 5, True)
    assert reused["run_id"] == run_id
    result = batches.process_existing_candidates("001", "trunk organizer", 5, True)
    assert result["run_id"] == run_id
    assert result["queued"] == 4
    assert result["status"] == "RUNNING"
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM browser_batch_runs WHERE store_id='001' AND keyword='trunk organizer' AND status IN ('PENDING','RUNNING','PAUSED','PAUSED_NEEDS_USER')").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM browser_batch_items WHERE batch_run_id=?", (run_id,)).fetchone()[0] == 4


def test_five_product_realistic_flow_auto_imports_and_classifies(tmp_path):
    db, capture, batches, run_id, _capture_run_id = _existing_five_candidate_state(tmp_path)
    result = batches.process_existing_candidates("001", "trunk organizer", 5, True)
    assert result["run_id"] == run_id and result["existing_imported"] == 1
    assert result["queued"] == 4
    assert result["precompleted_count"] == 1
    assert result["detail_pending"] == 4
    for _ in range(4):
        with connect(db) as con:
            con.execute("UPDATE browser_batch_runs SET checkpoint_json=json_set(checkpoint_json,'$.next_open_after','2000-01-01T00:00:00+00:00') WHERE run_id=?", (run_id,))
        item = batches.next_item(run_id)
        assert item is not None
        capture.capture_detail({"store_id": "001", "batch_run_id": run_id, "product": detail(item["asin"])})
    final = batches.get(run_id)
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM products WHERE source_kind='BROWSER_CAPTURE'").fetchone()[0] == 5
        assert con.execute("SELECT COUNT(*) FROM store_product_decisions WHERE store_id='001' AND product_id IN (SELECT id FROM products WHERE source_kind='BROWSER_CAPTURE')").fetchone()[0] == 5
        assert con.execute("SELECT COUNT(*) FROM browser_batch_items WHERE batch_run_id=? AND state='MASTER_IMPORTED'", (run_id,)).fetchone()[0] == 4
    assert final["status"] == "DONE"
    assert final["master_imported"] == 4
    assert batches.pipeline_summary("001")["master_count"] == 5


def test_no_duplicate_batch_or_items_after_repeated_process(tmp_path):
    db, _capture, batches, run_id, _capture_run_id = _existing_five_candidate_state(tmp_path)
    for _ in range(10):
        result = batches.process_existing_candidates("001", "trunk organizer", 5, True)
        assert result["run_id"] == run_id
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM browser_batch_runs WHERE store_id='001' AND keyword='trunk organizer' AND status IN ('PENDING','RUNNING','PAUSED','PAUSED_NEEDS_USER')").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM browser_batch_items WHERE batch_run_id=?", (run_id,)).fetchone()[0] == 4
        assert con.execute("SELECT COUNT(DISTINCT asin) FROM browser_batch_items WHERE batch_run_id=?", (run_id,)).fetchone()[0] == 4
        assert con.execute("SELECT COUNT(*) FROM products WHERE source_kind='BROWSER_CAPTURE'").fetchone()[0] == 1


def test_restart_does_not_duplicate_open_item(tmp_path):
    db, _capture, batches, run_id = prepared_batch(tmp_path, auto_import=False)
    item = batches.next_item(run_id)
    restarted = BatchSourcingService(db)
    assert restarted.next_item(run_id) is None
    assert restarted.get(run_id)["items"][0]["asin"] == item["asin"] or any(x["asin"] == item["asin"] for x in restarted.get(run_id)["items"])


def test_capture_counts_remain_distinct_from_batch_item_counts(tmp_path):
    db, _capture, batches, run_id, _capture_run_id = _existing_five_candidate_state(tmp_path)
    result = batches.process_existing_candidates("001", "trunk organizer", 5, True)
    assert result["capture_candidate_count"] == 5
    with connect(db) as con:
        batch_items = con.execute("SELECT COUNT(*) FROM browser_batch_items WHERE batch_run_id=?", (run_id,)).fetchone()[0]
    assert batch_items == 4
    assert batches.pipeline_summary("001")["candidates"] == 5


def test_process_unfinished_kicks_existing_pending_queue(tmp_path):
    _db, _capture, batches, run_id, _capture_run_id = _existing_five_candidate_state(tmp_path)
    first = batches.process_existing_candidates("001", "trunk organizer", 5, True)
    assert first["queued"] == 4 and first["detail_pending"] == 4
    # This is the second operator click / retry of the unfinished workflow.
    second = batches.process_existing_candidates("001", "trunk organizer", 5, True)
    assert second["queued"] == 0 and second["existing_imported"] == 0
    assert second["detail_pending"] == 4 and second["run_id"] == run_id
    kickoff = batches.kickoff(run_id)
    assert kickoff["state"] == "OPENED"
    assert kickoff["item"]["url"] == f"https://www.amazon.com/dp/{kickoff['item']['asin']}"


def test_existing_pending_queued_zero_is_not_empty(tmp_path):
    _db, _capture, batches, run_id, _capture_run_id = _existing_five_candidate_state(tmp_path)
    batches.process_existing_candidates("001", "trunk organizer", 5, True)
    result = batches.process_existing_candidates("001", "trunk organizer", 5, True)
    assert result["queued"] == 0
    assert result["detail_pending"] > 0
    assert batches.kickoff(run_id)["state"] == "OPENED"


def test_running_pending_opens_first_item(tmp_path):
    _db, _capture, batches, run_id, _capture_run_id = _existing_five_candidate_state(tmp_path)
    batches.process_existing_candidates("001", "trunk organizer", 5, True)
    result = batches.kickoff(run_id)
    assert result["state"] == "OPENED"
    assert result["item"]["asin"]


def test_running_opened_does_not_duplicate_tab(tmp_path):
    _db, _capture, batches, run_id, _capture_run_id = _existing_five_candidate_state(tmp_path)
    batches.process_existing_candidates("001", "trunk organizer", 5, True)
    first = batches.kickoff(run_id)
    second = batches.kickoff(run_id)
    assert first["state"] == "OPENED"
    assert second["state"] == "IN_PROGRESS" and second["item"] is None
    assert batches.next_item(run_id) is None


def test_resume_button_running_behavior(tmp_path):
    _db, _capture, batches, run_id, _capture_run_id = _existing_five_candidate_state(tmp_path)
    batches.process_existing_candidates("001", "trunk organizer", 5, True)
    # RUNNING uses kickoff (not RESUME, which correctly rejects RUNNING).
    assert batches.get(run_id)["status"] == "RUNNING"
    assert batches.kickoff(run_id)["state"] == "OPENED"
    with pytest.raises(ValueError, match="Batch is not paused"):
        batches.action(run_id, "RESUME")


def test_true_empty_state_message_only_when_no_pending(tmp_path):
    db = setup_db(tmp_path)
    batches = BatchSourcingService(db)
    run = batches.create("001", "trunk organizer", 5)
    batches.action(run["run_id"], "RESUME")
    outcome = batches.kickoff(run["run_id"])
    assert outcome["state"] == "NO_PENDING"
    assert outcome["run"]["detail_pending"] == 0


def test_operator_open_item_recovery(tmp_path):
    db, _capture, batches, run_id = prepared_batch(tmp_path, auto_import=False)
    first = batches.next_item(run_id)
    recovered = batches.recover_open_item(run_id)
    assert recovered["opened_count"] == 0
    assert recovered["detail_pending"] == 5
    assert any(event["event_type"] == "OPEN_ITEM_RECOVERED" for event in recovered["events"])
    opened_again = batches.kickoff(run_id)
    assert opened_again["state"] == "OPENED"
    assert opened_again["item"]["asin"] == first["asin"]
    with connect(db) as con:
        row = con.execute("SELECT state,last_error FROM browser_batch_items WHERE batch_run_id=? AND asin=?", (run_id, first["asin"])).fetchone()
    assert row["state"] == "DETAIL_OPENED"
    assert row["last_error"] == "Operator requested open-item recovery"


def test_recovery_requires_explicit_action(tmp_path):
    _db, _capture, batches, run_id = prepared_batch(tmp_path, auto_import=False)
    opened = batches.next_item(run_id)
    assert batches.kickoff(run_id)["state"] == "IN_PROGRESS"
    # Read-only status checks/kickoff do not reset or reserve a second item.
    assert batches.get(run_id)["opened_count"] == 1
    assert any(item["asin"] == opened["asin"] and item["state"] == "DETAIL_OPENED" for item in batches.get(run_id)["items"])
    source = (Path(__file__).parents[1] / "src" / "shopsource" / "ui" / "v2.py").read_text(encoding="utf-8")
    polling = source.split("def poll_batch():", 1)[1].split("def start_batch():", 1)[0]
    assert "recover_open_item(" not in polling
    assert "batch_service.recover_open_item(run_id)" in source


def test_recovery_does_not_create_duplicate_batch(tmp_path):
    db, _capture, batches, run_id, _capture_run_id = _existing_five_candidate_state(tmp_path)
    batches.process_existing_candidates("001", "trunk organizer", 5, True)
    batches.kickoff(run_id)
    batches.recover_open_item(run_id)
    batches.kickoff(run_id)
    assert batches.ensure_active_batch("001", "trunk organizer", 5, True)["run_id"] == run_id
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM browser_batch_runs WHERE store_id='001' AND lower(keyword)=lower('trunk organizer')").fetchone()[0] == 1


def test_five_product_flow_after_handshake(tmp_path):
    db, capture, batches, run_id, _capture_run_id = _existing_five_candidate_state(tmp_path)
    result = batches.process_existing_candidates("001", "trunk organizer", 5, True)
    assert result["existing_imported"] == 1 and result["detail_pending"] == 4
    for index in range(4):
        if index:
            with connect(db) as con:
                con.execute("UPDATE browser_batch_runs SET checkpoint_json=json_set(checkpoint_json,'$.next_open_after','2000-01-01T00:00:00+00:00') WHERE run_id=?", (run_id,))
        kickoff = batches.kickoff(run_id)
        assert kickoff["state"] == "OPENED"
        response = capture.capture_detail({"store_id": "001", "batch_run_id": run_id, "product": detail(kickoff["item"]["asin"])})
        assert response["status"] == "DETAIL_COMPLETE"
    final = batches.get(run_id)
    summary = batches.pipeline_summary("001")
    assert final["status"] == "DONE"
    assert summary["candidates"] == summary["master_count"] == summary["classified_count"] == 5


def test_replayed_successful_handshake_is_idempotent(tmp_path):
    db, capture, batches, run_id = prepared_batch(tmp_path, auto_import=True)
    opened = batches.next_item(run_id)
    payload = detail(opened["asin"])
    first = capture.capture_detail({"store_id": "001", "batch_run_id": run_id, "product": payload})
    second = capture.capture_detail({"store_id": "001", "batch_run_id": run_id, "product": payload})
    assert first["status"] == second["status"] == "DETAIL_COMPLETE"
    assert second["duplicate"] is True
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM product_occurrences WHERE product_id=(SELECT id FROM products WHERE asin=?)", (opened["asin"],)).fetchone()[0] == 1


def test_extension_event_allowlist_and_diagnostics(tmp_path):
    db, _capture, batches, run_id = prepared_batch(tmp_path, auto_import=False)
    event_run = batches.record_extension_event(run_id, "AUTO_CAPTURE_TRIGGERED", "BATCH00001", "", 42)
    event = event_run["events"][0]
    assert event["event_type"] == "AUTO_CAPTURE_TRIGGERED"
    assert '"tab_id":42' in event["payload_json"]
    with pytest.raises(ValueError, match="Unsupported extension event"):
        batches.record_extension_event(run_id, "UNTRUSTED_EVENT", "BATCH00001", "raw free text")
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM browser_batch_events WHERE batch_run_id=? AND event_type='UNTRUSTED_EVENT'", (run_id,)).fetchone()[0] == 0


def test_queue_existing_kicks_already_queued_items(tmp_path):
    _db, _capture, batches, run_id, _capture_run_id = _existing_five_candidate_state(tmp_path)
    queued = batches.process_existing_candidates("001", "trunk organizer", 5, True)
    assert queued["queued"] == 4
    already_queued = batches.queue_existing_candidates(run_id)
    assert already_queued["queued"] == 0 and already_queued["detail_pending"] == 4
    assert batches.kickoff(run_id)["state"] == "OPENED"


def test_popup_kickoff_requires_user_action_contract():
    source = (Path(__file__).parents[1] / "src" / "shopsource" / "ui" / "v2.py").read_text(encoding="utf-8")
    assert "def kick_batch(run_id):" in source
    assert "def process_unfinished():" in source and "outcome = kick_batch(result[\"run_id\"])" in source
    poll = source.split("def poll_batch():", 1)[1].split("def start_batch():", 1)[0]
    assert "kick_batch(" not in poll
    kickoff = source.split("def kick_batch(run_id):", 1)[1].split("def continue_batch():", 1)[0]
    assert "window.postMessage(" in kickoff
    assert "next_item(" not in kickoff and "window.open(" not in kickoff


def test_existing_bad_url_next_item_uses_canonical(tmp_path):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    batches = BatchSourcingService(db)
    run = batches.create("001", "trunk organizer", 1, False)
    batches.action(run["run_id"], "RESUME")
    captured = capture.capture_search({"store_id": "001", "keyword": "trunk organizer",
        "search_url": "https://www.amazon.com/s?k=trunk", "products": search_products(1)})
    batches.add_search_capture(run["run_id"], captured["run_id"])
    with connect(db) as con:
        con.execute("UPDATE browser_capture_candidates SET search_payload_json=json_set(search_payload_json,'$.url',NULL) WHERE run_id=?", (captured["run_id"],))
        con.execute("UPDATE browser_batch_items SET state='FAILED',retry_count=2,last_error='Detail page capture timed out' WHERE batch_run_id=?", (run["run_id"],))
        con.execute("UPDATE browser_batch_runs SET status='DONE',failed_count=1 WHERE run_id=?", (run["run_id"],))
    retried = batches.action(run["run_id"], "RETRY")
    item = batches.next_item(run["run_id"])
    assert retried["status"] == "RUNNING"
    assert item["url"] == "https://www.amazon.com/dp/BATCH00001"


def test_done_with_errors_status_and_retry(tmp_path):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    batches = BatchSourcingService(db)
    run = batches.create("001", "trunk organizer", 1, False)
    batches.action(run["run_id"], "RESUME")
    captured = capture.capture_search({"store_id": "001", "keyword": "trunk organizer",
        "search_url": "https://www.amazon.com/s?k=trunk", "products": search_products(1)})
    batches.add_search_capture(run["run_id"], captured["run_id"])
    with connect(db) as con:
        con.execute("UPDATE browser_batch_items SET state='FAILED',retry_count=2,last_error='Synthetic detail timeout' WHERE batch_run_id=?", (run["run_id"],))
    batches._refresh(run["run_id"])
    failed_run = batches.get(run["run_id"])
    assert failed_run["status"] == "DONE_WITH_ERRORS"
    assert failed_run["failed_items"][0]["last_error"] == "Synthetic detail timeout"
    assert BatchSourcingService(db).active("001")[0]["run_id"] == run["run_id"]
    retried = batches.action(run["run_id"], "RETRY")
    assert retried["status"] == "RUNNING"
    assert retried["failed_count"] == 0
    assert retried["items"][0]["retry_count"] == 0


def test_asin_mismatch_rejected_and_recorded(tmp_path):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    batches = BatchSourcingService(db)
    run = batches.create("001", "trunk organizer", 1, False)
    batches.action(run["run_id"], "RESUME")
    captured = capture.capture_search({"store_id": "001", "keyword": "trunk organizer",
        "search_url": "https://www.amazon.com/s?k=trunk", "products": search_products(1)})
    batches.add_search_capture(run["run_id"], captured["run_id"])
    batches.next_item(run["run_id"])
    wrong = detail("BATCH00002")
    with pytest.raises(ValueError, match="Opened product ASIN did not match queued ASIN"):
        capture.capture_detail({"store_id": "001", "batch_run_id": run["run_id"], "product": wrong})
    item = batches.get(run["run_id"])["failed_items"][0]
    assert item["asin"] == "BATCH00001"
    assert item["last_error"] == "Opened product ASIN did not match queued ASIN"
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM products WHERE asin='BATCH00002'").fetchone()[0] == 0


def test_four_failed_items_recover_to_master(tmp_path):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    batches = BatchSourcingService(db)
    capture.capture_search({"store_id": "001", "keyword": "trunk organizer",
        "search_url": "https://www.amazon.com/s?k=trunk", "products": search_products(5)})
    capture.capture_detail({"store_id": "001", "product": detail("BATCH00001")})
    run = batches.create("001", "trunk organizer", 5, True)
    batches.action(run["run_id"], "RESUME")
    queued = batches.process_existing_candidates("001", "trunk organizer", 5, True)
    assert queued["existing_imported"] == 1 and queued["queued"] == 4
    with connect(db) as con:
        con.execute("UPDATE browser_capture_candidates SET search_payload_json=json_set(search_payload_json,'$.url',NULL) WHERE asin!='BATCH00001'")
        con.execute("UPDATE browser_batch_items SET state='FAILED',retry_count=2,last_error='Missing valid Amazon product URL' WHERE batch_run_id=?", (run["run_id"],))
        con.execute("UPDATE browser_batch_runs SET status='DONE',failed_count=4 WHERE run_id=?", (run["run_id"],))
    retried = batches.action(run["run_id"], "RETRY")
    assert retried["status"] == "RUNNING"
    assert retried["precompleted_count"] == 1
    for _ in range(4):
        with connect(db) as con:
            con.execute("UPDATE browser_batch_runs SET checkpoint_json=json_set(checkpoint_json,'$.next_open_after','2000-01-01T00:00:00+00:00') WHERE run_id=?", (run["run_id"],))
        item = batches.next_item(run["run_id"])
        assert item and item["url"] == f"https://www.amazon.com/dp/{item['asin']}"
        capture.capture_detail({"store_id": "001", "batch_run_id": run["run_id"], "product": detail(item["asin"])})
    final = batches.get(run["run_id"])
    summary = batches.pipeline_summary("001")
    assert final["status"] == "DONE" and final["failed_count"] == 0
    assert summary["candidates"] == summary["detail_complete"] == summary["master_count"] == summary["classified_count"] == 5
    assert summary["failed_count"] == 0


def test_pipeline_summary_5_of_5(tmp_path):
    db = setup_db(tmp_path)
    capture = CaptureService(db)
    batches = BatchSourcingService(db)
    capture.capture_search({"store_id": "001", "keyword": "trunk organizer",
        "search_url": "https://www.amazon.com/s?k=trunk", "products": search_products(5)})
    for product in search_products(5):
        capture.capture_detail({"store_id": "001", "product": detail(product["asin"])})
    capture.import_candidates("001", [product["asin"] for product in search_products(5)])
    summary = batches.pipeline_summary("001")
    assert summary["candidates"] == 5
    assert summary["detail_complete"] == summary["master_count"] == summary["classified_count"] == 5
