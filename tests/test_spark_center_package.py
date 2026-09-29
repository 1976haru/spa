import json
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from shopsource.classifier import manual_override
from shopsource.cli import main as cli_main
from shopsource.connectors import spark_center_package as package_module
from shopsource.connectors.spark_center import capability as api_capability
from shopsource.connectors.spark_center_package import (
    SparkCenterPackageService,
    list_packages,
    mark_package,
    safe_store_folder,
)
from shopsource.connectors.spark_handoff import HandoffValidationError
from shopsource.db import connect, init_db, upsert_store, utc_now


def seed_store(db: Path, store_id="001", store_name="Cabin Tidy", primary_count=60):
    upsert_store({"store_id": store_id, "store_name": store_name, "category": "test"}, db)
    with connect(db) as con:
        rows = [(f"B{index:09}", "PRIMARY") for index in range(1, primary_count + 1)]
        rows += [("BRESERVE01", "RESERVE_B"), ("BRESTRICT1", "RESTRICTED")]
        for asin, status in rows:
            payload = {
                "asin": asin,
                "title": f"Synthetic {asin}",
                "brand": "Fixture",
                "price": 50,
                "images": [],
            }
            cur = con.execute(
                """INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?)""",
                (asin, payload["title"], json.dumps(payload), utc_now(), utc_now()),
            )
            con.execute(
                """INSERT INTO store_product_decisions(
                store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (store_id, cur.lastrowid, status, "SAFE", status, status, utc_now()),
            )


@pytest.fixture
def package_db(tmp_path):
    db = tmp_path / "packages.sqlite3"
    init_db(db)
    seed_store(db)
    return db


def test_default_project_local_package_limit_50_and_history(package_db, tmp_path, monkeypatch):
    export_dir = tmp_path / "한글 project with spaces" / "exports"
    monkeypatch.setattr(package_module, "EXPORT_DIR", export_dir)
    result = SparkCenterPackageService().create(
        store_id="001", limit=50, package_id="SC_001_DEFAULT_50", db=package_db
    )
    expected_store = export_dir / "spark_center" / "001_Cabin_Tidy"
    assert result.folder == expected_store / "ready" / result.package_id
    assert result.product_count == 50
    assert result.requested_limit == 50
    assert result.package_status == "CREATED"
    assert result.validation_status == "PASS"
    assert result.portal_package_verified is False
    assert result.spark_center_manual_upload_allowed is True
    files = sorted(result.folder.iterdir())
    assert len(files) == 50
    assert all(file.is_file() and file.suffix == ".json" for file in files)
    assert result.manifest_path == expected_store / "manifests" / f"{result.package_id}.manifest.json"
    assert result.validation_report_path == expected_store / "reports" / f"{result.package_id}.validation.json"
    assert result.manifest_path.parent != result.folder
    assert result.validation_report_path.parent != result.folder
    asins = [json.loads(file.read_text(encoding="utf-8"))["asin"] for file in files]
    assert len(asins) == len(set(asins)) == 50

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["target"] == "SPARK_CENTER_MANUAL"
    assert manifest["package_id"] == result.package_id
    assert manifest["requested_limit"] == 50
    assert manifest["package_status"] == "CREATED"
    assert manifest["spark_center_manual_upload_allowed"] is True
    assert manifest["portal_package_verified"] is False
    report = json.loads(result.validation_report_path.read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert report["checks"]["package_under_store_ready"] is True
    assert report["checks"]["upload_folder_json_only"] is True

    history = list_packages("001", db=package_db)
    assert history[0]["package_id"] == result.package_id
    assert history[0]["target"] == "SPARK_CENTER_MANUAL"
    assert history[0]["package_status"] == "CREATED"
    assert history[0]["store_name"] == "Cabin Tidy"


def test_primary_five_manual_override_and_restricted_gate(package_db, tmp_path):
    service = SparkCenterPackageService()
    five = service.create(
        store_id="001", limit=5, out_root=tmp_path / "five",
        package_id="SC_001_FIVE", db=package_db,
    )
    assert five.product_count == 5
    assert [path.name for path in sorted(five.folder.glob("*.json"))] == [
        f"{index:09}.json" for index in range(1, 6)
    ]

    manual_override("001", "BRESERVE01", "PRIMARY", db=package_db)
    promoted = service.create(
        store_id="001", statuses=["PRIMARY"], asins=["BRESERVE01"], limit=1,
        out_root=tmp_path / "manual", package_id="SC_001_MANUAL", db=package_db,
    )
    assert promoted.product_count == 1

    with pytest.raises(ValueError, match="allow_restricted"):
        service.create(
            store_id="001", statuses=["RESTRICTED"], limit=1,
            out_root=tmp_path / "restricted", package_id="SC_001_RESTRICTED", db=package_db,
        )


def test_mark_uploaded_and_invalid_status(package_db, tmp_path):
    result = SparkCenterPackageService().create(
        store_id="001", limit=5, out_root=tmp_path,
        package_id="SC_001_MARK", db=package_db,
    )
    marked = mark_package(result.package_id, "UPLOADED", "Portal upload clicked", package_db)
    assert marked["package_status"] == "UPLOADED"
    assert marked["uploaded_at"]
    assert marked["portal_package_verified"] is False
    history = list_packages("001", db=package_db)
    assert history[0]["package_status"] == "UPLOADED"
    assert history[0]["uploaded_at"] == marked["uploaded_at"]
    with pytest.raises(ValueError, match="Unsupported package status"):
        mark_package(result.package_id, "PORTAL_VERIFIED", db=package_db)


def test_validation_failure_records_failed_not_created(package_db, tmp_path):
    with connect(package_db) as con:
        con.execute(
            "UPDATE products SET raw_json=? WHERE asin='B000000001'",
            (json.dumps({"asin": "B000000001"}),),
        )
    with pytest.raises(HandoffValidationError, match="validation failed"):
        SparkCenterPackageService().create(
            store_id="001", asins=["B000000001"], limit=1, out_root=tmp_path,
            package_id="SC_001_FAILED", db=package_db,
        )
    history = list_packages("001", db=package_db)
    failed = next(row for row in history if row["package_id"] == "SC_001_FAILED")
    assert failed["validation_status"] == "FAIL"
    assert failed["package_status"] == "FAILED"


@pytest.mark.parametrize("package_id", ["../escape", "..", "bad/name", "bad\\name"])
def test_package_path_traversal_rejected(package_db, tmp_path, package_id):
    with pytest.raises(ValueError, match="package_id"):
        SparkCenterPackageService().create(
            store_id="001", limit=5, out_root=tmp_path,
            package_id=package_id, db=package_db,
        )


def test_package_collision_does_not_overwrite(package_db, tmp_path):
    service = SparkCenterPackageService()
    service.create(
        store_id="001", limit=5, out_root=tmp_path,
        package_id="SC_001_COLLISION", db=package_db,
    )
    with pytest.raises(FileExistsError):
        service.create(
            store_id="001", limit=5, out_root=tmp_path / "different root",
            package_id="SC_001_COLLISION", db=package_db,
        )


def test_store_special_name_uses_windows_safe_slug(tmp_path):
    db = tmp_path / "special.sqlite3"
    init_db(db)
    seed_store(db, "003", "Bath / 한글:* Sorted?", primary_count=5)
    assert safe_store_folder("003", "Bath / 한글:* Sorted?") == "003_Bath_Sorted"
    result = SparkCenterPackageService().create(
        store_id="003", limit=5, out_root=tmp_path / "경로 with spaces",
        package_id="SC_003_SAFE", db=db,
    )
    assert re.fullmatch(r"[A-Za-z0-9_-]+", result.folder.parent.parent.name)
    assert result.folder.parent.parent.name == "003_Bath_Sorted"


def test_legacy_export_runs_additive_migration(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute(
            """CREATE TABLE export_runs(
            id INTEGER PRIMARY KEY,job_id TEXT NOT NULL UNIQUE,store_id TEXT NOT NULL,
            statuses_json TEXT NOT NULL,output_path TEXT NOT NULL,product_count INTEGER NOT NULL,
            asin_hash TEXT NOT NULL,created_at TEXT NOT NULL,validation_status TEXT NOT NULL)"""
        )
        con.execute(
            "INSERT INTO export_runs VALUES(1,'OLD_JOB','001','[]','old',1,'hash','now','PASS')"
        )
    init_db(db)
    with connect(db) as con:
        columns = {row["name"] for row in con.execute("PRAGMA table_info(export_runs)")}
        row = con.execute("SELECT * FROM export_runs WHERE job_id='OLD_JOB'").fetchone()
    assert {
        "package_id", "target", "store_name", "requested_limit", "package_status",
        "uploaded_at", "note", "portal_package_verified",
    } <= columns
    assert row["package_id"] == "OLD_JOB"
    assert row["target"] == "SPARK_DESKTOP"


def test_api_connector_stays_contract_pending():
    assert api_capability().status == "CONTRACT_PENDING"


def test_cli_package_generation_list_and_mark_smoke(package_db, tmp_path, capsys):
    assert cli_main([
        "--db", str(package_db), "spark-center-package", "--store", "001",
        "--status", "PRIMARY", "--limit", "5", "--out-root", str(tmp_path / "CLI package"),
        "--package-id", "SC_001_CLI",
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["package_id"] == "SC_001_CLI"
    assert created["product_count"] == 5
    assert created["validation_status"] == "PASS"

    assert cli_main([
        "--db", str(package_db), "package-mark", "--package-id", "SC_001_CLI",
        "--status", "UPLOADED", "--note", "manual smoke",
    ]) == 0
    marked = json.loads(capsys.readouterr().out)
    assert marked["package_status"] == "UPLOADED"
    assert marked["uploaded_at"]

    assert cli_main([
        "--db", str(package_db), "package-list", "--store", "001", "--limit", "1",
    ]) == 0
    packages = json.loads(capsys.readouterr().out)
    assert packages[0]["package_id"] == "SC_001_CLI"


def test_generated_spark_center_packages_are_gitignored():
    root = Path(__file__).resolve().parents[1]
    candidate = "exports/spark_center/001_Cabin_Tidy/ready/SC_TEST/000000001.json"
    completed = subprocess.run(
        ["git", "check-ignore", candidate], cwd=root, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0
