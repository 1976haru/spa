from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from shopsource.category_shortcut_readiness import CategoryShortcutReadinessService
from shopsource.category_strategy import (
    ExistingCollectionReconciliationService,
    HomepageCategoryStrategyService,
)
from shopsource.db import connect, init_db, upsert_store
from shopsource.homepage_automation import discover_homepage_sections
from shopsource.merchandising_policy import StoreMerchandisingPolicyService
from shopsource.shopify_collections import ShopifyCollectionPublisher, _normalized_hash, save_connection


def _section(name="t:names.collection_list", filename="sections/collection-list.liquid"):
    schema = {"name": name, "settings": [{"type": "collection_list", "id": "collections"}]}
    return {filename: "{% schema %}" + json.dumps(schema) + "{% endschema %}"}


def test_category_schema_detection_hyphenated_filename():
    result = discover_homepage_sections(_section())
    assert result["category"]["supports_4_cards"] is True
    assert result["category"]["mode"] == "COLLECTION_LIST"


def test_category_schema_detection_translatable_name():
    result = discover_homepage_sections(_section("t:names.collection_list"))
    assert result["category_status"] == "CATEGORY_SUPPORTED_HIGH_CONFIDENCE"


def test_category_schema_detection_underscored_filename():
    result = discover_homepage_sections(_section("t:names.unknown", "sections/collection_list.liquid"))
    assert result["category"]["mode"] == "COLLECTION_LIST"


def test_category_schema_detection_semantic_collection_list_setting():
    result = discover_homepage_sections(_section("t:names.unknown", "sections/generic.liquid"))
    assert result["category"]["matched_semantics"] == ["COLLECTION_LIST_SETTING", "COLLECTION_LINK", "IMAGE"]


def _strategy(prefix="home"):
    return {"items": [{
        "category_key": f"{prefix}-{i}", "collection_key": f"{prefix}-{i}", "title": f"{prefix.title()} {i}",
        "conditions": [{"field": "TITLE", "relation": "CONTAINS", "value": f"topic{i}"}],
        "preferred_handle": f"{prefix}-{i}", "evidence": {"matched_count": i},
    } for i in range(1, 5)]}


def _seed_catalog(db, store="a"):
    init_db(db)
    upsert_store({"store_id": store, "store_name": f"Store {store}", "category": "General"}, db)
    CategoryShortcutReadinessService(db)
    products = [{"shopify_product_id": f"gid://shopify/Product/{i}", "shopify_handle": f"p-{i}",
                 "title": f"topic{i} product", "product_type": f"topic{i}", "tags": [],
                 "remote_status": "ACTIVE", "eligible": True, "storefront_eligible": True,
                 "verification_status": "REMOTE_READ_VERIFIED"} for i in range(1, 5)]
    with connect(db) as con:
        con.execute("INSERT OR REPLACE INTO homepage_featured_product_remote_cache VALUES(?,?,?,?,?)",
                    (store, datetime.now(timezone.utc).isoformat(), 4, f"hash-{store}", json.dumps(products)))


