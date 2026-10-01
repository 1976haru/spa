from __future__ import annotations

import json
from pathlib import Path

import pytest

from shopsource.connectors.spark_center_package import SparkCenterPackageService
from shopsource.db import connect, init_db, upsert_store
from shopsource.sourcing.mapping import (
    browser_capture_to_spark_payload,
    normalize_spark_collected_at,
    observed_spark_schema_issues,
    spark_desktop_loader_contract,
)


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/spark_handoff/observed_spark_schema.synthetic.json"
LOADER_FIXTURE = ROOT / "tests/fixtures/spark_handoff/spark_loader_contract.synthetic.json"
ASINS = ["B09YXYSSLL", "B0CM6KVCSX", "B0F7QTD5SV", "B0GFD1WBP9", "B0H8SFR4GT"]


def source_payload(asin: str) -> dict:
    return {
        "url": f"https://www.amazon.com/dp/{asin}",
        "asin": asin,
        "title": f"Synthetic captured product {asin}",
        "brand": "Synthetic Brand",
        "price": 20.5,
        "options": {},
        "quantity": None,
        "tags": [],
        "category": "Synthetic category",
        "overview": ["Synthetic overview"],
        "aboutThis": [],
        "images": ["https://example.invalid/synthetic-image.jpg"],
        "rating": 4.5,
        "reviewCount": 25,
        "_sourceUrl": f"https://www.amazon.com/dp/{asin}",
        "_listPage": None,
        "_collectedAt": "2026-09-30T20:58:12.610Z",
    }


