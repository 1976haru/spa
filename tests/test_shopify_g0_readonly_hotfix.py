import hashlib
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import pytest

import shopsource.shopify_collections as shopify
from shopsource.db import connect


class FakeAdmin:
    def __init__(self, domain="cabin-tidy.myshopify.com", primary_host="cabin-tidy.myshopify.com", scopes=None):
        self.domain = domain
        self.primary_host = primary_host
        self.scopes = scopes if scopes is not None else {"read_themes", "read_publications"}
        self.queries = []
        self.steps = []

    def __call__(self, domain, token, api_version):
        assert domain in {"cabin-tidy.myshopify.com", "kgxbpi-it.myshopify.com"}
        assert token == "private-fixture-token"
        assert api_version == "2026-07"
        return self

    def execute(self, query, variables=None):
        self.queries.append(query)
        assert "mutation" not in query.casefold()
        if "ShopSourceG0Identity" in query:
            self.steps.append("identity")
            return {"shop": {"id": "gid://shopify/Shop/42", "name": "Cabin Tidy",
                              "myshopifyDomain": self.domain,
                              "primaryDomain": {"host": self.primary_host, "id": "gid://shopify/Domain/1"}},
                    "currentAppInstallation": {"accessScopes": [{"handle": x} for x in sorted(self.scopes)]}}
        if "ShopSourcePublications" in query:
            self.steps.append("publications")
            return {"publications": {"nodes": [{"id": "gid://shopify/Publication/7", "name": "Online Store"}]}}
        raise AssertionError(f"Unexpected read query: {query}")


