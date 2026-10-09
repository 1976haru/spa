from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from shopsource.category_shortcut_readiness import CategoryShortcutReadinessService
from shopsource.db import connect, init_db, upsert_store
from shopsource.merchandising_policy import (
    PURPOSE_CATEGORY_SHORTCUTS,
    StoreMerchandisingPolicyService,
    match_policy_exclusion,
    validate_policy,
)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "merchandising-policy.sqlite3"


def product(number, title, kind="General", **extra):
    return {
        "shopify_product_id": f"gid://shopify/Product/{number}",
        "shopify_handle": f"product-{number}", "title": title,
        "product_type": kind, "tags": [], "remote_status": "ACTIVE",
        "eligible": True, "storefront_eligible": True,
        "verification_status": "REMOTE_READ_VERIFIED", **extra,
    }


def seed_store(db, store_id, name, products, **profile):
    init_db(db)
    upsert_store({"store_id": store_id, "store_name": name,
                  "category": profile.pop("category", "General retail"), **profile}, db)
    CategoryShortcutReadinessService(db)
    with connect(db) as con:
        con.execute("INSERT INTO homepage_featured_product_remote_cache VALUES(?,?,?,?,?)",
                    (store_id, datetime.now(timezone.utc).isoformat(), len(products),
                     f"hash-{store_id}", json.dumps(products)))


def add_plan(db, store_id, definitions):
    now = datetime.now(timezone.utc).isoformat()
    with connect(db) as con:
        con.execute("""INSERT INTO store_collection_plans
            (plan_id,store_id,version,planner_version,status,created_at,updated_at)
            VALUES(?,?,1,'test','DRAFT',?,?)""", (f"plan-{store_id}", store_id, now, now))
        for priority, (key, title, match) in enumerate(definitions):
            cursor = con.execute("""INSERT INTO store_collection_definitions
                (plan_id,collection_key,title,handle,priority,enabled,match_mode,created_at,updated_at)
                VALUES(?,?,?,?,?,1,'ANY',?,?)""", (f"plan-{store_id}", key, title, key, priority, now, now))
            con.execute("""INSERT INTO store_collection_conditions
                (collection_definition_id,field,relation,value,group_operator,priority)
                VALUES(?,'TITLE','CONTAINS',?,'OR',0)""", (cursor.lastrowid, match))


def test_policy_draft_does_not_affect_readiness(db):
    seed_store(db, "shop-a", "Hearth & Order", [product(1, "Pantry replacement bin", "Pantry")])
    policy = StoreMerchandisingPolicyService(db)
    draft = policy.create_draft("shop-a", PURPOSE_CATEGORY_SHORTCUTS,
                                {"exclude_keywords": ["replacement"]})
    before = CategoryShortcutReadinessService(db).build("shop-a", persist=False)
    assert before["eligible_product_count"] == 1
    assert before["active_policy_id"] is None
    assert before["excluded_by_policy_count"] == 0
    assert policy.effective_policy("shop-a") is None
    assert policy.latest("shop-a", include_draft=True)["policy_id"] == draft["policy_id"]


def test_approved_policy_affects_only_its_store_and_store_switch_isolated(db):
    products = [product(2, "Pantry replacement bin", "Pantry")]
    seed_store(db, "shop-a", "Hearth & Order", products)
    seed_store(db, "shop-b", "Loom & Leaf", products)
    repository = StoreMerchandisingPolicyService(db)
    draft = repository.create_draft("shop-a", PURPOSE_CATEGORY_SHORTCUTS,
                                    {"exclude_keywords": ["replacement"]})
    repository.approve(draft["policy_id"], confirmed=True)
    a = CategoryShortcutReadinessService(db).build("shop-a", persist=False)
    b = CategoryShortcutReadinessService(db).build("shop-b", persist=False)
    assert a["eligible_product_count"] == 0
    assert a["active_policy_id"] == draft["policy_id"]
    assert a["active_policy_version"] == 1
    assert a["excluded_by_policy_count"] == 1
    assert b["eligible_product_count"] == 1
    assert b["active_policy_id"] is None
    assert b["excluded_by_policy_count"] == 0


