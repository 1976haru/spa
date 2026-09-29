import json
from pathlib import Path

import pytest
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:
    FastAPI = TestClient = None

from shopsource.capture.amazon_parser import parse_product_html, parse_search_html
from shopsource.capture.models import completeness_score
from shopsource.capture.service import CaptureService, browser_capture_to_spark_payload
from shopsource.capture.validation import reject_captcha, validate_product
from shopsource.classifier import classify_store
from shopsource.connectors.spark_center_package import SparkCenterPackageService
from shopsource.db import connect, init_db, upsert_store


def make_db(tmp_path):
    db = tmp_path / "capture.sqlite3"
    init_db(db)
    profile = json.loads((Path(__file__).parents[1] / "stores" / "001_cabin_tidy.json").read_text(encoding="utf-8"))
    upsert_store(profile, db)
    return db


def detail(asin):
    return {"asin": asin, "url": f"https://www.amazon.com/dp/{asin}", "title": "Synthetic organizer",
            "brand": "Test Brand", "price": 50.0, "category": "Automotive", "overview": ["Synthetic feature"],
            "aboutThis": [], "images": ["https://m.media-amazon.com/images/I/synthetic.jpg"], "rating": 4.5,
            "reviewCount": 100, "options": {}, "quantity": None, "tags": [],
            "_sourceUrl": f"https://www.amazon.com/dp/{asin}", "_listPage": None,
            "_collectedAt": "2026-09-30T00:00:00Z"}


def test_pairing_auth_and_sensitive_payload_rejection(tmp_path):
    db = make_db(tmp_path)
    service = CaptureService(db)
    token = service.create_pairing_code()
    assert service.authenticate(token)
    assert not service.authenticate(token + "x")
    if FastAPI is None:
        pytest.skip("FastAPI is provided by the optional UI dependency")
    from shopsource.capture.bridge import install_capture_routes
    app = FastAPI()
    install_capture_routes(app, service)
    client = TestClient(app)
    assert client.get("/api/capture/health").status_code == 401
    assert client.get("/api/capture/health", headers={"X-ShopSource-Pairing": token}).status_code == 200
    with pytest.raises(ValueError, match="민감정보"):
        validate_product({"asin": "B000000001", "title": "test", "sessionToken": "no"})
    with pytest.raises(ValueError, match="민감정보"):
        validate_product({"asin": "B000000001", "title": "test", "sessionStorage": {"key": "value"}})
    with pytest.raises(ValueError, match="ASIN"):
        validate_product({"asin": "bad", "title": "test"})


def test_capture_dedupe_detail_master_classify_and_spark_package(tmp_path):
    db = make_db(tmp_path)
    service = CaptureService(db)
    search = {"store_id": "001", "keyword": "trunk organizer", "search_url": "https://www.amazon.com/s?k=trunk",
              "captured_at": "2026-09-30T00:00:00Z", "products": [
                  {"asin": f"B00000000{i}", "title": f"Candidate {i}", "url": f"https://www.amazon.com/dp/B00000000{i}",
                       "price": 50, "images": ["https://m.media-amazon.com/images/I/synthetic.jpg"], "sponsored": None}
                  for i in range(1, 6)]}
    run = service.capture_search(search)
    assert run["candidates"] == 5
    # Same run ASIN is not duplicated.
    search["products"].append(search["products"][0])
    # A new search is separately retained as capture evidence.
    run2 = service.capture_search(search)
    assert run2["candidates"] == 5
    for i in range(1, 6):
        service.capture_detail({"store_id": "001", "product": detail(f"B00000000{i}")})
    result = service.import_candidates("001", [f"B00000000{i}" for i in range(1, 6)])
    assert result["inserted"] == 5
    assert result["classified"]["processed"] == 5
    with connect(db) as con:
        products = con.execute("SELECT asin,source,source_kind,raw_json FROM products ORDER BY asin").fetchall()
        occurrences = con.execute("SELECT COUNT(*) AS n FROM product_occurrences WHERE source_kind='BROWSER_CAPTURE'").fetchone()["n"]
    assert len(products) == 5 and occurrences == 5
    assert all(row["source"] == "amazon_browser" and row["source_kind"] == "BROWSER_CAPTURE" for row in products)
    assert 80 <= completeness_score(detail("B000000001")) <= 100
    mapped = browser_capture_to_spark_payload(detail("B000000001"))
    assert set(mapped) == {"url", "asin", "title", "brand", "price", "options", "quantity", "tags", "category",
                           "overview", "aboutThis", "images", "rating", "reviewCount", "_sourceUrl", "_listPage", "_collectedAt"}
    assert "sponsored" not in mapped and "source_kind" not in mapped
    with connect(db) as con:
        for row in con.execute("SELECT id,asin FROM products"):
            con.execute("UPDATE store_product_decisions SET final_status='PRIMARY',manual_override=1 WHERE product_id=?", (row["id"],))
    package = SparkCenterPackageService().create(store_id="001", statuses=["PRIMARY"], limit=5,
                                                   out_root=tmp_path / "로컬 export with spaces", db=db)
    assert package.product_count == 5 and package.validation_status == "PASS"
    exported = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(package.folder.glob("*.json"))]
    assert len(exported) == 5 and all("sponsored" not in item and "source_kind" not in item for item in exported)
    manifest = json.loads(package.manifest_path.read_text(encoding="utf-8"))
    assert manifest["capability_status"] == "BROWSER_CAPTURE_TO_SPARK_MAPPING_UNVERIFIED"
    assert manifest["source_kinds"] == {"BROWSER_CAPTURE": 5}
    assert manifest["browser_capture_mapping_verified"] is False


def test_parser_fixtures_and_captcha():
    html = '''<html><div data-component-type="s-search-result" data-asin="B000000001"><h2><span class="a-size-medium">Organizer</span></h2><a href="/dp/B000000001"><img src="https://example.invalid/a.jpg"></a></div></html>'''
    rows = parse_search_html(html, "https://www.amazon.com/s?k=organizer")
    assert rows[0]["asin"] == "B000000001" and rows[0]["images"]
    product_html = '''<script type="application/ld+json">{"@type":"Product","name":"Organizer","brand":{"name":"Brand"},"image":"https://example.invalid/a.jpg","offers":{"price":"45.00"}}</script>'''
    parsed = parse_product_html(product_html, "https://www.amazon.com/dp/B000000001")
    assert parsed["title"] == "Organizer" and parsed["price"] == "45.00" and parsed["asin"] == "B000000001"
    missing = parse_search_html('<div data-component-type="s-search-result" data-asin="B000000002"><h2><span class="a-size-medium">No image</span></h2></div>', "https://www.amazon.com/s?k=x")
    assert missing[0]["price"] is None and missing[0]["images"] == []
    with pytest.raises(ValueError, match="확인 화면"):
        reject_captcha("Robot Check")


def test_extension_uses_minimum_permissions_and_no_sensitive_browser_apis():
    root = Path(__file__).parents[1] / "browser_extension" / "shopsource_capture"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert not set(manifest["permissions"]).intersection({"cookies", "webRequest", "history", "downloads", "proxy", "nativeMessaging"})
    scripts = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.js"))
    assert "document.cookie" not in scripts
    assert "localStorage" not in scripts and "sessionStorage" not in scripts
    assert "Authorization" not in scripts and "aws-waf-token" not in scripts.lower()
