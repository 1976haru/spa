import json
from pathlib import Path

from shopsource.brand_automation import brand_name_prompt, brand_name_state, brand_profile_from_store
from shopsource.db import get_store, init_db, upsert_store
from shopsource.store_portfolio import load_portfolio, production_bootstrap, selector_label
from shopsource.ui.v2_service import list_stores


ROOT = Path(__file__).parents[1]
STORE_001 = ROOT / "stores" / "001_cabin_tidy.json"
STORE_002 = ROOT / "stores" / "002_garage_workshop.json"
PORTFOLIO = ROOT / "stores" / "store_portfolio.json"


def _profiles():
    return json.loads(STORE_001.read_text(encoding="utf-8")), json.loads(STORE_002.read_text(encoding="utf-8"))


def test_store_001_is_additive_read_only_golden_reference():
    original = STORE_001.read_bytes()
    one, _ = _profiles()
    metadata = load_portfolio(PORTFOLIO)["stores"]["001"]
    state = production_bootstrap(one, metadata)
    assert metadata["role"] == "GOLDEN_REFERENCE"
    assert metadata["build_origin"] == "EXTERNAL_SERVICE"
    assert state == {"role": "GOLDEN_REFERENCE", "build_origin": "EXTERNAL_SERVICE",
                     "mode": "PAUSED_REFERENCE", "progress_percent": 21,
                     "evidence": "3/14", "read_only": True}
    assert STORE_001.read_bytes() == original


def test_store_002_bootstrap_is_independent_and_waits_for_shopify():
    one, two = _profiles()
    metadata = load_portfolio(PORTFOLIO)["stores"]["002"]
    state = production_bootstrap(two, metadata)
    assert two["category"] == "Garage / Workshop Organization"
    assert two["strategy"] == "HOME PROBLEM SOLVER"
    assert two["reference_store_id"] == "001"
    assert two["lifecycle"] == "PLANNING"
    assert two["production_progress_percent"] == 0
    assert state["status"] == "WAITING_FOR_INPUT"
    assert state["shopify"] == "NOT CONNECTED"
    assert state["shopify_gate"] == "WAITING_FOR_SHOPIFY_STORE"
    assert state["brand"] == "NOT SELECTED"
    assert state["current_step"] == "BRAND / SHOPIFY SETUP"
    assert state["blocker"] == "Store 002 Shopify Store 생성 필요"
    assert len(two["build_steps"]) == 23
    assert set(one.get("include_keywords", [])).isdisjoint(two["initial_product_concepts"])
    assert "logo" not in two and "favicon" not in two and "products" not in two


def test_working_name_is_candidate_seed_not_approved_brand(tmp_path, monkeypatch):
    _, two = _profiles()
    db = tmp_path / "bootstrap.sqlite3"
    init_db(db)
    upsert_store(two, db)
    monkeypatch.setattr("shopsource.brand_automation.EXPORT_DIR", tmp_path / "exports")
    profile = brand_profile_from_store("002", db=db)
    assert profile["profile"]["brand_name"] == ""
    assert brand_name_state("002", db=db)["name_status"] == "REVIEW_REQUIRED"
    prompt = brand_name_prompt("002", db=db)
    assert "The Garage Fix" in prompt
    assert "candidate only, never auto-select" in prompt
    assert "exactly 10" in prompt


def test_multi_store_switching_labels_and_db_profiles_are_separate(tmp_path):
    one, two = _profiles()
    db = tmp_path / "stores.sqlite3"
    init_db(db)
    upsert_store(one, db)
    upsert_store(two, db)
    rows = list_stores(db)
    assert [row["store_id"] for row in rows] == ["001", "002"]
    metadata = load_portfolio(PORTFOLIO)["stores"]
    assert selector_label(rows[0]["profile"], metadata["001"]) == "001 | Cabin Tidy — GOLDEN REFERENCE"
    assert selector_label(rows[1]["profile"], metadata["002"]) == "002 | Brand TBD — PRODUCTION BUILD"
    assert get_store("001", db)["category"] != get_store("002", db)["category"]


def test_phase_scope_contains_no_external_write_configuration():
    two = json.loads(STORE_002.read_text(encoding="utf-8"))
    forbidden = {"shopify_token", "client_secret", "theme_id", "amazon_source_check", "shopify_write"}
    assert forbidden.isdisjoint(two)
    assert two["sourcing_provider"] == "SPARK"
