import json
from pathlib import Path

from shopsource.classifier import classify_store, manual_override
from shopsource.db import connect, init_db, upsert_store
from shopsource.importer import import_spark

ROOT = Path(__file__).resolve().parents[1]


def make_storage(tmp_path):
    job = tmp_path / "datasets" / "test_job"
    job.mkdir(parents=True)
    fixtures = ROOT / "tests" / "fixtures"
    for f in fixtures.glob("*.json"):
        (job / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def test_import_dedupe_and_store_classification(tmp_path):
    db = tmp_path / "test.sqlite3"
    init_db(db)
    profile = json.loads((ROOT / "stores" / "001_cabin_tidy.json").read_text(encoding="utf-8"))
    upsert_store(profile, db)
    storage = make_storage(tmp_path / "storage")

    stats = import_spark(storage, db)
    assert stats["inserted"] == 3
    assert stats["rows_read"] == 3

    result = classify_store("001", db)
    assert result["processed"] == 3

    with connect(db) as con:
        rows = con.execute(
            """
            SELECT p.asin,d.final_status,d.price_status,d.risk_status
            FROM store_product_decisions d JOIN products p ON p.id=d.product_id
            ORDER BY p.asin
            """
        ).fetchall()
    got = {r["asin"]: (r["final_status"], r["price_status"], r["risk_status"]) for r in rows}
    assert got["BTEST00001"][0] == "PRIMARY"
    assert got["BTEST00002"][0] == "RESERVE_B"
    assert got["BTEST00003"][0] == "REVIEW"

    # Re-importing an unchanged source is skipped rather than duplicating work.
    again = import_spark(storage, db)
    assert again["skipped"] is True


def test_manual_override_survives_reclassification(tmp_path):
    db = tmp_path / "override.sqlite3"
    init_db(db)
    profile = json.loads((ROOT / "stores" / "001_cabin_tidy.json").read_text(encoding="utf-8"))
    upsert_store(profile, db)
    storage = make_storage(tmp_path / "storage2")
    import_spark(storage, db)
    classify_store("001", db)
    manual_override("001", "BTEST00002", "PRIMARY", "manual promotion", db)

    # Change the store rule so automatic output would still not be PRIMARY.
    profile["minimum_fit_score"] = 99
    upsert_store(profile, db)
    classify_store("001", db)

    with connect(db) as con:
        row = con.execute(
            """SELECT d.final_status,d.manual_override,d.memo FROM store_product_decisions d
               JOIN products p ON p.id=d.product_id
               WHERE d.store_id='001' AND p.asin='BTEST00002'"""
        ).fetchone()
    assert row["final_status"] == "PRIMARY"
    assert row["manual_override"] == 1
    assert row["memo"] == "manual promotion"
