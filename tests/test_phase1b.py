import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from shopsource.classifier import classify_store, clear_manual_override, manual_override
from shopsource.connectors.manifest import build_manifest
from shopsource.connectors.spark_handoff import SparkHandoffConnector
from shopsource.db import connect, init_db, upsert_store
from shopsource.importer import import_spark
from shopsource.schema_probe import probe_schema


def profile(primary_min=40, primary_max=100):
    return {
        "store_id": "001",
        "store_name": "Cabin Tidy",
        "category": "auto_interior",
        "minimum_fit_score": 0,
        "price_bands": [
            {"name": "below", "min": 0, "max": primary_min, "status": "RESERVE_B"},
            {"name": "primary", "min": primary_min, "max": primary_max, "status": "PRIMARY"},
            {"name": "above", "min": primary_max, "max": None, "status": "HIGH_RESERVE"},
        ],
    }


def write_storage(root: Path, products: list[dict], malformed=False) -> Path:
    job = root / "datasets" / "job-한글"
    job.mkdir(parents=True)
    for index, product in enumerate(products):
        (job / f"{index:09}.json").write_text(
            json.dumps(product, ensure_ascii=False), encoding="utf-8"
        )
    if malformed:
        (job / "broken.json").write_text("{broken", encoding="utf-8")
    return root


def product(asin: str, price: float) -> dict:
    return {"asin": asin, "title": f"Organizer {asin}", "price": price, "_sourceUrl": "synthetic"}


def decision(db: Path, asin: str):
    with connect(db) as con:
        return con.execute(
            """
            SELECT p.id product_id,d.auto_status,d.final_status,d.manual_override
            FROM products p JOIN store_product_decisions d ON d.product_id=p.id
            WHERE p.asin=? AND d.store_id='001'
            """,
            (asin,),
        ).fetchone()


def test_dynamic_price_change_reclassifies_same_master_product(tmp_path):
    db = tmp_path / "dynamic.sqlite3"
    init_db(db)
    upsert_store(profile(), db)
    storage = write_storage(tmp_path / "Spark 데이터 with spaces", [product("B35", 35)])
    import_spark(storage, db)
    classify_store("001", db)
    before = decision(db, "B35")
    assert before["final_status"] == "RESERVE_B"

    upsert_store(profile(30, 120), db)
    classify_store("001", db)
    after = decision(db, "B35")
    assert after["final_status"] == "PRIMARY"
    assert after["product_id"] == before["product_id"]


def test_all_prices_remain_in_master_and_only_decisions_change(tmp_path):
    prices = [10, 29, 35, 39, 40, 100, 101, 150]
    db = tmp_path / "prices.sqlite3"
    init_db(db)
    upsert_store(profile(), db)
    storage = write_storage(
        tmp_path / "storage", [product(f"B{price:03}", price) for price in prices]
    )
    import_spark(storage, db)
    classify_store("001", db)
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == len(prices)
        statuses = dict(
            con.execute(
                """SELECT p.asin,d.final_status FROM products p
                JOIN store_product_decisions d ON d.product_id=p.id"""
            ).fetchall()
        )
    assert set(statuses) == {f"B{price:03}" for price in prices}
    assert statuses["B010"] == "RESERVE_B"
    assert statuses["B040"] == "PRIMARY"
    assert statuses["B100"] == "HIGH_RESERVE"


def test_manual_override_survives_then_can_be_cleared(tmp_path):
    db = tmp_path / "override.sqlite3"
    init_db(db)
    upsert_store(profile(), db)
    import_spark(write_storage(tmp_path / "storage", [product("B035", 35)]), db)
    classify_store("001", db)
    manual_override("001", "B035", "PRIMARY", "promoted", db)
    classify_store("001", db)
    assert decision(db, "B035")["final_status"] == "PRIMARY"
    clear_manual_override("001", "B035", db)
    classify_store("001", db)
    row = decision(db, "B035")
    assert row["final_status"] == "RESERVE_B"
    assert row["manual_override"] == 0