def test_policy_version_supersedes_previous(db):
    seed_store(db, "shop-a", "Hearth & Order", [])
    service = StoreMerchandisingPolicyService(db)
    first = service.create_draft("shop-a", PURPOSE_CATEGORY_SHORTCUTS, {"exclude_keywords": ["old"]})
    service.approve(first["policy_id"], confirmed=True)
    second = service.create_draft("shop-a", PURPOSE_CATEGORY_SHORTCUTS, {"exclude_keywords": ["new"]})
    assert second["version"] == 2
    assert service.effective_policy("shop-a")["version"] == 1
    service.approve(second["policy_id"], confirmed=True)
    with connect(db) as con:
        statuses = [tuple(row) for row in con.execute(
            "SELECT version,status FROM store_merchandising_policies ORDER BY version")]
    assert statuses == [(1, "SUPERSEDED"), (2, "APPROVED")]
    assert service.effective_policy("shop-a")["policy_id"] == second["policy_id"]


def test_policy_keywords_phrases_patterns_normalized_and_statuses():
    policy = validate_policy({
        "exclude_keywords": ["  PANEL ", "panel", "  "],
        "exclude_phrases": ["Vehicle   specific component"],
        "exclude_title_patterns": [r"\btrim\s+panel\b"],
        "exclude_decision_statuses": [" restricted ", "RESTRICTED"],
    })
    assert policy["exclude_keywords"] == ["PANEL"]
    assert policy["exclude_decision_statuses"] == ["RESTRICTED"]
    assert match_policy_exclusion(product(1, "New PANEL insert"), policy) == "MERCH_POLICY_KEYWORD:PANEL"
    assert match_policy_exclusion(product(2, "Vehicle specific component"), policy) == "MERCH_POLICY_PHRASE:Vehicle specific component"
    assert match_policy_exclusion(product(3, "Trim panel assembly"),
                                  {**policy, "exclude_keywords": [], "exclude_phrases": []}) == "MERCH_POLICY_PATTERN:1"
    assert match_policy_exclusion(product(4, "Normal item", decision_status="restricted"), policy) == "MERCH_POLICY_DECISION_STATUS:RESTRICTED"


@pytest.mark.parametrize("pattern", [
    "(", r"(a+)+$", r"(x|xx)+y", r"\1abc", r"(?=bad)",
    "a?" * 9 + "b",
])
def test_invalid_or_unsafe_regex_rejected(pattern):
    with pytest.raises(ValueError):
        validate_policy({"exclude_title_patterns": [pattern]})


def test_policy_exclusion_reasons_reported(db):
    products = [product(10, "Filter replacement part"), product(11, "Safe product")]
    seed_store(db, "shop-a", "Hearth & Order", products)
    service = StoreMerchandisingPolicyService(db)
    draft = service.create_draft("shop-a", PURPOSE_CATEGORY_SHORTCUTS,
                                 {"exclude_phrases": ["replacement part"]})
    service.approve(draft["policy_id"], confirmed=True)
    package = CategoryShortcutReadinessService(db).build("shop-a", persist=False)
    assert package["policy_exclusion_reasons"] == {"MERCH_POLICY_PHRASE:replacement part": 1}
    assert package["excluded_by_policy_count"] == 1


def test_policy_preview_count_and_bounded_title_samples(db):
    seed_store(db, "shop-a", "Hearth & Order", [
        product(i, f"Replacement part {i}") for i in range(12)
    ] + [product(20, "Standard item")])
    result = StoreMerchandisingPolicyService(db).preview(
        "shop-a", {"exclude_keywords": ["replacement"]}, sample_limit=3)
    assert result["eligible_product_count"] == 13
    assert result["excluded_count"] == 12
    assert len(result["sample_excluded"]) == 3
    assert set(result["sample_excluded"][0]) == {"title", "reason"}


