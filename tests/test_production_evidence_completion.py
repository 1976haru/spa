import json

import pytest

from shopsource.db import connect
from shopsource.production import ProductionGoldenPathService
from shopsource.production_runner import ProductionEvidenceRunner
from shopsource.production_runner import _KeepaObservationProvider
from shopsource.source_safety import SourceMonitorService


class FakeSourceProvider:
    def __init__(self, availability="IN_STOCK"):
        self.calls = 0
        self.availability = availability

    def observe_batch(self, rows):
        self.calls += 1
        return {row["product_id"]: {"availability": self.availability,
            "availability_confidence": "HIGH", "source_price": 12.0, "source_currency": "USD",
            "evidence_kind": "MOCK_CANONICAL", "evidence": {"fixture": True}}
            for row in rows}


def _run(runner):
    return runner.start_or_resume("001")


def test_g2_calls_injected_provider_only_after_confirmation_and_continues(tmp_path):
    db = tmp_path / "g2.sqlite3"
    service = ProductionGoldenPathService(db=db)
    SourceMonitorService(db)
    now = "2026-01-01T00:00:00+00:00"
    with connect(db) as con:
        con.execute("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    ("B000000001", "Cabin organizer product", "{}", now, now))
    provider = FakeSourceProvider()
    runner = ProductionEvidenceRunner(db=db, service=service, source_provider=provider)
    run = _run(runner)
    with pytest.raises(PermissionError):
        runner.confirm_source_audit(run["run_id"], confirmed=False)
    assert provider.calls == 0
    result = runner.confirm_source_audit(run["run_id"], confirmed=True)
    assert result["status"] == "COMPLETE"
    assert provider.calls == 1
    assert result["production_run"]["gates"][2]["evidence"]["counts"]["checked"] == 1


def test_provider_error_is_not_oos_and_blocks_g2_until_rechecked(tmp_path):
    db = tmp_path / "error.sqlite3"
    service = ProductionGoldenPathService(db=db); SourceMonitorService(db)
    now = "2026-01-01T00:00:00+00:00"
    with connect(db) as con:
        con.execute("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    ("B000000002", "Another cabin organizer", "{}", now, now))
    runner = ProductionEvidenceRunner(db=db, service=service, source_provider=FakeSourceProvider("SOURCE_ERROR"))
    run = _run(runner)
    result = runner.confirm_source_audit(run["run_id"], confirmed=True)
    gate = result["production_run"]["gates"][2]
    assert result["status"] == "FAILED"
    assert gate["evidence"]["provider_errors_are_not_oos"] is True
    with connect(db) as con:
        snapshot = con.execute("SELECT availability FROM source_product_snapshots").fetchone()
    assert snapshot[0] == "SOURCE_ERROR"


def test_media_rights_require_selected_explicit_review_and_timestamp(tmp_path):
    db = tmp_path / "rights.sqlite3"
    service = ProductionGoldenPathService(db=db)
    runner = ProductionEvidenceRunner(db=db, service=service)
    run = _run(runner)
    with connect(db) as con:
        con.execute("INSERT INTO products(id,asin,title,images_json,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?)",
                    (7, "B000000007", "Organizer with exact image", '["merchant-owned.jpg"]', "{}", "now", "now"))
    with pytest.raises(PermissionError):
        runner.review_media_rights(run["run_id"], [7], "LICENSED")
    runner.review_media_rights(run["run_id"], [7], "LICENSED", confirmed=True, notes={7: "license checked"})
    with connect(db) as con:
        row = con.execute("SELECT policy,reviewed_at,notes FROM product_media_rights WHERE store_id='001' AND product_id=7").fetchone()
    assert row[0] == "LICENSED" and row[1] and row[2] == "license checked"


def test_pricing_policy_is_explicit_and_auto_reprice_stays_off(tmp_path):
    db = tmp_path / "pricing.sqlite3"
    runner = ProductionEvidenceRunner(db=db)
    run = _run(runner)
    policy = {"currency": "USD", "source_cost_buffer_fixed": 1, "source_cost_buffer_percent": 2,
              "min_margin_amount": 5, "min_margin_percent": 20, "unknown_fee_handling": "BLOCK",
              "warning_source_price_change_percent": 10}
    with pytest.raises(PermissionError): runner.save_pricing_policy(run["run_id"], policy)
    result = runner.save_pricing_policy(run["run_id"], policy, confirmed=True)
    assert result["gates"][5]["evidence"]["auto_reprice_enabled"] is False


def test_pages_policies_need_manual_content_review_and_never_invent_facts(tmp_path):
    db = tmp_path / "pages.sqlite3"
    runner = ProductionEvidenceRunner(db=db, collectors={"remote_pages": lambda store: {
        "pages": [{"title": title, "handle": title.casefold(), "body": "Existing merchant content"}
                  for title in ("Contact", "About", "Shipping", "Returns", "Privacy", "Terms")],
        "policies": [], "page_api_status": "READ", "policy_api_status": "READ"}})
    run = _run(runner)
    evidence = runner._pages_policies("001", run)
    assert evidence["status"] == "WAITING_FOR_INPUT"
    assert all(item["status"] == "PRESENT_NEEDS_REVIEW" for item in evidence["items"].values())
    assert evidence["business_facts_invented"] is False
    payload = {name: True for name in ("Contact", "About", "Shipping", "Returns/Refund", "Privacy", "Terms")}
    result = runner.save_manual_evidence(run["run_id"], "PAGES_POLICIES", "PAGES_POLICIES_SIGNOFF", payload, confirmed=True)
    assert result["production_run"]["gates"][11]["status"] == "VERIFIED"


def test_seo_visual_signoff_is_fingerprint_bound_and_not_wcag_claim(tmp_path, monkeypatch):
    runner = ProductionEvidenceRunner(db=tmp_path / "visual.sqlite3")
    run = _run(runner)
    monkeypatch.setattr(runner, "_theme_snapshot", lambda store: {"status": "CONNECTED", "theme": {"id": "theme-1"}, "template": {}, "theme_files": {}})
    evidence = runner._seo_mobile("001", run)
    assert evidence["wcag_automatically_certified"] is False
    checklist = {key: True for key in ("desktop", "mobile", "hero_crop", "category_cards", "menu", "footer", "readability")}
    result = runner.save_manual_evidence(run["run_id"], "SEO_ACCESSIBILITY_MOBILE", "VISUAL_SIGNOFF",
                                        checklist, confirmed=True, fingerprint=evidence["visual_signoff_fingerprint"])
    assert result["production_run"]["gates"][12]["evidence"]["human_visual_signed_off"] is True


def test_commerce_api_and_manual_evidence_are_separate(tmp_path):
    runner = ProductionEvidenceRunner(db=tmp_path / "commerce.sqlite3", collectors={"remote_commerce": lambda store: {
        "api_evidence": {"currency": "USD"}, "unsupported": ["tax"],
        "manual_verification_required": ["US market", "tax"]}})
    run = _run(runner)
    evidence = runner._commerce("001", run)
    assert evidence["api_evidence"] == {"currency": "USD"}
    assert evidence["status"] == "WAITING_FOR_INPUT"
    result = runner.save_manual_evidence(run["run_id"], "COMMERCE_READINESS", "COMMERCE_SIGNOFF",
                                        {"US market": True, "tax": True}, confirmed=True)
    assert result["production_run"]["gates"][13]["status"] == "VERIFIED"


def test_business_facts_are_operator_supplied_and_local_only(tmp_path):
    runner = ProductionEvidenceRunner(db=tmp_path / "business.sqlite3")
    with pytest.raises(PermissionError):
        runner.save_business_inputs("001", {"support_email": "help@example.test"})
    result = runner.save_business_inputs("001", {"support_email": "owner@example.test", "return_window": "30 days"}, confirmed=True)
    assert result["local_only"] is True and result["remote_write_performed"] is False
    with connect(runner.db) as con:
        payload = json.loads(con.execute("SELECT payload_json FROM production_business_inputs WHERE store_id='001'").fetchone()[0])
    assert payload == {"return_window": "30 days", "support_email": "owner@example.test"}


def test_keepa_documented_new_offer_stats_are_normalized_without_live_call(monkeypatch):
    monkeypatch.setenv("KEEPA_API_KEY", "synthetic-key")
    adapter = _KeepaObservationProvider()
    class FakeHydrator:
        def hydrate(self, asins):
            assert asins == ["B000000001", "B000000002", "B000000003"]
            return type("Batch", (), {"products": [
                {"asin": asins[0], "domainId": 1, "productType": 0, "lastUpdate": 123,
                 "stats": {"current": [-1, 2599, -1, 0, -1, -1, -1, -1, -1, -1, -1, 3]}},
                {"asin": asins[1], "domainId": 1, "productType": 0, "stats": {"current": [-1, -1, -1, 0, -1, -1, -1, -1, -1, -1, -1, 0]}},
                {"asin": asins[2], "domainId": 1, "productType": 0, "stats": {"current": [-1, -1, -1, 0, -1, -1, -1, -1, -1, -1, -1, -2]}},
            ]})()
    adapter.client = FakeHydrator()
    result = adapter.observe_batch([{"product_id": i, "asin": f"B{i:09d}"} for i in range(1, 4)])
    assert result[1]["availability"] == "IN_STOCK" and result[1]["source_price"] == 25.99
    assert result[2]["availability"] == "OUT_OF_STOCK"
    assert result[3]["availability"] == "UNKNOWN"


def test_g14_pilot_write_remains_unstarted(tmp_path):
    runner = ProductionEvidenceRunner(db=tmp_path / "pilot.sqlite3")
    run = _run(runner)
    assert run["gates"][14]["status"] == "NOT_STARTED"
    assert run["gates"][14]["gate_key"] == "CONTROLLED_LIVE_PILOT"


def test_source_audit_control_requires_initial_approval_and_stop_is_persistent(tmp_path):
    runner = ProductionEvidenceRunner(db=tmp_path / "control.sqlite3", source_provider=FakeSourceProvider())
    run = _run(runner)
    with pytest.raises(PermissionError): runner.source_audit_control(run["run_id"], "PAUSE")
    # An empty due list requires no external call but still records explicit approval.
    runner.confirm_source_audit(run["run_id"], observations={}, confirmed=True)
    stopped = runner.source_audit_control(run["run_id"], "STOP")
    assert stopped["status"] == "STOPPED"
    with pytest.raises(ValueError): runner.source_audit_control(run["run_id"], "RESUME")


def test_source_audit_pause_resume_uses_batch_checkpoint(tmp_path):
    db = tmp_path / "pause-resume.sqlite3"
    service = ProductionGoldenPathService(db=db); SourceMonitorService(db)
    now = "2026-01-01T00:00:00+00:00"
    with connect(db) as con:
        con.executemany("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                        [(f"B{i:09d}", f"Organizer product {i}", "{}", now, now) for i in range(101)])
    class PauseProvider(FakeSourceProvider):
        runner = None
        def observe_batch(self, rows):
            result = super().observe_batch(rows)
            if self.calls == 1:
                self.runner.source_audit_control(self.run_id, "PAUSE")
            return result
    provider = PauseProvider(); runner = ProductionEvidenceRunner(db=db, service=service, source_provider=provider)
    run = _run(runner); provider.runner = runner; provider.run_id = run["run_id"]
    paused = runner.confirm_source_audit(run["run_id"], confirmed=True)
    assert paused["status"] == "FAILED"
    checkpoint = service.get(run["run_id"])["checkpoint"]["source_audit"]
    assert checkpoint["completed"] == 100 and checkpoint["control"] == "PAUSED"
    resumed = runner.source_audit_control(run["run_id"], "RESUME")
    assert resumed["status"] == "COMPLETE"
    assert provider.calls == 2
    assert resumed["production_run"]["gates"][2]["status"] == "VERIFIED"
