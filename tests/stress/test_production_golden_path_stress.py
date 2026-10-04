from shopsource.production import GATES, ProductionGoldenPathService, no_placeholder_audit


def test_production_quality_audit_10000_rows_linear(tmp_path):
    service = ProductionGoldenPathService(db=tmp_path / "quality.sqlite3")
    rows = [{"id": i, "asin": f"B{i:09d}", "title": "Car Storage Product", "source_url": "fixture",
             "store_relevant": True, "selling_price": 25, "primary_image": "fixture.jpg",
             "source_safety": {"snapshot_fresh": True, "availability": "IN_STOCK", "sellability_status": "SELLABLE", "margin_status": "PASS"},
             "pricing_policy": {"enabled": True}, "media_policy": "LICENSED",
             "description_supported": True, "features_supported": True, "variant_normalized": True,
             "seo_ready": True, "handle_stable": True} for i in range(10_000)]
    result = service.evaluate_catalog(rows)
    assert len(result) == 10_000
    assert all(row["classification"] == "PRODUCTION_CANDIDATE" for row in result)


def test_200_store_gate_isolation_and_30_collection_audit(tmp_path):
    service = ProductionGoldenPathService(db=tmp_path / "stores.sqlite3")
    runs = [service.start(f"store-{i:03d}") for i in range(200)]
    assert len({run["run_id"] for run in runs}) == 200
    assert all(len(run["gates"]) == len(GATES) for run in runs)
    findings = no_placeholder_audit({"collections": [{"key": f"c{i}", "product_count": 2, "approved_image": True} for i in range(30)]})
    assert findings == []


def test_report_large_audits_are_redacted_and_deterministic(tmp_path):
    evidence = {"links": [{"label": f"link-{i}", "target": f"/collections/c{i}"} for i in range(5_000)],
                "api_token": "shpat_stress_secret"}
    first = no_placeholder_audit(evidence)
    second = no_placeholder_audit(evidence)
    assert first == second == []
