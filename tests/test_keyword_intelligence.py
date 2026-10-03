import json
import sys
from types import SimpleNamespace

from shopsource.db import connect, init_db, upsert_store
from shopsource.intelligence.keyword_engine import KeywordEngine, _similarity
from shopsource.intelligence.keyword_scoring import lexical_semantic_fit, score_keyword
from shopsource.intelligence.text_features import extract_ngrams, normalize_keyword, tokenize
from shopsource.sourcing import credentials
from shopsource.sourcing.models import DiscoveryPage, HydrationBatch, TokenTelemetry
from shopsource.ui.v2_service import bulk_override, clear_bulk_override, get_app_setting, product_page, set_app_settings


def profile():
    return {
        "store_id": "001", "store_name": "Cabin Tidy", "category": "auto_interior",
        "minimum_fit_score": 0, "include_keywords": ["car", "organizer", "storage"],
        "exclude_keywords": ["motorcycle"], "risk_rules": [],
        "price_bands": [{"name": "primary", "min": 40, "max": 100, "status": "PRIMARY"}],
        "keyword_templates": {
            "places": ["trunk", "backseat", "center console", "seat gap"],
            "uses": ["storage"],
            "product_terms": ["organizer", "tray", "filler", "hook"],
            "seed_templates": ["{context} {place} {product}", "{place} {product}", "{product} for {place}"],
        },
    }


def test_tokenize_stopwords_asin_sizes_and_brand_suppression():
    assert tokenize("The ACME B012345678 organizer for 12 inch trunk", {"ACME"}) == ["organizer", "trunk"]
    phrases = dict(extract_ngrams(["ACME car trunk organizer B012345678 12 inch"], brands={"ACME"}))
    assert "car trunk organizer" in phrases
    assert all("acme" not in phrase and "b012345678" not in phrase for phrase in phrases)
    assert normalize_keyword("Car Trunk Organizer") == "car trunk organizer"


def test_profile_seed_keyword_recommendations_without_products(tmp_path):
    db = tmp_path / "keyword.sqlite3"
    init_db(db); upsert_store(profile(), db)
    recommendations = KeywordEngine(db, store_dir=tmp_path / "stores").recommend("001", 50)
    keywords = {item["keyword"].lower() for item in recommendations}
    assert len(recommendations) >= 10
    assert "trunk organizer" in keywords
    assert "center console tray" in keywords
    assert "seat gap filler" in keywords
    assert all(item["source"] in {"PROFILE", "NGRAM", "KEYBERT", "KEEPA_DISCOVERY"} for item in recommendations)
    assert all(item["reason"] and 0 <= item["score"] <= 100 for item in recommendations)


def test_fuzzy_similarity_and_explainable_risk_penalty():
    assert _similarity("car trunk organizer", "organizer trunk car") >= 0.84
    assert _similarity("trunk organizer", "cup holder") < 0.84
    assert lexical_semantic_fit("trunk organizer", {"trunk", "organizer", "vehicle"}) > 0
    safe = score_keyword(semantic_fit=.9, candidate_yield=80, price_fit=.8, quality_fit=.9, novelty=1, risk_rate=0)
    risky = score_keyword(semantic_fit=.9, candidate_yield=80, price_fit=.8, quality_fit=.9, novelty=1, risk_rate=.4)
    assert safe > risky


def test_risk_rules_reduce_candidate_score_and_explain_reason(tmp_path):
    store = profile()
    store["risk_rules"] = [{"code": "battery", "status": "REVIEW", "terms": ["battery pack"]}]
    store["include_keywords"].append("battery pack")
    db = tmp_path / "risk.sqlite3"
    init_db(db); upsert_store(store, db)
    items = KeywordEngine(db).recommend("001", 100)
    candidate = next(item for item in items if item["keyword"] == "battery pack")
    safe = next(item for item in items if item["keyword"] == "trunk organizer")
    assert candidate["risk_rate"] == 1
    assert candidate["score"] < safe["score"]
    assert any("위험 규칙" in reason for reason in candidate["reason"])


def test_recipe_add_remove_and_wizard_profile(tmp_path):
    db = tmp_path / "profile.sqlite3"
    init_db(db); upsert_store(profile(), db)
    engine = KeywordEngine(db, store_dir=tmp_path / "stores")
    added = engine.add_recipes("001", ["trunk organizer", "trunk organizer"])
    assert added["added"] == ["trunk organizer"]
    assert engine.remove_recipe("001", "trunk organizer")["removed"] == 1
    created = engine.store_wizard_profile(
        store_id="888", store_name="New Shop", category="storage", concept="home storage",
        price_min=20, price_max=80, include_keywords=["shelf"], exclude_keywords=["used"],
    )
    engine.save_wizard_profile(created)
    path = tmp_path / "stores" / "888_New_Shop.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["sourcing"]["target_candidates"] == 5


def test_existing_manual_recipe_and_excluded_keyword_are_respected(tmp_path):
    db = tmp_path / "recipes.sqlite3"
    store = profile()
    store["sourcing"] = {"recipes": [{"keyword": "rare seat pocket phrase"}], "excluded_keywords": []}
    init_db(db); upsert_store(store, db)
    engine = KeywordEngine(db, store_dir=tmp_path / "stores")
    rows = engine.recommend("001", 80)
    manual = next(row for row in rows if row["keyword"] == "rare seat pocket phrase")
    assert manual["source"] == "MANUAL"
    assert manual["status"] == "EXISTS"
    engine.exclude_recommendations("001", ["rare seat pocket phrase"])
    assert all(row["keyword"] != "rare seat pocket phrase" for row in engine.recommend("001", 80))


