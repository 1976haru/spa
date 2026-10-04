from shopsource.production import GATES, ProductionGoldenPathService
from shopsource.production_runner import EVIDENCE_GATES, ProductionEvidenceRunner


def _ready_collectors():
    return {key: (lambda store_id, run, key=key: {
        "verified": True, "human_visual_signed_off": key == "SEO_ACCESSIBILITY_MOBILE",
        "fingerprint_input": {"key": key, "store": store_id}})
        for key in EVIDENCE_GATES}


def test_unfinished_production_run_is_resumed_without_duplicate(tmp_path):
    db = tmp_path / "resume.sqlite3"
    service = ProductionGoldenPathService(db=db)
    first = service.start("001")
    runner = ProductionEvidenceRunner(db=db, service=service, collectors=_ready_collectors())
    resumed = runner.start_or_resume("001")
    assert resumed["run_id"] == first["run_id"]
    assert runner.run("001")["run_id"] == first["run_id"]


def test_new_run_requires_explicit_confirmation(tmp_path):
    runner = ProductionEvidenceRunner(db=tmp_path / "new.sqlite3")
    try:
        runner.start_or_resume("001", new_run=True)
    except PermissionError:
        pass
    else:
        raise AssertionError("new run was created without confirmation")


def test_runner_persists_g0_g13_and_never_runs_pilot_write(tmp_path):
    db = tmp_path / "runner.sqlite3"
    service = ProductionGoldenPathService(db=db)
    runner = ProductionEvidenceRunner(db=db, service=service, collectors=_ready_collectors())
    run = runner.run("001")
    assert [gate["gate_key"] for gate in run["gates"]] == list(GATES)
    assert all(gate["status"] == "VERIFIED" for gate in run["gates"][:14])
    assert run["gates"][14]["status"] == "NOT_STARTED"
    assert run["status"] == "READY_FOR_PILOT"
    assert service.progress_report(run["run_id"])["production_readiness_percent"] == 100


def test_g0_missing_credentials_is_distinct_and_secret_is_not_exposed(tmp_path, monkeypatch):
    monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN", raising=False)
    runner = ProductionEvidenceRunner(db=tmp_path / "g0.sqlite3")
    result = runner._environment("001")
    assert result["status"] == "WAITING_FOR_CREDENTIALS"
    assert "token" not in str(result).lower()


def test_source_safety_preview_is_free_and_waits_for_provider_confirmation(tmp_path):
    db = tmp_path / "source-preview.sqlite3"
    runner = ProductionEvidenceRunner(db=db, collectors=_ready_collectors())
    result = runner._source("001")
    assert result["status"] == "VERIFIED"
    assert result["preview_only"] is True
    assert result["target_count"] == 0


def test_media_rights_are_never_auto_assigned(tmp_path):
    db = tmp_path / "rights.sqlite3"
    service = ProductionGoldenPathService(db=db)
    service.audit_master("001")
    runner = ProductionEvidenceRunner(db=db, service=service)
    result = runner._media("001")
    assert result["rights_auto_assigned"] is False
    assert result["status"] == "WAITING_FOR_INPUT"  # no candidates means there is no media evidence to verify


def test_source_provider_failure_is_not_classified_as_out_of_stock(tmp_path):
    db = tmp_path / "source.sqlite3"
    service = ProductionGoldenPathService(db=db)
    runner = ProductionEvidenceRunner(db=db, service=service, collectors=_ready_collectors())
    run = runner.start_or_resume("001")
    try:
        runner.confirm_source_audit(run["run_id"], {}, confirmed=False)
    except PermissionError:
        pass
    else:
        raise AssertionError("provider audit ran without explicit user confirmation")
