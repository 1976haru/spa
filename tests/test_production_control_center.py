from datetime import datetime, timezone

from shopsource.db import connect, init_db
from shopsource.production import GATES, ProductionGoldenPathService
from shopsource.production_runner import EVIDENCE_GATES, ProductionEvidenceRunner
from shopsource.ui.production_control_center import (
    GATE_ACTION_REGISTRY, gate_board_rows, production_summary, resolve_gate_click,
)


def _run(tmp_path):
    db = tmp_path / "control-center.sqlite3"
    service = ProductionGoldenPathService(db=db)
    run = service.start("001")
    return db, service, run


def test_gate_board_has_all_17_numbered_actionable_rows_and_current_highlight(tmp_path):
    _, service, run = _run(tmp_path)
    service.update(run["run_id"], {
        "ENVIRONMENT_STORE_IDENTITY": {"verified": True},
        "SOURCING_QUALITY": {"verified": True},
        "SOURCE_SAFETY": {"status": "WAITING_FOR_INPUT", "missing_inputs": ["source evidence"]},
    })
    run = service.get(run["run_id"])
    progress = service.progress_report(run["run_id"])
    rows = gate_board_rows(run, progress)
    assert len(rows) == len(GATES) == 17
    assert [row["number"] for row in rows] == list(range(1, 18))
    assert set(GATE_ACTION_REGISTRY) == set(GATES)
    assert rows[2]["current"] is True
    assert rows[2]["status_ko"] == "정보 입력 필요"
    assert resolve_gate_click(run, 4) == "PRODUCT_CONTENT"
    assert resolve_gate_click(run, "SOURCE_SAFETY") == "SOURCE_SAFETY"
    with_status = service.update(run["run_id"], {"SOURCE_SAFETY": {"status": "WAITING_FOR_INPUT", "missing_inputs": ["fixture"]}})
    row = gate_board_rows(with_status, service.progress_report(run["run_id"]))[2]
    assert row["status_ko"] == "정보 입력 필요"
    assert row["current"] is True


def test_gate_03_and_gate_04_resolve_to_their_own_action_panels():
    run = {"gates": [{"gate_key": key} for key in GATES]}
    assert GATE_ACTION_REGISTRY[resolve_gate_click(run, 3)]["panel"] == "source_safety"
    assert GATE_ACTION_REGISTRY[resolve_gate_click(run, 4)]["panel"] == "product_content"


def test_control_center_layout_is_compact_desktop_and_single_column_mobile():
    css = ( __import__("pathlib").Path(__file__).parents[1] / "src/shopsource/ui/beginner.py").read_text(encoding="utf-8")
    assert "grid-template-columns:minmax(0,1fr) minmax(0,1fr)" in css
    assert "@media (max-width: 900px)" in css and "grid-template-columns:minmax(0,1fr)" in css
    ui_source = (__import__("pathlib").Path(__file__).parents[1] / "src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert 'on_click=lambda key=gate_row["gate_key"]: open_gate_action(key)' in ui_source


def test_gate_board_summary_keeps_evidence_denominator_14(tmp_path):
    _, service, run = _run(tmp_path)
    progress = service.progress_report(run["run_id"])
    summary = production_summary(run, progress)
    assert summary["denominator"] == 14
    assert summary["percent"] == 0
    assert "17개" in summary["progress_note"] and "14개" in summary["progress_note"]


def test_product_content_draft_is_local_and_rechecks_selected_gate(tmp_path):
    db = tmp_path / "content-draft.sqlite3"
    init_db(db)
    now = datetime.now(timezone.utc).isoformat()
    with connect(db) as con:
        con.execute("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    ("B000000001", "Original MASTER title", "{}", now, now))
        con.execute("""INSERT INTO store_product_decisions(store_id,product_id,fit_score,price_status,risk_status,
            auto_status,final_status,classified_at) VALUES('001',1,90,'UNKNOWN','CLEAR','PRIMARY','PRIMARY',?)""", (now,))
    service = ProductionGoldenPathService(db=db)
    other_collectors = {key: (lambda store, run: {"verified": True, "fingerprint_input": {"gate": key}})
                        for key in EVIDENCE_GATES if key != "PRODUCT_CONTENT"}
    runner = ProductionEvidenceRunner(db=db, service=service, collectors=other_collectors)
    run = service.start("001")
    values = {"storefront_title": "Cabin storage organizer", "description": "Operator supplied factual description.",
              "features": "Foldable storage\nRemovable dividers", "variant_summary": "Single option confirmed", "variant_reviewed": True,
              "seo_title": "Cabin storage organizer", "seo_description": "Organize items in your vehicle.",
              "handle": "cabin-storage-organizer"}
    result = runner.save_content_draft(run["run_id"], 1, values, source_facts_confirmed=True, confirmed=True)
    assert result["local_only"] is True
    assert result["master_data_overwritten"] is False
    assert result["shopify_write_performed"] is False
    g3 = next(gate for gate in result["production_run"]["gates"] if gate["gate_key"] == "PRODUCT_CONTENT")
    assert g3["status"] == "VERIFIED"
    with connect(db) as con:
        title = con.execute("SELECT title FROM products WHERE id=1").fetchone()[0]
    assert title == "Original MASTER title"


def test_product_content_draft_needs_explicit_fact_and_variant_confirmation(tmp_path):
    db, service, _ = _run(tmp_path)
    runner = ProductionEvidenceRunner(db=db, service=service)
    with connect(db) as con:
        now = datetime.now(timezone.utc).isoformat()
        con.execute("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    ("B000000002", "Product title", "{}", now, now))
        con.execute("""INSERT INTO store_product_decisions(store_id,product_id,fit_score,price_status,risk_status,
            auto_status,final_status,classified_at) VALUES('001',1,90,'UNKNOWN','CLEAR','PRIMARY','PRIMARY',?)""", (now,))
    run = service.start("001")
    runner = ProductionEvidenceRunner(db=db, service=service)
    values = {"storefront_title": "Good product title", "description": "description", "features": "feature", "variant_summary": "variant",
              "variant_reviewed": False, "seo_title": "SEO title", "seo_description": "SEO description", "handle": "good-product"}
    try:
        runner.save_content_draft(run["run_id"], 1, values, source_facts_confirmed=False, confirmed=True)
    except ValueError as exc:
        assert "variant" in str(exc).lower()
    else:
        raise AssertionError("variant review must be explicit")
