import json
from pathlib import Path

import pytest

from shopsource.connectors import spark_desktop_staging as staging
from shopsource.connectors.spark_center_package import (
    SparkCenterPackageService,
    confirm_spark_desktop_roundtrip,
    list_packages,
    stage_package_for_spark_desktop,
)
from shopsource.db import connect, init_db, upsert_store, utc_now


def make_json_folder(path: Path, count=5):
    path.mkdir(parents=True)
    for i in range(1, count + 1):
        (path / f"{i:09d}.json").write_bytes(
            json.dumps({"asin": f"B{i:09d}", "title": f"Synthetic {i}"}, ensure_ascii=False).encode("utf-8")
        )
    return path


def test_spark_desktop_root_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert staging.spark_desktop_datasets_root() == tmp_path / "spark" / "storage" / "datasets"


def test_stage_creates_new_dataset_folder(tmp_path):
    source = make_json_folder(tmp_path / "source")
    root = tmp_path / "datasets"
    root.mkdir()
    result = staging.stage_dataset(source, "CONTROL_NATIVE_5", datasets_root=root)
    assert result.destination_path == root / "CONTROL_NATIVE_5"
    assert len(list(result.destination_path.glob("*.json"))) == 5


def test_stage_refuses_existing_dataset(tmp_path):
    source = make_json_folder(tmp_path / "source", 1)
    root = tmp_path / "datasets"
    root.mkdir()
    existing = root / "already_here"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep")
    with pytest.raises(staging.DatasetAlreadyExists):
        staging.stage_dataset(source, "already_here", datasets_root=root)
    assert (existing / "keep.txt").read_text() == "keep"


def test_stage_only_copies_json(tmp_path):
    source = make_json_folder(tmp_path / "source", 1)
    root = tmp_path / "datasets"
    root.mkdir()
    result = staging.stage_dataset(source, "only_json", datasets_root=root)
    assert [p.name for p in result.destination_path.iterdir()] == ["000000001.json"]


def test_stage_requires_contiguous_nine_digit_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "1.json").write_text('{"asin":"B1"}')
    with pytest.raises(ValueError, match="contiguous 9-digit"):
        staging.stage_dataset(source, "bad_names", datasets_root=tmp_path)


def test_stage_rejects_invalid_json(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "000000001.json").write_text("not json")
    with pytest.raises(ValueError, match="Invalid product JSON"):
        staging.stage_dataset(source, "bad_json", datasets_root=tmp_path)


def test_stage_preserves_bytes(tmp_path):
    source = make_json_folder(tmp_path / "source", 1)
    raw = (source / "000000001.json").read_bytes()
    root = tmp_path / "datasets"
    root.mkdir()
    result = staging.stage_dataset(source, "byte_copy", datasets_root=root)
    assert (result.destination_path / "000000001.json").read_bytes() == raw


def test_stage_hashes_match(tmp_path):
    source = make_json_folder(tmp_path / "source", 2)
    root = tmp_path / "datasets"
    root.mkdir()
    result = staging.stage_dataset(source, "hashes", datasets_root=root)
    assert result.all_hashes_match
    assert result.source_hashes == result.destination_hashes


def _assert_staging_preserves_protected_sibling(tmp_path, protected):
    source = make_json_folder(tmp_path / "source", 1)
    root = tmp_path / "spark" / "storage" / "datasets"
    root.mkdir(parents=True)
    sibling = root.parent / protected
    sibling.mkdir()
    marker = sibling / "existing.bin"
    marker.write_bytes(b"unchanged")
    staging.stage_dataset(source, "safe", datasets_root=root)
    assert marker.read_bytes() == b"unchanged"
    assert list(sibling.iterdir()) == [marker]


def test_stage_does_not_touch_request_queues(tmp_path):
    _assert_staging_preserves_protected_sibling(tmp_path, "request_queues")


def test_stage_does_not_touch_key_value_stores(tmp_path):
    _assert_staging_preserves_protected_sibling(tmp_path, "key_value_stores")


def test_stage_control_native_five_contract(tmp_path):
    source_name = "CONTROL_NATIVE_5_20261001_185948"
    source = make_json_folder(tmp_path / source_name)
    root = tmp_path / "datasets"
    root.mkdir()
    result = staging.stage_dataset(source, source_name, datasets_root=root)
    assert result.product_count == 5
    assert sorted(p.name for p in result.destination_path.iterdir()) == [f"{i:09d}.json" for i in range(1, 6)]


def test_stage_browser_five_contract(tmp_path):
    source_name = "SC_001_20261001_101734_ab09"
    source = make_json_folder(tmp_path / source_name)
    root = tmp_path / "datasets"
    root.mkdir()
    result = staging.stage_dataset(source, source_name, datasets_root=root)
    assert result.product_count == 5
    assert sorted(p.name for p in result.destination_path.iterdir()) == [f"{i:09d}.json" for i in range(1, 6)]


