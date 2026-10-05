import json

import pytest

from shopsource.db import connect
from shopsource.production import ProductionGoldenPathService
from shopsource.production_runner import ProductionEvidenceRunner
from shopsource.source_provider_profiles import SourceProviderProfiles
from shopsource.source_safety import SourceMonitorService


class Provider:
    def __init__(self, availability="IN_STOCK", tokens=10000, fail=False):
        self.availability = availability
        self.tokens = tokens
        self.fail = fail
        self.calls = []
        self.last_tokens_consumed = 0

    def health(self):
        if self.fail:
            raise TimeoutError("synthetic provider timeout")
        return {"ok": True, "tokensLeft": self.tokens, "refillIn": 60, "refillRate": 5}

    def observe_batch(self, rows):
        self.calls.append(list(rows))
        return {row["product_id"]: {"availability": self.availability,
            "availability_confidence": "HIGH", "source_price": 12, "source_currency": "USD",
            "evidence_kind": "MOCK", "evidence": {}} for row in rows}


def _setup(tmp_path, count=3, provider=None):
    db = tmp_path / "source-provider.sqlite3"
    service = ProductionGoldenPathService(db=db)
    SourceMonitorService(db)
    now = "2026-01-01T00:00:00+00:00"
    with connect(db) as con:
        con.executemany("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
            [(f"B{i:09d}", f"Organizer product {i}", "{}", now, now) for i in range(1, count + 1)])
    runner = ProductionEvidenceRunner(db=db, service=service, source_provider=provider)
    run = runner.start_or_resume("001")
    return db, runner, run


def _mock_keyring(monkeypatch):
    values = {}
    class FakeKeyring:
        @staticmethod
        def get_password(service, username): return values.get((service, username))
        @staticmethod
        def set_password(service, username, password): values[(service, username)] = password
    monkeypatch.setattr(SourceProviderProfiles, "_keyring", staticmethod(lambda: FakeKeyring))
    return values


def test_provider_missing_cannot_approve_or_leave_checkpoint(tmp_path):
    _, runner, run = _setup(tmp_path)
    with pytest.raises(RuntimeError, match="Source provider is not ready"):
        runner.confirm_source_audit(run["run_id"], confirmed=True)
    assert "approved_at" not in runner.service.get(run["run_id"])["checkpoint"].get("source_audit", {})


def test_provider_health_failure_blocks_and_does_not_approve(tmp_path, monkeypatch):
    _mock_keyring(monkeypatch)
    _, runner, run = _setup(tmp_path)
    runner.source_profiles.save_keepa_profile("keepa-shared", "Keepa shared", "synthetic-secret", store_id="001")
    result = runner.source_provider_health_check(run["run_id"], provider=Provider(fail=True))
    assert result["health"] == "FAIL"
    assert runner.source_provider_preflight(run["run_id"])["status"] == "WAITING_FOR_INPUT"
    with pytest.raises(RuntimeError): runner.confirm_source_audit(run["run_id"], confirmed=True)
    assert "approved_at" not in runner.service.get(run["run_id"])["checkpoint"].get("source_audit", {})


def test_profile_credentials_are_secret_free_in_sqlite_and_reused_across_stores(tmp_path, monkeypatch):
    values = _mock_keyring(monkeypatch)
    db = tmp_path / "profiles.sqlite3"
    profiles = SourceProviderProfiles(db)
    profiles.save_keepa_profile("shared", "Shared Keepa", "secret-never-in-db", store_id="001")
    profiles.bind("002", "shared")
    assert profiles.for_store("002")["profile_id"] == "shared"
    with connect(db) as con:
        dump = " ".join(str(value) for row in con.execute("SELECT * FROM source_provider_profiles") for value in row)
    assert "secret-never-in-db" not in dump
    assert values
    assert profiles.credential("shared")[0] == "secret-never-in-db"


def test_pilot_is_at_most_100_and_never_verifies_g2(tmp_path):
    provider = Provider()
    _, runner, run = _setup(tmp_path, count=125, provider=provider)
    result = runner.run_source_provider_pilot(run["run_id"], confirmed=True)
    assert result["status"] == "PASS"
    assert len(provider.calls) == 1 and len(provider.calls[0]) == 100
    assert result["production_run"]["gates"][2]["status"] != "VERIFIED"
    assert result["production_run"]["checkpoint"]["source_audit"]["provider_pilot"]["target_count"] == 100
    with connect(runner.db) as con:
        assert con.execute("SELECT COUNT(*) FROM source_product_snapshots").fetchone()[0] == 0


def test_pilot_unknown_needs_review_and_full_audit_resumes_batches(tmp_path):
    provider = Provider("UNKNOWN")
    _, runner, run = _setup(tmp_path, count=105, provider=provider)
    pilot = runner.run_source_provider_pilot(run["run_id"], confirmed=True)
    assert pilot["status"] == "REVIEW_REQUIRED"
    assert pilot["production_run"]["gates"][2]["status"] != "VERIFIED"


def test_full_audit_uses_100_batches_and_finishes_only_after_all_due_products(tmp_path):
    provider = Provider()
    _, runner, run = _setup(tmp_path, count=205, provider=provider)
    pilot = runner.run_source_provider_pilot(run["run_id"], confirmed=True)
    assert pilot["status"] == "PASS"
    result = runner.confirm_source_audit(run["run_id"], confirmed=True)
    assert result["status"] == "COMPLETE"
    assert result["checked"] == result["preview_count"] == 205
    assert [len(batch) for batch in provider.calls] == [100, 100, 100, 5]
    assert result["production_run"]["gates"][2]["status"] == "VERIFIED"


def test_full_catalog_1999_is_verified_only_after_all_items_complete(tmp_path):
    provider = Provider()
    _, runner, run = _setup(tmp_path, count=1999, provider=provider)
    pilot = runner.run_source_provider_pilot(run["run_id"], confirmed=True)
    assert pilot["status"] == "PASS"
    assert pilot["production_run"]["gates"][2]["status"] != "VERIFIED"
    result = runner.confirm_source_audit(run["run_id"], confirmed=True)
    assert result["status"] == "COMPLETE"
    assert result["checked"] == result["preview_count"] == 1999
    assert result["production_run"]["gates"][2]["status"] == "VERIFIED"


def test_tokens_below_due_target_blocks_full_and_estimated_cost_stays_unknown(tmp_path, monkeypatch):
    _mock_keyring(monkeypatch)
    _, runner, run = _setup(tmp_path, count=150)
    runner.source_profiles.save_keepa_profile("shared", "Keepa", "fixture-key", store_id="001")
    runner.source_provider_health_check(run["run_id"], provider=Provider(tokens=50))
    preflight = runner.source_provider_preflight(run["run_id"])
    assert preflight["status"] == "BLOCKED"
    assert preflight["estimated_cost"] == "UNKNOWN"
    assert preflight["available_tokens"] == 50
    with pytest.raises(RuntimeError): runner.confirm_source_audit(run["run_id"], confirmed=True)


def test_no_secret_in_provider_profile_or_preflight(tmp_path, monkeypatch):
    _mock_keyring(monkeypatch)
    db, runner, run = _setup(tmp_path)
    runner.source_profiles.save_keepa_profile("shared", "Keepa", "TOPSECRET", store_id="001")
    runner.source_provider_health_check(run["run_id"], provider=Provider())
    text = json.dumps(runner.source_provider_preflight(run["run_id"])) + json.dumps(runner.source_profiles.profile("shared"))
    with connect(db) as con:
        text += " ".join(str(v) for row in con.execute("SELECT * FROM source_provider_profiles") for v in row)
    assert "TOPSECRET" not in text