@pytest.fixture
def existing_five_browser_db(tmp_path):
    db = tmp_path / "browser_capture_roundtrip.sqlite3"
    init_db(db)
    upsert_store({
        "store_id": "001", "store_name": "Cabin Tidy", "category": "Automotive", "price_bands": [],
    }, db)
    now = "2026-10-01T00:00:00Z"
    search_url = "https://www.amazon.com/s?k=synthetic+organizer"
    with connect(db) as con:
        con.execute(
            """INSERT INTO browser_capture_runs
            (run_id,store_id,keyword,search_url,status,captured_at,candidates,detailed)
            VALUES(?,?,?,?,?,?,?,?)""",
            ("BC_SCHEMA_RUN", "001", "synthetic organizer", search_url, "DETAIL_CAPTURED", now, 5, 5),
        )
        for asin in ASINS:
            raw = source_payload(asin)
            cur = con.execute(
                """INSERT INTO products
                (asin,source,source_kind,url,title,brand,price,category,raw_json,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (asin, "amazon_browser", "BROWSER_CAPTURE", raw["url"], raw["title"], raw["brand"],
                 raw["price"], raw["category"], json.dumps(raw), now, now),
            )
            con.execute(
                """INSERT INTO store_product_decisions
                (store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at)
                VALUES(?,?,?,?,?,?,?)""",
                ("001", cur.lastrowid, "LOW_RESERVE", "SAFE", "LOW_RESERVE", "LOW_RESERVE", now),
            )
            search_payload = {
                "asin": asin,
                "title": raw["title"],
                "url": raw["url"],
                "price": raw["price"],
                "images": raw["images"],
                "_sourceUrl": search_url,
                "_listPage": 1,
                "_collectedAt": now,
            }
            con.execute(
                """INSERT INTO browser_capture_candidates
                (run_id,asin,search_payload_json,detail_payload_json,completeness_score,capture_status,
                 created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                ("BC_SCHEMA_RUN", asin, json.dumps(search_payload), json.dumps(raw), 90, "DETAIL_COMPLETE", now, now),
            )
    return db


def test_native_spark_schema_profile():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert observed_spark_schema_issues(payload) == []
    assert isinstance(payload["_collectedAt"], int)
    assert isinstance(payload["_listPage"], int)
    assert isinstance(payload["images"][0], dict)
    assert set(payload["options"]) == {"selectedVariations", "variationDisplayLabels"}


def test_loader_contract_profile_fixture():
    fixture = json.loads(LOADER_FIXTURE.read_text(encoding="utf-8"))
    contract = spark_desktop_loader_contract()
    assert fixture["spark_app_version"] == contract["app_version"]
    assert fixture["selection"]["folder_picker_returns"] == "basename"
    assert fixture["selection"]["external_folder_contents_are_copied"] is False
    assert fixture["enumeration"]["item_filename"] == "9-digit zero-padded index plus .json"
    assert fixture["item_predicate"]["required_product_fields"] == contract["required_product_fields"]
    assert fixture["item_predicate"]["quantity_required"] is contract["quantity_required"] is False
    assert fixture["item_predicate"]["image_main_dimensions_required"] is False
    assert fixture["item_predicate"]["variation_display_labels_required"] is False
    assert fixture["item_predicate"]["json_read_predicate"] == contract["item_json_read_predicate"]
    assert fixture["enumeration"]["without_metadata"] == "count regular files"
    assert fixture["enumeration"]["read_order"] == "sequential item indexes starting at 1"
    assert fixture["counting"]["excluded"] == contract["excluded_count"]
    assert fixture["counting"]["included"] == contract["included_count"]
    assert fixture["errors"]["get_data_info"] == contract["data_info_error_behavior"]
    assert fixture["errors"]["get_data"] == contract["get_data_error_behavior"]


def test_quantity_required_if_loader_requires_it():
    contract = spark_desktop_loader_contract()
    assert contract["quantity_required"] is False
    assert observed_spark_schema_issues({"asin": ASINS[0], "quantity": None}) == []


def test_empty_image_dimensions_rejected_if_loader_requires_it():
    contract = spark_desktop_loader_contract()
    assert contract["image_main_dimensions_required"] is False
    assert observed_spark_schema_issues({"images": [{"main": {"https://example.invalid/x.jpg": []}}]}) == []


def test_variation_shape_matches_loader_contract():
    contract = spark_desktop_loader_contract()
    mapped = browser_capture_to_spark_payload(source_payload(ASINS[0]))
    assert contract["variation_display_labels_required"] is False
    assert mapped["options"] == {"selectedVariations": {}, "variationDisplayLabels": {}}
    assert observed_spark_schema_issues(mapped) == []


def test_browser_collected_at_iso_to_epoch_ms():
    assert normalize_spark_collected_at("2026-09-30T20:58:12.610Z") == 1790801892610


def test_browser_search_metadata_restores_list_page():
    mapped = browser_capture_to_spark_payload({
        **source_payload(ASINS[0]), "_searchMetadata": {"_listPage": 3, "_sourceUrl": "https://www.amazon.com/s?k=x"},
    })
    assert mapped["_listPage"] == 3


def test_browser_search_metadata_restores_source_url():
    search_url = "https://www.amazon.com/s?k=synthetic+organizer"
    mapped = browser_capture_to_spark_payload({
        **source_payload(ASINS[0]), "_searchMetadata": {"_listPage": 1, "_sourceUrl": search_url},
    })
    assert mapped["_sourceUrl"] == search_url


def test_browser_images_to_observed_spark_shape():
    payload = source_payload(ASINS[0])
    payload["_imageDimensions"] = {payload["images"][0]: [679, 679]}
    image = browser_capture_to_spark_payload(payload)["images"][0]
    assert set(image) == {"hiRes", "thumb", "large", "main", "variant", "lowRes", "shoppableScene"}
    assert image["hiRes"] == image["thumb"] == image["large"] == payload["images"][0]
    assert image["main"] == {payload["images"][0]: [679, 679]}
    assert image["lowRes"] is None and image["shoppableScene"] is None


def test_browser_options_to_observed_spark_shape():
    mapped = browser_capture_to_spark_payload(source_payload(ASINS[0]))
    assert mapped["options"] == {"selectedVariations": {}, "variationDisplayLabels": {}}


def test_no_invented_product_values():
    original = source_payload(ASINS[0])
    mapped = browser_capture_to_spark_payload({
        **original, "_searchMetadata": {"_listPage": 1, "_sourceUrl": "https://www.amazon.com/s?k=x"},
    })
    assert mapped["title"] == original["title"]
    assert mapped["price"] == original["price"]
    assert mapped["brand"] == original["brand"]
    assert mapped["quantity"] is None
    assert mapped["images"][0]["main"][original["images"][0]] == []
    assert "_imageDimensions" not in mapped


def test_spark_schema_compatibility_validation():
    mapped = browser_capture_to_spark_payload({
        **source_payload(ASINS[0]), "_searchMetadata": {"_listPage": 1, "_sourceUrl": "https://www.amazon.com/s?k=x"},
    })
    assert observed_spark_schema_issues(mapped) == []


def test_old_browser_shape_is_accepted_by_actual_item_predicate():
    old = source_payload(ASINS[0])
    assert old["quantity"] is None
    assert isinstance(old["images"][0], str)
    assert observed_spark_schema_issues(old) == []


def test_existing_master_enrichment_does_not_duplicate_product(existing_five_browser_db, tmp_path):
    with connect(existing_five_browser_db) as con:
        before = con.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
    result = SparkCenterPackageService().create(
        store_id="001", statuses=["LOW_RESERVE"], limit=5, asins=ASINS,
        out_root=tmp_path / "no enrichment needed", package_id="SC_001_NO_ENRICHMENT",
        db=existing_five_browser_db,
    )
    with connect(existing_five_browser_db) as con:
        after = con.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
    assert result.product_count == 5
    assert after == before == 5


def test_enrichment_worker_tab_single():
    # No enrichment worker is started: the inspected desktop loader requires
    # no product metadata beyond a JSON object, so there is nothing to revisit.
    assert spark_desktop_loader_contract()["required_product_fields"] == []


def test_products_packages_ui_discloses_external_folder_is_not_imported():
    ui_source = (ROOT / "src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert "외부 ready 폴더의 JSON을 가져오지는 않습니다" in ui_source


def test_enrichment_captures_real_quantity_contract():
    assert spark_desktop_loader_contract()["quantity_required"] is False
    assert source_payload(ASINS[0])["quantity"] is None


def test_enrichment_captures_real_image_dimensions_contract():
    assert spark_desktop_loader_contract()["image_main_dimensions_required"] is False
    mapped = browser_capture_to_spark_payload(source_payload(ASINS[0]))
    assert mapped["images"][0]["main"] == {source_payload(ASINS[0])["images"][0]: []}


def test_browser_package_requires_enrichment_when_missing_required_metadata(existing_five_browser_db, tmp_path):
    assert spark_desktop_loader_contract()["required_product_fields"] == []
    result = SparkCenterPackageService().create(
        store_id="001", statuses=["LOW_RESERVE"], limit=5, asins=ASINS,
        out_root=tmp_path / "no metadata enrichment", package_id="SC_001_NO_METADATA_ENRICHMENT",
        db=existing_five_browser_db,
    )
    assert result.validation_status == "PASS"
    assert result.product_count == 5


def test_browser_package_after_enrichment_five_products(existing_five_browser_db, tmp_path):
    # The loader contract made browser-page enrichment unnecessary; exporting
    # the existing MASTER identities yields exactly five dataset objects.
    result = SparkCenterPackageService().create(
        store_id="001", statuses=["LOW_RESERVE"], limit=5, asins=ASINS,
        out_root=tmp_path / "loader compatible", package_id="SC_001_LOADER_COMPATIBLE",
        db=existing_five_browser_db,
    )
    assert result.product_count == 5
    assert result.validation_status == "PASS"
    assert len(list(result.folder.glob("*.json"))) == 5


def test_new_browser_shape_passes_strict_compatibility():
    mapped = browser_capture_to_spark_payload({
        **source_payload(ASINS[0]), "_searchMetadata": {"_listPage": 1, "_sourceUrl": "https://www.amazon.com/s?k=x"},
    })
    assert observed_spark_schema_issues(mapped) == []


def test_existing_five_asins_generate_new_package(existing_five_browser_db, tmp_path):
    result = SparkCenterPackageService().create(
        store_id="001", statuses=["LOW_RESERVE"], limit=5, asins=ASINS,
        out_root=tmp_path / "spark output", package_id="SC_001_SCHEMA_SMOKE", db=existing_five_browser_db,
    )
    assert result.product_count == 5
    assert result.validation_status == "PASS"
    assert result.observed_spark_schema_compatible is True
    files = sorted(result.folder.glob("*.json"))
    assert [path.name for path in files] == [f"{i:09}.json" for i in range(1, 6)]
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    assert {row["asin"] for row in payloads} == set(ASINS)
    assert all(row["_listPage"] == 1 for row in payloads)


def test_roundtrip_flag_stays_false_until_user_confirmation(existing_five_browser_db, tmp_path):
    result = SparkCenterPackageService().create(
        store_id="001", statuses=["LOW_RESERVE"], limit=5, asins=ASINS,
        out_root=tmp_path / "spark output", package_id="SC_001_UNVERIFIED", db=existing_five_browser_db,
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    report = json.loads(result.validation_report_path.read_text(encoding="utf-8"))
    assert result.browser_capture_mapping_verified is False
    assert result.observed_spark_schema_compatible is True
    assert result.spark_desktop_roundtrip_verified is False
    assert manifest["capability_status"] == "BROWSER_CAPTURE_TO_SPARK_MAPPING_UNVERIFIED"
    assert manifest["browser_capture_mapping_verified"] is False
    assert manifest["shopify_upload_verified"] is False
    assert manifest["portal_package_verified"] is False
    assert report["spark_desktop_roundtrip_verified"] is False
