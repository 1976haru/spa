import hashlib
import json
from pathlib import Path

import pytest

import shopsource.shopify_collections as shopify
from shopsource.db import connect


class FakeAdmin:
    def __init__(self, domain="cabin-tidy.myshopify.com", scopes=None):
        self.domain = domain
        self.scopes = scopes if scopes is not None else {"read_themes", "read_publications"}
        self.queries = []

    def __call__(self, domain, token, api_version):
        assert domain == "cabin-tidy.myshopify.com"
        assert token == "private-fixture-token"
        assert api_version == "2026-07"
        return self

    def execute(self, query, variables=None):
        self.queries.append(query)
        assert "mutation" not in query.casefold()
        if "ShopSourceG0Identity" in query:
            return {"shop": {"name": "Cabin Tidy", "myshopifyDomain": self.domain},
                    "currentAppInstallation": {"accessScopes": [{"handle": x} for x in sorted(self.scopes)]}}
        if "ShopSourcePublications" in query:
            return {"publications": {"nodes": [{"id": "gid://shopify/Publication/7", "name": "Online Store"}]}}
        raise AssertionError(f"Unexpected read query: {query}")


@pytest.fixture
def configured(tmp_path, monkeypatch):
    db = tmp_path / "g0.sqlite3"
    shopify.save_connection("001", "cabin-tidy.myshopify.com", auth_mode=shopify.DEV_DASHBOARD_CLIENT_CREDENTIALS, db=db)
    monkeypatch.setattr(shopify, "get_shopify_token", lambda *args, **kwargs: ("private-fixture-token", "mock"))
    return db


def run_g0(db, fake, *, theme_status="CONNECTED", theme=None):
    class ThemeReader:
        def __init__(self, *, db=None):
            self.db = db

        def discover(self, store_id):
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
    assert result["last_verified_at"]


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


def test_wrong_myshopify_domain_is_blocked_before_downstream_reads(configured):
    fake = FakeAdmin(domain="wrong-shop.myshopify.com")
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
    assert hashlib.sha256(path.read_bytes()).hexdigest() == "9a480be4cd24bf93beab8e02483512db3801f10f506adc610c38667c5832051f"