class FakeValidator:
    def __init__(self): self.calls = []
    def discover(self, recipe, page=0):
        self.calls.append(("discover", recipe.keyword))
        return DiscoveryPage(["DUP", "NEW"], page, False, TokenTelemetry(tokens_left=300, tokens_consumed=2))
    def hydrate(self, asins):
        self.calls.append(("hydrate", list(asins)))
        products = []
        for asin, price, rating, reviews in (("DUP", 5000, 45, 90), ("NEW", 15000, 35, 10)):
            current = [-1] * 19; current[1] = price; current[16] = rating; current[17] = reviews
            products.append({"asin": asin, "title": f"Car organizer {asin}", "stats": {"current": current}})
        return HydrationBatch(products, TokenTelemetry(tokens_left=250, tokens_consumed=2))


def test_keepa_validation_fake_provider_and_batch_limit(tmp_path):
    db = tmp_path / "validation.sqlite3"
    init_db(db); upsert_store(profile(), db)
    with connect(db) as con:
        con.execute("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    ("DUP", "Existing", "{}", "now", "now"))
    provider = FakeValidator()
    rows = KeywordEngine(db).validate("001", ["trunk organizer"], provider)
    assert rows[0]["candidate_yield"] == 2
    assert rows[0]["master_duplicate_rate"] == .5
    assert rows[0]["tokens_consumed"] == 4
    assert provider.calls[-1][0] == "hydrate"
    recommended = KeywordEngine(db).recommend("001", 80)
    assert next(row for row in recommended if row["keyword"] == "trunk organizer")["candidate_yield"] == 2
    try:
        KeywordEngine(db).validate("001", [f"keyword {index}" for index in range(11)], provider)
    except ValueError:
        pass
    else:
        raise AssertionError("validation above ten keywords must be rejected")


def test_product_pagination_and_bulk_override(tmp_path):
    db = tmp_path / "products.sqlite3"
    init_db(db); upsert_store(profile(), db)
    now = "2026-09-30T00:00:00Z"
    with connect(db) as con:
        for index in range(30):
            cur = con.execute("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                              (f"B{index:03}", f"Organizer {index}", "{}", now, now))
            con.execute("""INSERT INTO store_product_decisions(store_id,product_id,price_status,risk_status,
                auto_status,final_status,classified_at) VALUES(?,?,?,?,?,?,?)""",
                        ("001", cur.lastrowid, "PRIMARY", "SAFE", "PRIMARY", "PRIMARY", now))
    first = product_page(store_id="001", page=0, page_size=25, db=db)
    second = product_page(store_id="001", page=1, page_size=25, db=db)
    assert first["total"] == 30 and len(first["rows"]) == 25 and len(second["rows"]) == 5
    ids = [row["id"] for row in first["rows"][:2]]
    assert bulk_override("001", ids, "RESERVE_B", "bulk", db) == 2
    with connect(db) as con:
        assert {row["final_status"] for row in con.execute(
            "SELECT final_status FROM store_product_decisions WHERE store_id='001' AND product_id IN (?,?)", ids
        )} == {"RESERVE_B"}
    assert clear_bulk_override("001", ids, db) == 2
    with connect(db) as con:
        assert {row["final_status"] for row in con.execute(
            "SELECT final_status FROM store_product_decisions WHERE store_id='001' AND product_id IN (?,?)", ids
        )} == {"PRIMARY"}


def test_credential_adapter_uses_keyring_without_writing_files(monkeypatch, tmp_path):
    saved = {}
    class WinVaultKeyring:
        def set_password(self, service, username, password): saved[(service, username)] = password
        def get_password(self, service, username): return saved.get((service, username))
        def delete_password(self, service, username): saved.pop((service, username), None)
    fake = SimpleNamespace(
        get_keyring=lambda: WinVaultKeyring(),
        set_password=WinVaultKeyring().set_password,
        get_password=WinVaultKeyring().get_password,
        delete_password=WinVaultKeyring().delete_password,
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.delenv("KEEPA_API_KEY", raising=False)
    credentials.save_api_key("test-secret")
    assert credentials.get_api_key() == ("test-secret", "windows-credential-manager")
    assert list(tmp_path.iterdir()) == []
    credentials.delete_api_key()
    assert credentials.get_api_key() == (None, "missing")


def test_v2_registers_ten_pages_without_replacing_tkinter():
    from shopsource.ui.v2 import NAV_ITEMS, OperatorUI
    registered = {}
    class FakeUI:
        def page(self, path):
            return lambda handler: registered.setdefault(path, handler)
    instance = OperatorUI.__new__(OperatorUI)
    instance.ui = FakeUI()
    instance._register_pages()
    assert len(registered) == 10
    assert {path for path, _icon, _label in NAV_ITEMS} == set(registered)
    from shopsource.ui.app import main as tkinter_main
    assert callable(tkinter_main)


def test_operator_preferences_are_persisted_in_sqlite(tmp_path):
    db = tmp_path / "settings.sqlite3"
    set_app_settings({"default_target": 50, "default_token_budget": 2500, "theme": "Dark"}, db)
    assert get_app_setting("default_target", db=db) == "50"
    assert get_app_setting("default_token_budget", db=db) == "2500"
    assert get_app_setting("theme", db=db) == "Dark"
    assert get_app_setting("missing", "fallback", db) == "fallback"