def test_cabin_tidy_like_fixture_policy_restores_repair_exclusion(db):
    products = [
        product(21, "Car trunk organizer", "Automotive storage"),
        product(22, "Center console organizer", "Automotive storage"),
        product(23, "Seat organizer", "Automotive storage"),
        product(24, "Car trash bin", "Automotive storage"),
        product(25, "Center console replacement panel", "Automotive part"),
        product(26, "Seat fitment trim", "Automotive part"),
        product(27, "Trunk repair component", "Automotive part"),
    ]
    seed_store(db, "fixture-cabin", "Cabin Tidy Fixture", products,
               primary_category="Automotive interior")
    add_plan(db, "fixture-cabin", [
        ("trunk", "Trunk Storage", "trunk"),
        ("console", "Console Storage", "console"),
        ("seat", "Seat Organization", "seat"),
        ("cleanup", "Cleanup", "trash"),
    ])
    repository = StoreMerchandisingPolicyService(db)
    draft = repository.create_draft("fixture-cabin", PURPOSE_CATEGORY_SHORTCUTS,
        {"exclude_keywords": ["replacement", "repair", "fitment"]})
    # A draft must leave the plan's raw matches intact.
    draft_package = CategoryShortcutReadinessService(db).build("fixture-cabin", persist=False)
    assert next(row for row in draft_package["items"] if row["collection_key"] == "console")["product_count"] == 2
    repository.approve(draft["policy_id"], confirmed=True)
    approved_package = CategoryShortcutReadinessService(db).build("fixture-cabin", persist=False)
    counts = {row["collection_key"]: row["product_count"] for row in approved_package["items"]}
    assert counts == {"trunk": 1, "console": 1, "seat": 1, "cleanup": 1}
    assert approved_package["active_policy_id"] == draft["policy_id"]
    assert approved_package["active_policy_version"] == 1
    assert approved_package["excluded_by_policy_count"] == 3


def test_non_automotive_store_not_affected_by_other_store_policy(db):
    seed_store(db, "fixture-cabin", "Cabin Tidy Fixture", [product(31, "Console replacement panel")])
    seed_store(db, "hearth", "Hearth & Order", [product(32, "Pantry replacement basket", "Pantry")])
    service = StoreMerchandisingPolicyService(db)
    cabin = service.create_draft("fixture-cabin", PURPOSE_CATEGORY_SHORTCUTS,
                                 {"exclude_keywords": ["replacement"]})
    service.approve(cabin["policy_id"], confirmed=True)
    isolated = CategoryShortcutReadinessService(db).build("hearth", persist=False)
    assert isolated["eligible_product_count"] == 1
    assert isolated["active_policy_id"] is None
    assert isolated["excluded_by_policy_count"] == 0


def test_store_profile_suggestion_is_draft_only_and_never_auto_approves(db):
    seed_store(db, "shop-a", "Hearth & Order", [], exclude_keywords=["explicit profile rule"])
    service = StoreMerchandisingPolicyService(db)
    draft = service.suggest_draft("shop-a")
    assert draft["source"] == "SUGGESTED"
    assert draft["status"] == "DRAFT"
    assert draft["policy"]["exclude_keywords"] == ["explicit profile rule"]
    assert service.effective_policy("shop-a") is None


def test_approval_requires_explicit_confirmation_and_policy_code_is_local_only(db, monkeypatch):
    seed_store(db, "shop-a", "Hearth & Order", [product(40, "Safe product")])
    service = StoreMerchandisingPolicyService(db)
    draft = service.create_draft("shop-a", PURPOSE_CATEGORY_SHORTCUTS, {})
    with pytest.raises(PermissionError):
        service.approve(draft["policy_id"], confirmed=False)
    monkeypatch.setattr("shopsource.shopify_collections.ShopifyGraphQLClient",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Shopify must not be called")))
    service.approve(draft["policy_id"], confirmed=True)
    assert CategoryShortcutReadinessService(db).build("shop-a", persist=False)["active_policy_id"] == draft["policy_id"]


def test_homepage_policy_review_ui_has_manual_approval_gate():
    source = (Path(__file__).parents[1] / "src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert "Review merchandising policy" in source
    assert "Suggest draft from Store Profile" in source
    assert "Approve selected draft" in source
    assert "confirmed=policy_approval_confirm.value is True" in source
    assert "Merchandising policy: REVIEW REQUIRED" in source
