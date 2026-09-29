import json
from pathlib import Path

import pytest

from shopsource.classifier import manual_override
from shopsource.cli import main as cli_main
from shopsource.connectors.spark_handoff import (
    HandoffValidationError,
    SparkHandoffConnector,
)
from shopsource.db import connect, init_db, upsert_store, utc_now


@pytest.fixture
def handoff_db(tmp_path):
    db = tmp_path / "handoff.sqlite3"
    init_db(db)
    upsert_store({"store_id": "001", "store_name": "Cabin Tidy", "category": "test"}, db)
    rows = [
        ("B001", "PRIMARY"),
        ("B002", "PRIMARY"),
        ("B003", "PRIMARY"),
        ("B004", "PRIMARY"),
        ("B005", "PRIMARY"),
        ("B006", "RESERVE_B"),
        ("B007", "RESTRICTED"),
    ]
    with connect(db) as con:
        for asin, status in rows:
            payload = {
                "url": f"https://example.invalid/{asin}",
                "asin": asin,
                "title": f"Product fallback {asin}",
                "brand": "Synthetic",
                "price": 50,
                "options": {"Color": ["Black"]},
                "images": ["https://example.invalid/image.jpg"],
                "_sourceUrl": "synthetic",
            }
            cur = con.execute(
                """INSERT INTO products(
                asin,title,raw_json,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?)""",
                (asin, payload["title"], json.dumps(payload), utc_now(), utc_now()),
            )
            product_id = cur.lastrowid
            con.execute(
                """INSERT INTO store_product_decisions(
                store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at
                ) VALUES(?,?,?,?,?,?,?)""",
                ("001", product_id, status, "SAFE", status, status, utc_now()),
            )
            if asin != "B002":
                occurrence = dict(payload)
                occurrence["title"] = f"Latest occurrence {asin}"
                con.execute(
                    """INSERT INTO product_occurrences(
                    product_id,job_id,source_file,collected_at,raw_json
                    ) VALUES(?,?,?,?,?)""",
                    (product_id, "source-new", f"{asin}.json", "2026-09-29T12:00:00+00:00", json.dumps(occurrence)),
                )
            if asin == "B001":
                older = dict(payload)
                older["title"] = "Older occurrence B001"
                con.execute(
                    """INSERT INTO product_occurrences(
                    product_id,job_id,source_file,collected_at,raw_json
                    ) VALUES(?,?,?,?,?)""",
                    (product_id, "source-old", "old-B001.json", "2026-09-28T12:00:00+00:00", json.dumps(older)),
                )
    return db


def test_primary_five_export_structure_payload_and_validation(handoff_db, tmp_path):
    out_root = tmp_path / "한글 Spark output with spaces" / "jobs"
    result = SparkHandoffConnector().export(
        store_id="001", statuses=["PRIMARY"], limit=5, out_root=out_root,
        job_id="TEST_5ITEMS", db=handoff_db,
    )
    assert result.validation_status == "PASS"
    assert result.product_count == result.asin_count == 5
    files = sorted(result.folder.glob("*.json"))
    assert [file.name for file in files] == [f"{n:09}.json" for n in range(1, 6)]
    payloads = [json.loads(file.read_text(encoding="utf-8")) for file in files]
    assert [payload["asin"] for payload in payloads] == ["B001", "B002", "B003", "B004", "B005"]
    assert len({payload["asin"] for payload in payloads}) == 5
    assert payloads[0]["title"] == "Latest occurrence B001"
    assert payloads[1]["title"] == "Product fallback B002"
    forbidden = {"fit_score", "final_status", "manual_override", "store_id", "risk_status", "memo"}
    assert all(not forbidden.intersection(payload) for payload in payloads)
    assert result.manifest_path.parent != result.folder
    assert result.validation_report_path.parent != result.folder
    assert {path.name for path in result.folder.iterdir()} == {file.name for file in files}
    assert not (out_root / "request_queues").exists()
    assert not (out_root / "key_value_stores").exists()
    assert not any("SESSION" in path.name or "STATISTICS" in path.name for path in result.folder.iterdir())

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    report = json.loads(result.validation_report_path.read_text(encoding="utf-8"))
    assert manifest["capability_status"] == "DATASET_LOAD_VERIFIED"
    assert manifest["shopify_upload_verified"] is False
    assert manifest["asin_count"] == 5
    assert manifest["source_job_ids"] == ["source-new"]
    manifest_text = json.dumps(manifest).lower()
    assert all(term not in manifest_text for term in (
        "accesstoken", "session-token", "aws-waf-token", "cookie", "password", "secret"
    ))
    assert report["status"] == "PASS"
    with connect(handoff_db) as con:
        run = con.execute("SELECT * FROM export_runs WHERE job_id='TEST_5ITEMS'").fetchone()
    assert run["validation_status"] == "PASS"
    assert run["store_id"] == "001"


