import json

import pytest

from shopsource.db import connect, init_db
from shopsource.production import (
    GATES, ProductionGoldenPathService, inspect_product, no_placeholder_audit,
)


def _candidate(**overrides):
    row = {
        "id": 1, "asin": "B000000001", "title": "Foldable Car Trunk Organizer", "source_url": "https://source.invalid/p/1",
        "store_relevant": True, "selling_price": 39.99, "primary_image": "exact-product.jpg",
        "media_policy": "LICENSED", "description_supported": True, "features_supported": True,
        "variant_normalized": True, "seo_ready": True, "handle_stable": True,
    }
    row.update(overrides)
    return row


def _safe():
    return {"snapshot_fresh": True, "freshness_status": "FRESH", "availability": "IN_STOCK",
            "sellability_status": "SELLABLE", "margin_status": "PASS"}


def test_all_production_gates_are_persisted_and_unknown_is_not_ready(tmp_path):
    svc = ProductionGoldenPathService(db=tmp_path / "prod.sqlite3", export_dir=tmp_path / "exports")
    run = svc.start("001")
    assert len(run["gates"]) == 17 and tuple(x["gate_key"] for x in run["gates"]) == GATES
    assert run["gates"][0]["status"] == "REVIEW_REQUIRED"
    assert run["status"] == "NOT_READY"
    assert run["summary"]["completion_percent"] == 0
    assert run["checkpoint"]["store_build_mode"] == "PREVIEW"
    assert run["checkpoint"]["store_build_run_id"].startswith("SBR_")


def test_gate_prerequisites_block_false_ready_and_blocker_overrides_score(tmp_path):
    svc = ProductionGoldenPathService(db=tmp_path / "gates.sqlite3")
    run = svc.start("001")
    changed = svc.update(run["run_id"], {"FINAL_LAUNCH_READINESS": {"ready": True}})
    final = changed["gates"][-1]
    assert final["status"] == "BLOCKED"
    assert any("ENVIRONMENT_STORE_IDENTITY" in reason for reason in final["blockers"])
    assert changed["status"] == "NOT_READY"


def test_master_decision_never_bypasses_unknown_or_stale_source(tmp_path):
    base = _candidate()
    assert inspect_product(base, safety=_safe(), pricing_policy={"enabled": True})["classification"] == "PRODUCTION_CANDIDATE"
    assert inspect_product(base, safety={**_safe(), "availability": "UNKNOWN"}, pricing_policy={"enabled": True})["classification"] == "REVIEW_REQUIRED"
    assert inspect_product(base, safety={**_safe(), "freshness_status": "STALE_BLOCKED", "snapshot_fresh": False}, pricing_policy={"enabled": True})["classification"] == "REVIEW_REQUIRED"


def test_restricted_archived_and_missing_price_are_never_candidates():
    assert inspect_product(_candidate(restricted=True), safety=_safe(), pricing_policy={"enabled": True})["classification"] == "RESTRICTED"
    assert inspect_product(_candidate(archived=True), safety=_safe(), pricing_policy={"enabled": True})["classification"] == "REJECT_FOR_STORE"
    result = inspect_product(_candidate(selling_price=None), safety=_safe(), pricing_policy={"enabled": True})
    assert "MISSING_SELLING_PRICE" in result["reasons"]


def test_missing_margin_policy_blocks_active_and_unconfirmed_media(tmp_path):
    candidate = _candidate()
    assert "PRICING_POLICY_MISSING" in inspect_product(candidate, safety=_safe())["reasons"]
    assert "MEDIA_RIGHTS_UNCONFIRMED" in inspect_product(_candidate(media_policy="NO_RIGHTS_CONFIRMED"), safety=_safe(), pricing_policy={"enabled": True})["reasons"]
    svc = ProductionGoldenPathService(db=tmp_path / "active.sqlite3")
    assert svc.active_publication_gate(blockers=[])["status"] == "WAITING_FOR_CONFIRMATION"
    assert svc.active_publication_gate(blockers=["margin"])["status"] == "BLOCKED"
    assert svc.active_publication_gate(blockers=[], explicit_confirmed=True)["write_performed"] is False
    assert "UNSUPPORTED_MULTI_VARIANT" in inspect_product(_candidate(variant_count=3), safety=_safe(), pricing_policy={"enabled": True})["reasons"]


def test_generated_lifestyle_cannot_substitute_exact_product_media():
    product = _candidate(media_policy="LICENSED", primary_image=None, images=None, lifestyle_image="scene.jpg")
    result = inspect_product(product, safety=_safe(), pricing_policy={"enabled": True})
    assert "MISSING_EXACT_PRODUCT_MEDIA" in result["reasons"]
    assert result["classification"] != "PRODUCTION_CANDIDATE"


def test_placeholder_and_wrong_collection_targets_block(tmp_path):
    findings = no_placeholder_audit({
        "links": [{"label": "Shop", "target": "#"}, {"label": "wrong", "target": "/collections/trunk", "wrong_target": True}],
        "hero": {"approved_image": True, "cta_target": "#"},
        "collections": [{"key": "seat", "product_count": 2, "approved_image": False}],
        "products": [{"asin": "B1", "selling_price": 0, "media_approved": False,
                      "source_safety": {"availability": "UNKNOWN", "freshness_status": "STALE_BLOCKED"}}],
        "text": "lorem ipsum",
    })
    codes = {row["code"] for row in findings}
    assert {"PLACEHOLDER_LINK", "WRONG_COLLECTION_TARGET", "HERO_WITHOUT_REAL_CTA", "MISSING_COLLECTION_IMAGE",
            "UNKNOWN_SOURCE", "STALE_SOURCE", "MISSING_SELLING_PRICE", "UNAPPROVED_PRODUCT_MEDIA"} <= codes


