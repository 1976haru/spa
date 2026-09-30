import json
from pathlib import Path

import pytest
from shopsource.capture.batch import BatchSourcingService
from shopsource.capture.service import CaptureService
from shopsource.db import connect, init_db, upsert_store


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
    assert "shopsource_capture" in detail_script and "autoStarted" in detail_script
    assert "tabs" in manifest["permissions"]
    assert not set(manifest["permissions"]).intersection({"cookies", "webRequest", "history", "downloads", "proxy", "nativeMessaging"})
    assert "document.cookie" not in search + detail_script
    assert "localStorage" not in search + detail_script and "sessionStorage" not in search + detail_script


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