def _collection_plan(db, store="a"):
    now = datetime.now(timezone.utc).isoformat()
    with connect(db) as con:
        con.execute("INSERT INTO store_collection_plans(plan_id,store_id,version,planner_version,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (f"plan-{store}", store, 1, "test", "DRAFT", now, now))
        for i in range(1, 5):
            cur = con.execute("""INSERT INTO store_collection_definitions
              (plan_id,collection_key,title,handle,priority,enabled,match_mode,created_at,updated_at)
              VALUES(?,?,?,?,?,1,'ANY',?,?)""", (f"plan-{store}", f"plan-{i}", f"Plan {i}", f"plan-{i}", i, now, now))
            con.execute("INSERT INTO store_collection_conditions(collection_definition_id,field,relation,value,group_operator,priority) VALUES(?,?,?,?,?,?)",
                        (cur.lastrowid, "TITLE", "CONTAINS", f"topic{i}", "OR", 0))


def test_approved_strategy_precedes_collection_plan(tmp_path):
    db = tmp_path / "s.sqlite3"; _seed_catalog(db); _collection_plan(db)
    service = HomepageCategoryStrategyService(db)
    service.approve(service.create_draft("a", _strategy(), source="SUGGESTED")["strategy_id"], confirmed=True)
    package = CategoryShortcutReadinessService(db).build("a", persist=False)
    assert package["candidate_source"] == "CATEGORY_STRATEGY"
    assert all(item["title"].startswith("Home") for item in package["items"])


def test_draft_strategy_does_not_override(tmp_path):
    db = tmp_path / "s.sqlite3"; _seed_catalog(db); _collection_plan(db)
    HomepageCategoryStrategyService(db).create_draft("a", _strategy(), source="SUGGESTED")
    assert CategoryShortcutReadinessService(db).build("a", persist=False)["candidate_source"] == "COLLECTION_PLAN"
    assert CategoryShortcutReadinessService(db).build("a", persist=False, simulate_draft=True)["draft_simulation"] is True


def test_fallback_category_blocks_preview():
    from shopsource.category_shortcut_readiness import readiness_summary
    package = {"theme_schema_status": "READY", "items": [
        {"mapping_status": "READY", "image_status": "READY", "candidate_status": "REVIEW_REQUIRED"}
        for _ in range(4)]}
    assert readiness_summary(package)["preview_enabled"] is False


def test_strategy_approval_supersedes_previous(tmp_path):
    service = HomepageCategoryStrategyService(tmp_path / "s.sqlite3")
    first = service.approve(service.create_draft("a", _strategy("one"))["strategy_id"], confirmed=True)
    second = service.approve(service.create_draft("a", _strategy("two"))["strategy_id"], confirmed=True)
    with connect(service.db) as con:
        assert con.execute("SELECT status FROM homepage_category_strategies WHERE strategy_id=?", (first["strategy_id"],)).fetchone()[0] == "SUPERSEDED"
    assert service.effective("a")["strategy_id"] == second["strategy_id"]


def test_strategy_store_isolation(tmp_path):
    service = HomepageCategoryStrategyService(tmp_path / "s.sqlite3")
    service.create_draft("a", _strategy("alpha")); service.create_draft("b", _strategy("beta"))
    assert service.latest("a")["strategy"]["items"][0]["title"].startswith("Alpha")
    assert service.latest("b")["strategy"]["items"][0]["title"].startswith("Beta")


def test_reconciliation_mapping_is_store_isolated(tmp_path):
    db = tmp_path / "s.sqlite3"; service = ExistingCollectionReconciliationService(db)
    proposal = service.propose("a", {"collection_key": "trunk", "title": "Trunk Organizers", "handle": "trunk-organizers"}, [_remote()])
    service.adopt(proposal, confirmed=True)
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM shopify_collection_mappings WHERE store_id='a'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM shopify_collection_mappings WHERE store_id='b'").fetchone()[0] == 0


def _remote(identifier="1", handle="trunk-organizers", title="Trunk Organizers", count=372):
    return {"id": f"gid://shopify/Collection/{identifier}", "handle": handle, "title": title,
            "products_count": count, "products_count_precision": "EXACT"}


def test_reconciliation_exact_match(tmp_path):
    service = ExistingCollectionReconciliationService(tmp_path / "s.sqlite3")
    assert service.propose("a", {"collection_key": "trunk", "title": "Trunk Organizers", "handle": "trunk-organizers"}, [_remote()])["status"] == "EXACT_MATCH"


def test_reconciliation_high_confidence_requires_user_confirmation(tmp_path):
    service = ExistingCollectionReconciliationService(tmp_path / "s.sqlite3")
    proposal = service.propose("a", {"collection_key": "trunk", "title": "Trunk Organizers", "handle": "store-trunk-organizers"}, [_remote()])
    assert proposal["status"] == "HIGH_CONFIDENCE_CANDIDATE"
    with pytest.raises(PermissionError): service.adopt(proposal)


def test_reconciliation_ambiguous_blocks_adoption(tmp_path):
    service = ExistingCollectionReconciliationService(tmp_path / "s.sqlite3")
    proposal = service.propose("a", {"collection_key": "trunk", "title": "Trunk Organizers"}, [_remote("1"), _remote("2", "trunk-2")])
    assert proposal["status"] == "AMBIGUOUS"
    with pytest.raises(ValueError): service.adopt(proposal, confirmed=True)


def test_existing_collection_adoption_writes_local_mapping_only(tmp_path):
    db = tmp_path / "s.sqlite3"; service = ExistingCollectionReconciliationService(db)
    proposal = service.propose("a", {"collection_key": "trunk", "title": "Trunk Organizers", "handle": "trunk-organizers"}, [_remote()])
    result = service.adopt(proposal, confirmed=True)
    assert result["shopify_write_performed"] is False
    with connect(db) as con:
        assert con.execute("SELECT handle FROM shopify_collection_mappings WHERE store_id='a'").fetchone()[0] == "trunk-organizers"


def test_adoption_does_not_invent_publication_ids(tmp_path):
    db = tmp_path / "s.sqlite3"; service = ExistingCollectionReconciliationService(db)
    proposal = service.propose("a", {"collection_key": "trunk", "title": "Trunk Organizers", "handle": "trunk-organizers"}, [_remote()])
    service.adopt(proposal, confirmed=True)
    with connect(db) as con:
        assert json.loads(con.execute("SELECT published_ids_json FROM shopify_collection_mappings").fetchone()[0]) == []


def test_collection_dry_run_uses_approved_strategy_conditions(tmp_path, monkeypatch):
    db = tmp_path / "s.sqlite3"; service = HomepageCategoryStrategyService(db)
    service.approve(service.create_draft("a", _strategy())["strategy_id"], confirmed=True)
    save_connection("a", "example.myshopify.com", db=db)
    class Client:
        def __init__(self, *args): pass
        def execute(self, query, variables=None): return {"collections": {"nodes": []}}
    monkeypatch.setattr("shopsource.shopify_collections.get_shopify_token", lambda *a, **k: ("token", "test"))
    result = ShopifyCollectionPublisher(db=db, client_factory=Client).dry_run(service.publisher_plan("a"))
    assert result["counts"]["CREATE"] == 4
    assert all(item["conditions"] for item in result["items"])


def test_non_automotive_strategy_portability(tmp_path):
    service = HomepageCategoryStrategyService(tmp_path / "s.sqlite3")
    service.create_draft("kitchen", _strategy("pantry"), source="SUGGESTED")
    text = json.dumps(service.latest("kitchen")["strategy"]).casefold()
    assert "pantry" in text and all(term not in text for term in ("trunk", "automotive", "car seat"))


def test_zero_effect_merch_policy_not_auto_approved(tmp_path):
    db = tmp_path / "s.sqlite3"; _seed_catalog(db)
    service = StoreMerchandisingPolicyService(db)
    draft = service.create_draft("a", "CATEGORY_SHORTCUTS", {"exclude_keywords": ["not-present"]})
    assert service.preview("a", draft["policy"])["excluded_count"] == 0
    assert service.effective_policy("a") is None


def test_no_shopify_write(tmp_path):
    service = ExistingCollectionReconciliationService(tmp_path / "s.sqlite3")
    proposal = service.propose("a", {"collection_key": "trunk", "title": "Trunk Organizers"}, [_remote()])
    assert proposal["remote_write_performed"] is False