def test_folder_zip_idempotency_and_malformed_report(tmp_path):
    db = tmp_path / "import.sqlite3"
    storage = write_storage(
        tmp_path / "원본 storage folder", [product("B001", 35), product("B001", 36)], malformed=True
    )
    first = import_spark(storage, db)
    assert first["inserted"] == 1
    assert first["invalid"] == 1
    assert import_spark(storage, db)["skipped"] is True
    forced = import_spark(storage, db, allow_reimport=True)
    assert forced["inserted"] == 0
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM product_occurrences").fetchone()[0] == 2
        assert con.execute(
            "SELECT COUNT(*) FROM product_occurrences WHERE import_run_id IS NULL"
        ).fetchone()[0] == 0
        raw_prices = {
            json.loads(row["raw_json"])["price"]
            for row in con.execute("SELECT raw_json FROM product_occurrences")
        }
        assert raw_prices == {35, 36}
        error = con.execute("SELECT error_code,source_file FROM import_errors").fetchone()
    assert error["error_code"] == "MALFORMED_JSON"
    assert error["source_file"] == "broken.json"

    archive = tmp_path / "Spark 한글 sample.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for file in storage.rglob("*"):
            if file.is_file():
                zf.write(file, Path("wrapper") / file.relative_to(storage))
    zipped = import_spark(archive, db)
    assert zipped["inserted"] == 0
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM product_occurrences").fetchone()[0] == 2


def test_schema_probe_is_read_only_and_reports_shape(tmp_path):
    storage = write_storage(tmp_path / "probe", [product("B001", 35), product("B001", 36)], malformed=True)
    broken = storage / "datasets" / "job-한글" / "broken.json"
    before = broken.read_bytes()
    report = probe_schema(storage)
    assert report["file_count"] == 3
    assert report["json_count"] == 2
    assert report["product_count"] == 2
    assert report["unique_asin"] == 1
    assert report["duplicate_asin"] == 1
    assert report["job_ids"] == ["job-한글"]
    assert report["fields"]["price"]["types"] == {"number": 2}
    assert broken.read_bytes() == before


def test_handoff_capability_and_manifest_have_no_credentials():
    connector = SparkHandoffConnector()
    assert connector.capability.status == "DATASET_LOAD_VERIFIED"
    assert connector.capability.dataset_folder_load_verified is True
    assert connector.capability.shopify_upload_verified is False
    manifest = build_manifest(
        store_id="001", store_name="Cabin Tidy", asins=["B2", "B1", "B1"],
        source_job_ids=["job2", "job1"], filter_profile={"statuses": ["PRIMARY"]},
    )
    assert manifest["product_count"] == 3
    assert manifest["asin_count"] == 2
    assert "token" not in json.dumps(manifest).lower()


def test_scaling_indexes_exist(tmp_path):
    db = tmp_path / "indexes.sqlite3"
    init_db(db)
    with connect(db) as con:
        indexes = {
            row["name"]
            for table in (
                "products", "product_occurrences", "store_product_decisions", "import_errors",
                "export_runs",
            )
            for row in con.execute(f"PRAGMA index_list({table})")
        }
    assert {
        "idx_decisions_store_status", "idx_decisions_product", "idx_occurrence_job",
        "idx_occurrence_product", "idx_occurrence_import_run", "idx_import_errors_run",
        "idx_export_runs_store_created",
    } <= indexes


def test_existing_occurrence_table_gets_additive_raw_json_migration(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute(
            """CREATE TABLE product_occurrences (
            id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, import_run_id INTEGER,
            job_id TEXT, source_file TEXT, collected_at TEXT, source_url TEXT, list_page INTEGER,
            UNIQUE(product_id, job_id, source_file))"""
        )
    init_db(db)
    with connect(db) as con:
        columns = {row["name"] for row in con.execute("PRAGMA table_info(product_occurrences)")}
    assert "raw_json" in columns


@pytest.mark.skip(reason="Shopify Location selection and actual upload remain unverified")
def test_spark_shopify_upload_pending():
    """Activation requires Spark UI Location selection and test upload evidence."""
