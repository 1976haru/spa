import json

import pytest

from shopsource.classifier import classify_store
from shopsource.db import connect, init_db, upsert_store
from shopsource.importer import import_amazon_source


def _write(folder, name, payload):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _profile():
    return {
        "store_id": "001",
        "store_name": "Cabin Tidy",
        "category": "auto_interior",
        "minimum_fit_score": 0,
        "price_bands": [
            {"name": "reserve", "min": 0, "max": 40, "status": "RESERVE_B"},
            {"name": "primary", "min": 40, "max": 100, "status": "PRIMARY"},
            {"name": "high", "min": 100, "max": None, "status": "HIGH_RESERVE"},
        ],
    }


def test_amazon_source_import_preserves_raw_dedupes_and_classifies(tmp_path):
    db = tmp_path / "source.sqlite3"
    inbox = tmp_path / "한글 source folder"
    first = {"asin": "b-source", "title": "Original title", "price": 35, "custom": {"x": 1}}
    second = {"asin": "B-SOURCE", "title": "Latest title", "price": 45, "custom": {"x": 2}}
    _write(inbox / "batch 1", "one.json", first)
    _write(inbox / "batch 2", "two.json", second)

    result = import_amazon_source(inbox, db)
    assert result["inserted"] == 1
    assert result["updated"] == 1
    assert result["duplicates"] == 1
    assert result["batches"] == ["batch 1", "batch 2"]

    upsert_store(_profile(), db)
    classify_store("001", db)
    with connect(db) as con:
        product = con.execute("SELECT id,title,raw_json FROM products").fetchone()
        occurrences = con.execute(
            "SELECT raw_json FROM product_occurrences ORDER BY id"
        ).fetchall()
        decision = con.execute(
            "SELECT final_status FROM store_product_decisions WHERE product_id=?",
            (product["id"],),
        ).fetchone()
    assert product["title"] == "Latest title"
    assert json.loads(product["raw_json"]) == second
    assert [json.loads(row["raw_json"]) for row in occurrences] == [first, second]
    assert decision["final_status"] == "PRIMARY"


def test_amazon_source_reimport_skip_then_detects_new_file(tmp_path):
    db = tmp_path / "source.sqlite3"
    inbox = tmp_path / "inbox"
    _write(inbox, "one.json", {"asin": "B1", "title": "One"})
    assert import_amazon_source(inbox, db)["skipped"] is False
    assert import_amazon_source(inbox, db)["skipped"] is True
    _write(inbox, "two.json", {"asin": "B2", "title": "Two"})
    added = import_amazon_source(inbox, db)
    assert added["skipped"] is False
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


def test_amazon_source_reports_bad_and_sensitive_files(tmp_path):
    db = tmp_path / "source.sqlite3"
    inbox = tmp_path / "inbox"
    _write(inbox, "broken.json", "{not json")
    _write(inbox, "missing.json", {"asin": "B-MISSING"})
    _write(inbox, "secret.json", {"asin": "B-SECRET", "title": "No", "session-token": "x"})
    _write(inbox, "SDK_SESSION_POOL_STATE.json", {"asin": "B-RUNTIME", "title": "No"})
    result = import_amazon_source(inbox, db)
    assert result["files_seen"] == 4
    assert result["invalid"] == 4
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
        codes = {row[0] for row in con.execute("SELECT error_code FROM import_errors")}
    assert codes == {
        "MALFORMED_JSON",
        "MISSING_REQUIRED_FIELD",
        "SENSITIVE_FIELD_REJECTED",
        "RUNTIME_FILE_REJECTED",
    }


def test_amazon_source_empty_folder_has_operator_message(tmp_path):
    inbox = tmp_path / "empty"
    inbox.mkdir()
    with pytest.raises(ValueError, match="source/amazon/inbox"):
        import_amazon_source(inbox, tmp_path / "empty.sqlite3")