def test_status_manual_override_and_restricted_gate(handoff_db, tmp_path):
    connector = SparkHandoffConnector()
    with pytest.raises(ValueError, match="matching Store Decision/status"):
        connector.export(
            store_id="001", statuses=["PRIMARY"], asins=["B006"],
            out_root=tmp_path / "a", job_id="RESERVE_NOT_PRIMARY", db=handoff_db,
        )
    reserve = connector.export(
        store_id="001", statuses=["RESERVE_B"], asins=["B006"],
        out_root=tmp_path / "b", job_id="RESERVE_EXPLICIT", db=handoff_db,
    )
    assert reserve.product_count == 1

    manual_override("001", "B006", "PRIMARY", db=handoff_db)
    promoted = connector.export(
        store_id="001", statuses=["PRIMARY"], asins=["B006"],
        out_root=tmp_path / "c", job_id="MANUAL_PRIMARY", db=handoff_db,
    )
    assert promoted.product_count == 1

    with pytest.raises(ValueError, match="allow_restricted"):
        connector.export(
            store_id="001", statuses=["RESTRICTED"], out_root=tmp_path / "d",
            job_id="RESTRICTED_BLOCKED", db=handoff_db,
        )
    restricted = connector.export(
        store_id="001", statuses=["RESTRICTED"], allow_restricted=True,
        out_root=tmp_path / "e", job_id="RESTRICTED_ALLOWED", db=handoff_db,
    )
    assert restricted.product_count == 1


@pytest.mark.parametrize("job_id", ["../escape", "..", "bad/name", "bad\\name"])
def test_job_id_path_traversal_is_rejected(handoff_db, tmp_path, job_id):
    with pytest.raises(ValueError, match="job_id"):
        SparkHandoffConnector().export(
            store_id="001", out_root=tmp_path, job_id=job_id, db=handoff_db
        )


def test_existing_job_folder_collision_is_rejected(handoff_db, tmp_path):
    existing = tmp_path / "COLLISION"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(FileExistsError):
        SparkHandoffConnector().export(
            store_id="001", out_root=tmp_path, job_id="COLLISION", db=handoff_db
        )
    assert marker.read_text(encoding="utf-8") == "do not overwrite"


def test_invalid_payload_creates_fail_report_not_success(handoff_db, tmp_path):
    with connect(handoff_db) as con:
        cur = con.execute(
            """INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?)""",
            ("B008", "DB title", json.dumps({"asin": "B008"}), utc_now(), utc_now()),
        )
        con.execute(
            """INSERT INTO store_product_decisions(
            store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at
            ) VALUES(?,?,?,?,?,?,?)""",
            ("001", cur.lastrowid, "PRIMARY", "SAFE", "PRIMARY", "PRIMARY", utc_now()),
        )
    with pytest.raises(HandoffValidationError) as caught:
        SparkHandoffConnector().export(
            store_id="001", asins=["B008"], out_root=tmp_path,
            job_id="INVALID_PAYLOAD", db=handoff_db,
        )
    result = caught.value.result
    report = json.loads(result.validation_report_path.read_text(encoding="utf-8"))
    assert report["status"] == "FAIL"
    assert any("missing required fields title" in error for error in report["errors"])
    assert list(result.folder.iterdir()) == []


def test_cli_end_to_end_smoke(handoff_db, tmp_path, capsys):
    assert cli_main([
        "--db", str(handoff_db), "spark-handoff", "--store", "001",
        "--status", "PRIMARY", "--limit", "5", "--out-root", str(tmp_path / "CLI 출력"),
        "--job-id", "CLI_SMOKE",
    ]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["job_id"] == "CLI_SMOKE"
    assert summary["product_count"] == 5
    assert summary["validation_status"] == "PASS"
    assert Path(summary["folder"]).is_dir()
