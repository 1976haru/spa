import json
import urllib.error

import pytest

from shopsource.connectors.spark_handoff import SparkHandoffConnector
from shopsource.db import connect, init_db, upsert_store
from shopsource.sourcing.engine import SourcingEngine
from shopsource.sourcing.mapping import (
    KEEPA_TO_SPARK_CAPABILITY,
    keepa_price,
    keepa_to_canonical,
)
from shopsource.sourcing.models import DiscoveryPage, HydrationBatch, TokenTelemetry
from shopsource.sourcing.providers.keepa import KeepaError, KeepaProvider, redact_key
from shopsource.sourcing.recipes import (
    dollars_to_cents,
    finder_query,
    rating_to_keepa,
    recipes_for_profile,
)


def profile():
    return {
        "store_id": "001", "store_name": "Cabin Tidy", "category": "auto_interior",
        "minimum_fit_score": 0,
        "include_keywords": ["organizer"],
        "risk_rules": [{"code": "battery", "status": "REVIEW", "terms": ["battery"]}],
        "price_bands": [
            {"name": "reserve", "min": 30, "max": 40, "status": "RESERVE_B"},
            {"name": "primary", "min": 40, "max": 100, "status": "PRIMARY"},
            {"name": "high", "min": 100, "max": 120, "status": "RESERVE_A"},
        ],
    }


def raw(asin, cents=5000, title=None, **extra):
    current = [-1] * 19
    current[0] = cents
    return {
        "asin": asin, "title": title or f"Car organizer {asin}", "brand": "Synthetic",
        "stats": {"current": current}, "imagesCSV": "one.jpg,two.jpg",
        **extra,
    }


class FakeProvider:
    name = "keepa"

    def __init__(self, products, tokens=1):
        self.products = products
        self.tokens = tokens
        self.discover_calls = []
        self.hydrate_calls = []

    def discover(self, recipe, page=0):
        self.discover_calls.append((recipe.recipe_id, page))
        asins = [item["asin"] for item in self.products]
        return DiscoveryPage(asins + asins[:1], page, False, TokenTelemetry(tokens_left=500, tokens_consumed=self.tokens))

    def hydrate(self, asins):
        assert len(asins) <= 100
        self.hydrate_calls.append(asins)
        selected = [item for item in self.products if item["asin"] in asins]
        return HydrationBatch(selected, TokenTelemetry(tokens_left=400, tokens_consumed=len(selected)))


class PagingProvider(FakeProvider):
    def discover(self, recipe, page=0):
        self.discover_calls.append((recipe.recipe_id, page))
        pages = [["P1", "P2"], ["P2", "P3"]]
        return DiscoveryPage(pages[page], page, page == 0, TokenTelemetry(tokens_left=500, tokens_consumed=1))


def test_dry_run_has_no_provider_or_network(tmp_path):
    db = tmp_path / "dry.sqlite3"
    init_db(db); upsert_store(profile(), db)
    result = SourcingEngine().preview("001", 5, db)
    assert result["dry_run"] is True
    assert result["network_requests"] == 0
    assert result["target_candidates"] == 5
    assert "trunk organizer" in result["keywords"]


def test_finder_query_units_recipes_and_guard():
    recipes = recipes_for_profile(profile())
    assert len(recipes) == 10
    query = finder_query(recipes[0])
    assert dollars_to_cents(30) == query["current_NEW_gte"] == 3000
    assert rating_to_keepa(4.0) == query["current_RATING_gte"] == 40
    assert query["isAdultProduct"] is False and query["isHazMat"] is False
    with pytest.raises(ValueError, match="10,000"):
        finder_query(recipes[0], 100)