def seed_package_db(db):
    init_db(db)
    upsert_store({"store_id": "001", "store_name": "Test Store", "category": "test"}, db)
    with connect(db) as con:
        for i in range(1, 6):
            product = {"asin": f"B{i:09d}", "title": f"Test product {i}", "brand": "Fixture", "price": 10}
            cur = con.execute(
                "INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                (product["asin"], product["title"], json.dumps(product), utc_now(), utc_now()),
            )
            con.execute(
                "INSERT INTO store_product_decisions(store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at) VALUES(?,?,?,?,?,?,?)",
                ("001", cur.lastrowid, "OK", "SAFE", "PRIMARY", "PRIMARY", utc_now()),
            )


def test_desktop_roundtrip_flag_defaults_false(tmp_path):
    db = tmp_path / "app.sqlite3"
    seed_package_db(db)
    package = SparkCenterPackageService().create(
        store_id="001", statuses=["PRIMARY"], limit=5, db=db,
        out_root=tmp_path / "exports", package_id="SC_001_DEFAULT_UNVERIFIED",
    )
    assert list_packages("001", db=db)[0]["spark_desktop_roundtrip_verified"] is False


def test_desktop_roundtrip_manual_confirmation_sets_true(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite3"
    seed_package_db(db)
    source_root = tmp_path / "exports"
    package = SparkCenterPackageService().create(
        store_id="001", statuses=["PRIMARY"], limit=5, db=db,
        out_root=source_root, package_id="SC_001_TEST_DESKTOP",
    )
    row = list_packages("001", db=db)[0]
    assert row["spark_desktop_roundtrip_verified"] is False
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    monkeypatch.setattr(staging, "spark_desktop_datasets_root", lambda: datasets)
    staged = stage_package_for_spark_desktop(package.package_id, db)
    assert staged["all_hashes_match"] and staged["product_count"] == 5
    with connect(db) as con:
        before = con.execute(
            "SELECT portal_package_verified,spark_desktop_roundtrip_verified FROM export_runs WHERE package_id=?",
            (package.package_id,),
        ).fetchone()
    assert tuple(before) == (0, 0)
    confirmed = confirm_spark_desktop_roundtrip(package.package_id, confirmed=True, db=db)
    assert confirmed["spark_desktop_roundtrip_verified"] is True
    with connect(db) as con:
        after = con.execute(
            "SELECT portal_package_verified,spark_desktop_roundtrip_verified FROM export_runs WHERE package_id=?",
            (package.package_id,),
        ).fetchone()
    assert tuple(after) == (0, 1)


def test_desktop_verification_does_not_set_portal_verified(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite3"
    seed_package_db(db)
    package = SparkCenterPackageService().create(
        store_id="001", statuses=["PRIMARY"], limit=5, db=db,
        out_root=tmp_path / "exports", package_id="SC_001_SEPARATE_FLAGS",
    )
    root = tmp_path / "datasets"
    root.mkdir()
    monkeypatch.setattr(staging, "spark_desktop_datasets_root", lambda: root)
    stage_package_for_spark_desktop(package.package_id, db)
    confirm_spark_desktop_roundtrip(package.package_id, confirmed=True, db=db)
    with connect(db) as con:
        row = con.execute(
            "SELECT portal_package_verified,spark_desktop_roundtrip_verified FROM export_runs WHERE package_id=?",
            (package.package_id,),
        ).fetchone()
    assert row["portal_package_verified"] == 0
    assert row["spark_desktop_roundtrip_verified"] == 1


def test_desktop_verification_does_not_set_shopify_verified(tmp_path, monkeypatch):
    db = tmp_path / "app.sqlite3"
    seed_package_db(db)
    package = SparkCenterPackageService().create(
        store_id="001", statuses=["PRIMARY"], limit=5, db=db,
        out_root=tmp_path / "exports", package_id="SC_001_NO_SHOPIFY_FLAG",
    )
    root = tmp_path / "datasets"
    root.mkdir()
    monkeypatch.setattr(staging, "spark_desktop_datasets_root", lambda: root)
    stage_package_for_spark_desktop(package.package_id, db)
    confirm_spark_desktop_roundtrip(package.package_id, confirmed=True, db=db)
    manifest = json.loads(package.manifest_path.read_text(encoding="utf-8"))
    assert manifest["shopify_upload_verified"] is False


def test_packages_ui_has_spark_desktop_actions():
    source = Path(__file__).parents[1] / "src/shopsource/ui/v2.py"
    text = source.read_text(encoding="utf-8")
    assert "Spark Desktop에 설치" in text
    assert "Desktop 로드 확인" in text
    assert "기존 dataset은 덮어쓰지 않습니다" in text