@pytest.fixture
def configured(tmp_path, monkeypatch):
    db = tmp_path / "g0.sqlite3"
    shopify.save_connection("001", "cabin-tidy.myshopify.com", auth_mode=shopify.DEV_DASHBOARD_CLIENT_CREDENTIALS, db=db)
    stamp=datetime.now(timezone.utc).isoformat()
    with connect(db) as con:
        con.execute("INSERT INTO shopify_app_profiles(profile_id,display_name,expected_app_gid,expected_app_title,client_id_fingerprint,api_version,required_scopes_json,optional_scopes_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("fixture-app","Fixture Production App","gid://shopify/App/fixture","Fixture Production App","fingerprint","2026-07",'["read_themes"]','[]',"VERIFIED",stamp,stamp))
        con.execute("UPDATE shopify_connections SET app_profile_id=? WHERE store_id=?",("fixture-app","001"))
    monkeypatch.setattr(shopify, "get_shopify_token", lambda *args, **kwargs: ("private-fixture-token", "mock"))
    return db


def run_g0(db, fake, *, theme_status="CONNECTED", theme=None):
    class ThemeReader:
        def __init__(self, *, db=None):
            self.db = db

        def discover(self, store_id):
            fake.steps.append("theme")
            return {"status": theme_status, "theme": theme or {"name": "Main Theme", "role": "MAIN"}}

    service = shopify.ShopifyReadOnlyVerificationService(db=db, client_factory=fake,
                                                         theme_reader_factory=ThemeReader)
    return service.verify("001")


def test_publications_query_uses_2026_07_supported_minimal_fields():
    assert "PublicationCatalog" not in shopify.PUBLICATIONS_QUERY
    assert "catalogType: APP" in shopify.PUBLICATIONS_QUERY
    assert "nodes { id name }" in shopify.PUBLICATIONS_QUERY
    assert "catalog {" not in shopify.PUBLICATIONS_QUERY


def test_read_only_verification_succeeds_with_2026_07_publication_fixture_and_no_write_scope(configured):
    fake = FakeAdmin(scopes={"read_themes", "read_publications"})
    result = run_g0(configured, fake)
    assert result["status"] == "VERIFIED"
    assert result["shop_domain_verified"] is True
    assert result["publications_status"] == "PASS"
    assert result["online_store_publications"][0]["name"] == "Online Store"
    assert "write_products" not in result["granted_scopes"]
    assert result["shop_id"] == "gid://shopify/Shop/42"
    assert result["primary_domain_host"] == "cabin-tidy.myshopify.com"
    assert result["shop_id_matches"] is None
    assert result["last_verified_at"]
    connection = shopify.get_connection("001", db=configured)
    assert connection["shopify_shop_gid"] == "gid://shopify/Shop/42"


def test_configured_primary_domain_matches_when_myshopify_domain_changed(configured):
    fake = FakeAdmin(domain="kgxbpi-it.myshopify.com", primary_host="cabin-tidy.myshopify.com")
    result = run_g0(configured, fake)
    assert result["status"] == "VERIFIED"
    assert result["shop_domain_verified"] is True
    assert result["actual_shop_domain"] == "kgxbpi-it.myshopify.com"


def test_configured_myshopify_domain_matches_when_primary_domain_is_different(configured):
    shopify.save_connection("001", "kgxbpi-it.myshopify.com", db=configured)
    fake = FakeAdmin(domain="kgxbpi-it.myshopify.com", primary_host="cabin-tidy.myshopify.com")
    result = run_g0(configured, fake)
    assert result["status"] == "VERIFIED"
    assert result["shop_domain_verified"] is True


def test_both_domain_candidates_mismatch_is_blocked(configured):
    fake = FakeAdmin(domain="remote.myshopify.com", primary_host="other.example.com")
    result = run_g0(configured, fake)
    assert result["status"] == "BLOCKED"
    assert result["shop_domain_verified"] is False
    assert result["publications_status"] == "NOT_CHECKED"
    assert len(fake.queries) == 1


def test_identity_publications_theme_read_order_and_no_mutation(configured):
    fake = FakeAdmin()
    result = run_g0(configured, fake)
    assert result["status"] == "VERIFIED"
    assert fake.steps == ["identity", "publications", "theme"]
    assert all("mutation" not in query.casefold() for query in fake.queries)


def test_saved_stable_shop_gid_mismatch_blocks(configured):
    with connect(configured) as con:
        con.execute("UPDATE shopify_connections SET shopify_shop_gid=? WHERE store_id=?",
                    ("gid://shopify/Shop/other", "001"))
    result = run_g0(configured, FakeAdmin())
    assert result["status"] == "BLOCKED"
    assert result["shop_id_matches"] is False


def test_g0_only_executes_read_queries(configured):
    fake = FakeAdmin(scopes={"read_themes", "read_publications"})
    result = run_g0(configured, fake)
    assert result["mutation_executed"] is False
    assert all("mutation" not in query.casefold() for query in fake.queries)


def test_missing_read_themes_waits_for_input_even_when_identity_is_valid(configured):
    fake = FakeAdmin(scopes={"read_publications"})
    result = run_g0(configured, fake)
    assert result["status"] == "WAITING_FOR_INPUT"
    assert result["missing_read_scopes"] == ["read_themes"]
    assert result["theme_status"] == "MISSING_SCOPE"
    assert shopify.get_connection("001", db=configured)["shopify_shop_gid"] == "gid://shopify/Shop/42"


def test_app_identity_mismatch_is_not_connected_or_verified(configured, monkeypatch):
    from shopsource.shopify_auth import ShopifyAuthError
    monkeypatch.setattr(shopify, "get_shopify_token", lambda *a, **k: (_ for _ in ()).throw(
        ShopifyAuthError("APP_IDENTITY_MISMATCH", "authenticated hps app", {
            "authenticated_app_id":"gid://shopify/App/hps", "authenticated_app_title":"hps-automation"})))
    result=shopify.ShopifyReadOnlyVerificationService(db=configured).verify("001")
    assert result["status"]=="APP_IDENTITY_MISMATCH"
    assert result["app_binding_status"]=="VERIFIED"
    assert result["expected_app_gid"]=="gid://shopify/App/fixture"
    assert result["authenticated_app_title"]=="hps-automation"
    assert shopify.get_connection("001",db=configured)["status"]=="APP_IDENTITY_MISMATCH"


def test_wrong_myshopify_domain_is_blocked_before_downstream_reads(configured):
    fake = FakeAdmin(domain="wrong-shop.myshopify.com", primary_host="other-shop.myshopify.com")
    result = run_g0(configured, fake)
    assert result["status"] == "BLOCKED"
    assert result["shop_domain_verified"] is False
    assert result["publications_status"] == "NOT_CHECKED"
    assert len(fake.queries) == 1


def test_identity_and_theme_pass_without_write_products(configured):
    fake = FakeAdmin(scopes={"read_themes"})
    result = run_g0(configured, fake)
    assert result["status"] == "VERIFIED"
    assert "write_products" in result["missing_future_write_scopes"]
    assert result["publications_status"] == "MISSING_SCOPE"


def test_token_and_secret_never_appear_in_verification_result_or_logs(configured, caplog):
    fake = FakeAdmin()
    result = run_g0(configured, fake)
    rendered = json.dumps(result) + caplog.text
    assert "private-fixture-token" not in rendered
    assert "client-secret" not in rendered


def test_settings_verification_uses_g0_service_not_collection_write_capability_verify():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    button_section = source.split('def verify_shopify():', 1)[1].split('ui.button("Shopify', 1)[0]
    assert "ShopifyReadOnlyVerificationService" in button_section
    assert "ShopifyCollectionPublisher().verify" not in button_section


def test_protected_store_file_is_byte_for_byte_unchanged():
    path = Path("stores/001_cabin_tidy.json")
    # The main worktree may carry a protected local operator edit; a parallel
    # worktree instead starts from its tracked HEAD version. Accept either
    # known-safe starting point while still detecting changes made by tests.
    local_operator_hash = "9a480be4cd24bf93beab8e02483512db3801f10f506adc610c38667c5832051f"
    tracked_hash = subprocess.run(
        ["git", "hash-object", "--path=stores/001_cabin_tidy.json", "stores/001_cabin_tidy.json"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    head_hash = subprocess.run(
        ["git", "rev-parse", "HEAD:stores/001_cabin_tidy.json"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert (hashlib.sha256(path.read_bytes()).hexdigest() == local_operator_hash
            or tracked_hash == head_hash)
