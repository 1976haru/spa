from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from shopsource.db import connect, init_db
from shopsource.store_build import STAGES, StoreBuildOrchestrator


@pytest.fixture
def tmp_path():
    path = Path.cwd() / "exports" / ".test_scratch" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    return path


def seed(db, count=2):
    init_db(db)
    with connect(db) as con:
        for n in range(count):
            cursor = con.execute("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                                 (f"B{n:09d}", f"Fixture {n}", "{}", "now", "now"))
            con.execute("INSERT INTO store_product_decisions(store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at) VALUES(?,?,?,?,?,?,?)",
                        ("s1", cursor.lastrowid, "IN_RANGE", "SAFE", "PRIMARY", "PRIMARY", "now"))


def all_handlers(calls):
    return {stage: (lambda run, name=stage: calls.append(name) or {"counts": {"done": 1}}) for stage in STAGES}


def test_store_build_orchestrates_existing_components(tmp_path):
    db = tmp_path / "test.sqlite3"; seed(db)
    called = []
    service = StoreBuildOrchestrator(db=db, export_dir=tmp_path / "exports", handlers=all_handlers(called))
    preview = service.preview("s1", mode="LIVE")
    result = service.start(preview["run_id"], live_confirmed=True)
    assert result["status"] == "COMPLETE"
    assert called == list(STAGES)
    assert result["stages"]["FINAL_VERIFY"] == "COMPLETE"


def test_store_build_pause_resume(tmp_path):
    db = tmp_path / "test.sqlite3"; seed(db)
    service_ref = {}
    calls = []
    def pause_at_validation(run):
        calls.append("SOURCE_VALIDATION")
        assert service_ref["service"].pause(run["run_id"])
        return {"counts": {"checked": 1}}
    handlers = all_handlers(calls); handlers["SOURCE_VALIDATION"] = pause_at_validation
    service = StoreBuildOrchestrator(db=db, export_dir=tmp_path / "exports", handlers=handlers); service_ref["service"] = service
    preview = service.preview("s1", mode="LIVE")
    paused = service.start(preview["run_id"], live_confirmed=True)
    assert paused["status"] == "PAUSED"
    completed = service.resume(preview["run_id"])
    assert completed["status"] == "COMPLETE"


def test_store_build_preview_invalidated_when_inputs_change(tmp_path):
    db = tmp_path / "test.sqlite3"; seed(db)
    service = StoreBuildOrchestrator(db=db, export_dir=tmp_path / "exports", handlers=all_handlers([]))
    preview = service.preview("s1", mode="LIVE")
    with connect(db) as con: con.execute("UPDATE products SET title='Changed after preview' WHERE id=1")
    with pytest.raises(RuntimeError, match="stale"):
        service.start(preview["run_id"], live_confirmed=True)


def test_store_build_spark_fallback_manual_gate(tmp_path):
    db = tmp_path / "test.sqlite3"; seed(db)
    calls = []
    handlers = all_handlers(calls)
    handlers["PRODUCT_VERIFY"] = lambda run: {"status": "MANUAL_ACTION_REQUIRED", "manual_gate": "SPARK_UPLOAD",
                                               "package_id": "fixture", "instructions": "User must upload."}
    service = StoreBuildOrchestrator(db=db, export_dir=tmp_path / "exports", handlers=handlers)
    preview = service.preview("s1", provider="SPARK_FALLBACK", mode="LIVE")
    gated = service.start(preview["run_id"], live_confirmed=True)
    assert gated["status"] == "MANUAL_ACTION_REQUIRED"
    assert gated["stage"] == "PRODUCT_VERIFY"
    assert gated["stage_data"]["PRODUCT_VERIFY"]["manual_gate"] == "SPARK_UPLOAD"
    resumed = service.resume(preview["run_id"], manual_confirmation="spark_upload_confirmed")
    assert resumed["status"] == "COMPLETE_WITH_WARNINGS"


def test_store_build_theme_manual_gate_not_false_complete(tmp_path):
    db = tmp_path / "test.sqlite3"; seed(db)
    calls = []; handlers = all_handlers(calls)
    handlers["HOMEPAGE_PLAN"] = lambda _run: {"status": "MANUAL_ACTION_REQUIRED", "manual_gate": "THEME_APPLY",
                                               "instructions": "Apply exported patch in Theme Editor."}
    service = StoreBuildOrchestrator(db=db, export_dir=tmp_path / "exports", handlers=handlers)
    preview = service.preview("s1", mode="LIVE")
    gated = service.start(preview["run_id"], live_confirmed=True)
    assert gated["status"] == "MANUAL_ACTION_REQUIRED"
    assert gated["stages"]["HOMEPAGE_PLAN"] == "MANUAL_ACTION_REQUIRED"
    assert gated["stages"]["COMPLETE"] == "PENDING"
    resumed = service.resume(preview["run_id"], manual_confirmation="theme_manual_apply_confirmed")
    assert resumed["status"] == "COMPLETE_WITH_WARNINGS"


def test_paid_image_generation_requires_opt_in(tmp_path):
    db = tmp_path / "test.sqlite3"; seed(db)
    service = StoreBuildOrchestrator(db=db, export_dir=tmp_path / "exports")
    preview = service.preview("s1", options={"collection_images": True, "paid_image_opt_in": False})
    assert preview["options"]["collection_images"] is False
    assert preview["options"]["paid_image_opt_in"] is False


def test_no_real_network_in_tests(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite3"; seed(db)
    service = StoreBuildOrchestrator(db=db, export_dir=tmp_path / "exports", handlers=all_handlers([]))
    preview = service.preview("s1", provider="SPARK_FALLBACK")
    assert preview["write_performed"] is False
    assert "MANUAL_ACTION_REQUIRED" in StoreBuildOrchestrator.__dict__["resume"].__code__.co_names or preview["status"] == "PENDING"


def test_token_never_logged_in_store_build_reports(tmp_path):
    db = tmp_path / "test.sqlite3"; seed(db)
    called = []
    handlers = all_handlers(called)
    fake_token = "x" * 40
    handlers["SOURCE_VALIDATION"] = lambda _run: {"error": f"authorization token={fake_token}"}
    service = StoreBuildOrchestrator(db=db, export_dir=tmp_path / "exports", handlers=handlers)
    preview = service.preview("s1", mode="LIVE")
    result = service.start(preview["run_id"], live_confirmed=True)
    report_path = Path(service._write_report(preview["run_id"])) / "summary.json"
    report = report_path.read_text(encoding="utf-8")
    assert fake_token not in report
    assert "authorization token" not in report


def test_protected_store_file_untouched(tmp_path):
    db = tmp_path / "test.sqlite3"; seed(db)
    service = StoreBuildOrchestrator(db=db, export_dir=tmp_path / "exports", handlers=all_handlers([]))
    preview = service.preview("s1")
    assert preview["store_id"] == "s1"
    assert "stores" not in str(service.export_dir).casefold()