def test_business_facts_are_never_fabricated_and_report_is_secret_safe(tmp_path):
    svc = ProductionGoldenPathService(db=tmp_path / "report.sqlite3", export_dir=tmp_path / "exports")
    run = svc.start("001")
    svc.assess(run["run_id"], {"gates": {"ENVIRONMENT_STORE_IDENTITY": {"ready": True},
        "PAGES_POLICIES": {"missing_inputs": ["support email", "return address"]}},
        "commerce": {"token": "shpat_test_secret", "payment": "UNKNOWN"}})
    report = tmp_path / "exports" / "001" / run["run_id"] / "production_summary.json"
    text = report.read_text(encoding="utf-8")
    assert "shpat_test_secret" not in text
    assert json.loads(text)["shopify_writes"] == 0
    assert "support email" in text or "PAGES_POLICIES" in text


def test_rollout_is_10_50_200_confirmed_draft_and_mismatch_stops(tmp_path):
    svc = ProductionGoldenPathService(db=tmp_path / "rollout.sqlite3")
    first = svc.rollout("001")
    assert first["batch_size"] == 10 and first["publish_status"] == "DRAFT" and not first["write_performed"]
    assert svc.record_batch_verification("001", expected_count=10, verified_count=10)["next_batch"] == 50
    assert svc.rollout("001", explicit_confirmed=True)["batch_size"] == 50
    assert svc.record_batch_verification("001", expected_count=50, verified_count=50)["next_batch"] == 200
    assert svc.rollout("001", explicit_confirmed=True)["batch_size"] == 200
    assert svc.record_batch_verification("001", expected_count=200, verified_count=199)["status"] == "BLOCKED"
    assert svc.rollout("001", critical_mismatch=True)["status"] == "BLOCKED"


def test_controlled_pilot_is_max_10_draft_and_remote_mismatch_blocks(tmp_path):
    svc = ProductionGoldenPathService(db=tmp_path / "pilot.sqlite3")
    rows = [{"id": i, "classification": "PRODUCTION_CANDIDATE", "publishable": True} for i in range(12)]
    preview = svc.pilot_preview(rows)
    assert preview["selected_count"] == 10 and preview["publish_status"] == "DRAFT"
    assert preview["write_performed"] is False
    with pytest.raises(ValueError): svc.pilot_preview(rows, limit=11)
    assert svc.verify_pilot_preview(preview, current_fingerprint=preview["preview_fingerprint"],
        identity_rows=[{"identity_match": True, "duplicate": False}] * 10)["status"] == "READY_FOR_EXPLICIT_CONFIRMATION"
    assert svc.verify_pilot_preview(preview, current_fingerprint="stale", identity_rows=[])["reason"] == "PREVIEW_STALE"


def test_restart_resume_reads_persisted_gates_and_is_store_isolated(tmp_path):
    db = tmp_path / "resume.sqlite3"
    first = ProductionGoldenPathService(db=db)
    run = first.start("001")
    second = ProductionGoldenPathService(db=db)
    assert second.get(run["run_id"])["gates"][0]["status"] == "REVIEW_REQUIRED"
    other = second.start("002")
    assert other["store_id"] != second.get(run["run_id"])["store_id"]


def test_batch_audit_and_50k_evaluation_keep_row_count(tmp_path):
    svc = ProductionGoldenPathService(db=tmp_path / "stress.sqlite3")
    rows = [_candidate(id=i, asin=f"B{i:09d}", store_relevant=True) for i in range(50_000)]
    audited = svc.evaluate_catalog(rows)
    assert len(audited) == 50_000
    assert all(row["classification"] == "REVIEW_REQUIRED" for row in audited)
    assert sum(row["classification"] == "PRODUCTION_CANDIDATE" for row in audited) == 0


def test_audit_master_uses_store_decision_but_source_gate_remains_unknown(tmp_path):
    db = tmp_path / "master.sqlite3"
    init_db(db)
    with connect(db) as con:
        con.execute("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES('B000000001','Car Trunk Organizer','{}','2026-01-01','2026-01-01')")
        pid = con.execute("SELECT id FROM products WHERE asin='B000000001'").fetchone()[0]
        con.execute("INSERT INTO store_product_decisions(store_id,product_id,fit_score,price_status,risk_status,auto_status,final_status,classified_at) VALUES('001',?,100,'PASS','LOW','PRIMARY','PRIMARY','2026-01-01')", (pid,))
    audit = ProductionGoldenPathService(db=db).audit_master("001")
    assert audit[0]["classification"] == "REVIEW_REQUIRED"
    assert "SOURCE_NOT_VERIFIED_IN_STOCK" in audit[0]["reasons"]


def test_start_and_audit_have_no_outbound_network_or_mutation(tmp_path, monkeypatch):
    import socket
    import urllib.request
    def denied(*_args, **_kwargs): raise AssertionError("unexpected real network")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    svc = ProductionGoldenPathService(db=tmp_path / "offline.sqlite3", export_dir=tmp_path / "exports")
    run = svc.start("001")
    report = svc.write_report(run["run_id"], {})
    assert svc.get(run["run_id"])["checkpoint"]["store_build_mode"] == "PREVIEW"
    assert (report / "final_launch_report.md").is_file()
