import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from shopsource.capture.batch import BatchSourcingService
from shopsource.db import connect
from shopsource.production_runner import ProductionEvidenceRunner
from shopsource.ui.browser_capture_bridge import request_batch_open_next, start_free_capture


def _runner_with_source_candidates(path):
    from shopsource.db import init_db

    init_db(path)
    now = datetime.now(timezone.utc).isoformat()
    with connect(path) as con:
        con.execute("INSERT INTO stores(store_id,store_name,category,profile_json,created_at,updated_at) VALUES('001','Synthetic Cabin','Car Organization','{}',?,?)", (now, now))
        for index in range(1, 5):
            asin = f"B{index:09d}"
            con.execute("INSERT INTO products(asin,title,price,source_url,source_kind,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?)",
                (asin, f"Organizer {index}", 12.0, f"https://www.amazon.com/dp/{asin}", "BROWSER_CAPTURE", json.dumps({}), now, now))
            con.execute("INSERT INTO store_product_decisions(store_id,product_id,fit_score,price_status,risk_status,auto_status,final_status,classified_at) VALUES('001',?,50,'OK','CLEAR','PRIMARY','PRIMARY',?)", (index, now))
    runner = ProductionEvidenceRunner(db=path)
    return runner, runner.start_or_resume("001")


def test_repeated_pilot_start_reuses_existing_browser_queue(tmp_path):
    runner, run = _runner_with_source_candidates(tmp_path / "pilot.sqlite3")

    first = runner.prepare_free_browser_capture_batch(run["run_id"], confirmed=True)
    second = runner.prepare_free_browser_capture_batch(run["run_id"], confirmed=True)

    assert first["target_count"] <= 10
    assert second["reused"] is True
    assert second["release_batch_id"] == first["release_batch_id"]
    assert second["browser_batch_run_id"] == first["browser_batch_run_id"]
    with connect(runner.db) as con:
        assert con.execute("SELECT COUNT(*) FROM source_safety_release_batches WHERE store_id='001' AND batch_kind='SOURCE_CHECK_PILOT'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM browser_batch_runs WHERE store_id='001' AND keyword='FREE SOURCE SAFETY'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM source_product_snapshots").fetchone()[0] == 0


def test_existing_paused_pilot_can_resume_without_adding_queue_items(tmp_path):
    runner, run = _runner_with_source_candidates(tmp_path / "resume.sqlite3")
    prepared = runner.prepare_free_browser_capture_batch(run["run_id"], confirmed=True)
    before = BatchSourcingService(runner.db).get(prepared["browser_batch_run_id"])
    resumed = runner.resume_free_browser_capture_batch(prepared["browser_batch_run_id"])
    after = BatchSourcingService(runner.db).get(prepared["browser_batch_run_id"])

    assert before["detail_pending"] == after["detail_pending"] == resumed["detail_pending"]
    assert resumed["status"] == "RUNNING"
    assert after["opened_count"] == 0


def test_launch_uses_existing_extension_bridge_run_id_and_ack_state():
    class FakeUI:
        async def run_javascript(self, source, timeout):
            self.source = source
            self.timeout = timeout
            return {"state": "ACKNOWLEDGED"}

    ui = FakeUI()
    result = asyncio.run(request_batch_open_next(ui, "BB_0123456789abcdefabcd"))
    assert result["state"] == "ACKNOWLEDGED"
    assert "batch-open-next" in ui.source
    assert "shopsource-studio-ui" in ui.source
    assert "BB_0123456789abcdefabcd" in ui.source
    assert "EXTENSION_NOT_CONNECTED" in ui.source
    assert ui.timeout == 3.0

    class MissingExtensionUI:
        async def run_javascript(self, source, timeout):
            return None

    missing = asyncio.run(request_batch_open_next(MissingExtensionUI(), "BB_0123456789abcdefabcd"))
    assert missing["state"] == "EXTENSION_NOT_CONNECTED"


def test_one_click_prepares_resumes_and_launches_existing_queue(tmp_path):
    runner, run = _runner_with_source_candidates(tmp_path / "one-click.sqlite3")

    class FakeUI:
        async def run_javascript(self, source, timeout):
            self.source = source
            return {"state": "ACKNOWLEDGED"}

    ui = FakeUI()
    result = asyncio.run(start_free_capture(runner, run["run_id"], "SOURCE_CHECK_PILOT", ui))

    assert result["bridge"]["state"] == "ACKNOWLEDGED"
    assert result["browser_batch_run_id"] == result["batch"]["browser_batch_run_id"]
    assert "batch-open-next" in ui.source
    assert BatchSourcingService(runner.db).get(result["browser_batch_run_id"])["status"] == "RUNNING"
    assert runner.prepare_free_browser_capture_batch(run["run_id"], confirmed=True)["browser_batch_run_id"] == result["browser_batch_run_id"]


def test_production_source_ui_exposes_resume_and_requires_full_capture_before_apply():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert "10개 무료 검사 계속" in source
    assert "request_batch_open_next(ui, browser_run_id)" in source
    assert "캡처 결과 반영" in source
    assert "captured_count" in source  # progress is read from persisted capture candidates
    assert "모든 파일럿 캡처가 끝난 뒤 결과를 반영" in source
    assert "확장프로그램 연결이 필요합니다" in source