def test_keepa_key_error_redaction_and_retry(monkeypatch):
    monkeypatch.delenv("KEEPA_API_KEY", raising=False)
    with pytest.raises(KeepaError, match="KEEPA_API_KEY"):
        KeepaProvider(api_key="")
    assert "real-key" not in redact_key("url?key=real-key", "real-key")
    calls = []
    def transport(method, url, body, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(url, 429 if len(calls) == 1 else 503, "x", {}, None)
        return {"asinList": [], "tokensLeft": 10}
    provider = KeepaProvider("real-key", transport=transport, sleep=lambda _: None)
    provider.discover(recipes_for_profile(profile())[0])
    assert len(calls) == 3


def test_timeout_retry_is_bounded():
    attempts = []
    def transport(method, url, body, timeout):
        attempts.append(1)
        raise TimeoutError("timed out")
    provider = KeepaProvider("key", transport=transport, sleep=lambda _: None, max_retries=2)
    with pytest.raises(KeepaError, match="after retries"):
        provider.discover(recipes_for_profile(profile())[0])
    assert len(attempts) == 3


def test_price_and_canonical_mapping_fallbacks():
    product = raw("B1", 4550)
    assert keepa_price(product) == 45.5
    canonical = keepa_to_canonical(product)
    assert canonical["price"] == 45.5
    assert canonical["_sourceKind"] == "KEEPA"
    assert len(canonical["images"]) == 2
    missing = raw("B2", -1)
    assert keepa_to_canonical(missing)["price"] is None


def test_fake_provider_e2e_dedupe_classify_checkpoint_and_source_aware_export(tmp_path):
    db = tmp_path / "한글 sourcing path" / "auto.sqlite3"
    init_db(db); upsert_store(profile(), db)
    products = [raw(f"B{i:03}", 3500 if i == 1 else 5000) for i in range(1, 6)]
    fake = FakeProvider(products)
    result = SourcingEngine(fake).run("001", 5, db=db)
    assert result["status"] == "DONE"
    assert result["discovered_asins"] == 5
    assert result["hydrated_products"] == 5
    assert result["inserted"] == 5
    assert len(fake.hydrate_calls) == 1
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 5
        assert con.execute("SELECT COUNT(*) FROM product_occurrences WHERE source_kind='KEEPA'").fetchone()[0] == 5
        assert con.execute("SELECT COUNT(DISTINCT asin) FROM sourcing_run_candidates").fetchone()[0] == 5
        assert con.execute("SELECT final_status FROM store_product_decisions d JOIN products p ON p.id=d.product_id WHERE p.asin='B001'").fetchone()[0] == "RESERVE_B"
        raw_occurrence = json.loads(con.execute("SELECT raw_json FROM product_occurrences LIMIT 1").fetchone()[0])
        canonical = json.loads(con.execute("SELECT raw_json FROM products LIMIT 1").fetchone()[0])
    assert "stats" in raw_occurrence and "stats" not in canonical

    handoff = SparkHandoffConnector().export(
        store_id="001", statuses=["PRIMARY"], limit=1,
        out_root=tmp_path / "spark output", job_id="KEEPA_TEST", db=db,
    )
    payload = json.loads(next(handoff.folder.glob("*.json")).read_text(encoding="utf-8"))
    assert "stats" not in payload
    manifest = json.loads(handoff.manifest_path.read_text(encoding="utf-8"))
    assert manifest["capability_status"] == KEEPA_TO_SPARK_CAPABILITY
    assert handoff.warnings


def test_pagination_dedupes_across_pages(tmp_path):
    db = tmp_path / "pages.sqlite3"
    init_db(db); upsert_store(profile(), db)
    provider = PagingProvider([raw("P1"), raw("P2"), raw("P3")])
    result = SourcingEngine(provider).run("001", 3, db=db)
    assert result["discovered_asins"] == 3
    assert provider.discover_calls[:2] == [("001-01", 0), ("001-01", 1)]


def test_token_budget_pause_and_cancel(tmp_path):
    db = tmp_path / "budget.sqlite3"
    init_db(db); upsert_store(profile(), db)
    paused = SourcingEngine(FakeProvider([raw("B1")], tokens=50)).run(
        "001", 1, db=db, max_tokens_per_run=10
    )
    assert paused["status"] == "PAUSED_TOKEN_BUDGET"
    with connect(db) as con:
        con.execute("UPDATE sourcing_runs SET status='PAUSED' WHERE run_id=?", (paused["run_id"],))
    cancelled = SourcingEngine.cancel(paused["run_id"], db)
    assert cancelled["status"] == "CANCELLED"


def test_resume_and_hydration_batches_are_at_most_100(tmp_path):
    db = tmp_path / "resume.sqlite3"
    init_db(db); upsert_store(profile(), db)
    products = [raw(f"X{i:03}") for i in range(205)]
    first = SourcingEngine(FakeProvider(products, tokens=20)).run(
        "001", 205, db=db, max_tokens_per_run=10
    )
    assert first["status"] == "PAUSED_TOKEN_BUDGET"
    provider = FakeProvider(products)
    resumed = SourcingEngine(provider).resume(first["run_id"], db)
    assert resumed["status"] == "DONE"
    assert [len(batch) for batch in provider.hydrate_calls] == [100, 100, 5]


def test_adult_and_hazmat_are_preserved_but_not_auto_approved(tmp_path):
    db = tmp_path / "risk.sqlite3"
    init_db(db); upsert_store(profile(), db)
    products = [raw("ADULT", isAdultProduct=True), raw("HAZMAT", isHazMat=True)]
    result = SourcingEngine(FakeProvider(products)).run("001", 2, db=db)
    assert result["status"] == "DONE"
    with connect(db) as con:
        rows = dict(con.execute("""SELECT p.asin,d.final_status FROM products p
            JOIN store_product_decisions d ON d.product_id=p.id""").fetchall())
    assert rows == {"ADULT": "RESTRICTED", "HAZMAT": "REVIEW"}


def test_missing_price_is_preserved_for_review(tmp_path):
    db = tmp_path / "missing-price.sqlite3"
    init_db(db); upsert_store(profile(), db)
    result = SourcingEngine(FakeProvider([raw("NOPRICE", -1)])).run("001", 1, db=db)
    assert result["classification"]["counts"] == {"REVIEW": 1}
    with connect(db) as con:
        row = con.execute("SELECT price,source_kind FROM products WHERE asin='NOPRICE'").fetchone()
    assert row["price"] is None and row["source_kind"] == "KEEPA"


def test_api_key_never_stored(tmp_path):
    db = tmp_path / "secret.sqlite3"
    init_db(db); upsert_store(profile(), db)
    key = "KEEPASECRET123"
    provider = FakeProvider([raw("B1")])
    provider.api_key = key
    SourcingEngine(provider).run("001", 1, db=db)
    assert key not in db.read_bytes().decode("utf-8", errors="ignore")
