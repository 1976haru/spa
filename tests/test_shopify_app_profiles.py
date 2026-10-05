import io
import json
import os
import sqlite3
import sys
import urllib.error

import pytest

from shopsource import shopify_auth as auth
from shopsource.shopify_collections import save_connection
from shopsource.shopify_scope_contract import scope_preflight


class FakeKeyring:
    values = {}
    @classmethod
    def set_password(cls, service, key, value): cls.values[(service, key)] = value
    @classmethod
    def get_password(cls, service, key): return cls.values.get((service, key))
    @classmethod
    def delete_password(cls, service, key): cls.values.pop((service, key), None)
    @staticmethod
    def get_keyring(): return type("WinCredentialManager", (), {})()


class Response:
    def __init__(self, payload): self.payload = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return self.payload


@pytest.fixture(autouse=True)
def fake_os_keyring(monkeypatch):
    FakeKeyring.values = {}
    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring)
    monkeypatch.setattr(os, "name", "nt", raising=False)
    with auth._CACHE_LOCK: auth._CACHE.clear()
    yield
    with auth._CACHE_LOCK: auth._CACHE.clear()


def grant_opener(*, fail=False):
    calls=[]
    def opener(request, timeout):
        calls.append(request.full_url)
        if fail:
            raise urllib.error.HTTPError(request.full_url,400,"bad",None,io.BytesIO(b'{"error":"shop_not_permitted"}'))
        assert request.full_url.endswith("/admin/oauth/access_token")
        return Response({"access_token":"synthetic-token","scope":"read_products read_themes","expires_in":3600})
    return opener, calls


def app_client(*, api_key="spark-client", app_id="gid://shopify/App/spark", shop_id="gid://shopify/Shop/1",
               domain="cabin-tidy.myshopify.com", scopes=("read_themes",)):
    class Client:
        def __init__(self,*args): pass
        def execute(self, query):
            assert "query ShopSourceAppIdentity" in query
            return {"app":{"id":app_id,"title":"spark","apiKey":api_key,
                    "requestedAccessScopes":[{"handle":"read_products"},{"handle":"read_themes"}],
                    "optionalAccessScopes":[{"handle":"write_themes"}]},
                "currentAppInstallation":{"id":"gid://shopify/AppInstallation/1",
                    "accessScopes":[{"handle":scope} for scope in scopes]},
                "shop":{"id":shop_id,"name":"Cabin Tidy","myshopifyDomain":"renamed.myshopify.com",
                    "primaryDomain":{"host":domain}}}
    return Client


def create_profile(db, *, opener=None, client_factory=None, client_id="spark-client"):
    save_connection("001","cabin-tidy.myshopify.com",db=db)
    opener, _ = (opener or grant_opener())
    return auth.verify_and_bind_app_profile("001","cabin-tidy.myshopify.com",client_id,"client-secret",
        display_name="ShopSource Production App",db=db,opener=opener,client_factory=client_factory or app_client(api_key=client_id))


def test_authenticated_wrong_app_is_distinct_and_safe(tmp_path):
    db=tmp_path/"wrong-app.sqlite3"
    opener,_=grant_opener()
    with pytest.raises(auth.ShopifyAuthError) as error:
        auth.verify_and_bind_app_profile("001","cabin-tidy.myshopify.com","spark-client","candidate-secret",
            db=db,opener=opener,client_factory=app_client(api_key="hps-client",app_id="gid://shopify/App/hps"))
    assert error.value.code=="APP_IDENTITY_MISMATCH"
    assert error.value.details["authenticated_app_title"]=="spark"
    assert not FakeKeyring.values
    assert not db.exists() or b"candidate-secret" not in db.read_bytes()


def test_client_id_api_key_mismatch_rejects_credential_persistence(tmp_path):
    db=tmp_path/"key-mismatch.sqlite3"; opener,_=grant_opener()
    with pytest.raises(auth.ShopifyAuthError) as error:
        auth.verify_and_bind_app_profile("001","cabin-tidy.myshopify.com","input-id","secret",db=db,
            opener=opener,client_factory=app_client(api_key="different-id"))
    assert error.value.code=="APP_IDENTITY_MISMATCH"
    assert not FakeKeyring.values


def test_expected_spark_app_gid_rejects_authenticated_hps_app(tmp_path):
    db=tmp_path/"dashboard-app-mismatch.sqlite3"
    profile=create_profile(db)
    opener,_=grant_opener()
    with pytest.raises(auth.ShopifyAuthError) as error:
        auth.verify_and_bind_app_profile("001","cabin-tidy.myshopify.com","spark-client","candidate-secret",
            profile_id=profile["profile_id"],db=db,opener=opener,
            client_factory=app_client(api_key="spark-client",app_id="gid://shopify/App/hps-automation"))
    assert error.value.code=="APP_IDENTITY_MISMATCH"
    assert error.value.details["authenticated_app_id"]=="gid://shopify/App/hps-automation"


def test_authoritative_app_store_identity_passes_and_persists_only_fingerprint(tmp_path):
    db=tmp_path/"pass.sqlite3"
    result=create_profile(db)
    raw=db.read_bytes()
    assert result["status"]=="VERIFIED" and result["app_title"]=="spark"
    assert result["shop_id"]=="gid://shopify/Shop/1"
    assert b"spark-client" not in raw and b"client-secret" not in raw and b"synthetic-token" not in raw
    assert FakeKeyring.values[(auth.KEYRING_SERVICE, f"app-profile:{result['profile_id']}:dev-dashboard-client")]


def test_expected_shop_gid_mismatch_blocks_binding(tmp_path):
    db=tmp_path/"store-mismatch.sqlite3"
    save_connection("001","cabin-tidy.myshopify.com",db=db)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shopify_connections SET shopify_shop_gid=? WHERE store_id=?",("gid://shopify/Shop/expected","001"))
    with pytest.raises(auth.ShopifyAuthError) as error:
        auth.verify_and_bind_app_profile("001","cabin-tidy.myshopify.com","spark-client","secret",db=db,
            opener=grant_opener()[0],client_factory=app_client())
    assert error.value.code=="STORE_IDENTITY_MISMATCH"
    assert not FakeKeyring.values


def test_bound_app_profile_reused_for_second_store_without_reentry(tmp_path):
    db=tmp_path/"multi.sqlite3"
    first=create_profile(db)
    opener,calls=grant_opener()
    result=auth.bind_existing_app_profile("002","second.myshopify.com",first["profile_id"],db=db,
        opener=opener,client_factory=app_client(shop_id="gid://shopify/Shop/2",domain="second.myshopify.com"))
    assert result["status"]=="VERIFIED" and result["app_id"]==first["app_id"]
    assert calls==["https://second.myshopify.com/admin/oauth/access_token"]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT app_profile_id,shopify_shop_gid FROM shopify_connections WHERE store_id='002'").fetchone()==(first["profile_id"],"gid://shopify/Shop/2")


def test_g0_scope_contract_does_not_require_writes():
    check=scope_preflight(["read_themes"],gate="G0_READ_ONLY")
    assert check["missing_for_current_gate"]==[]
    assert "write_products" in check["future_write"]
    assert "write_themes" in check["restricted_or_approval_required"]


def test_g0_missing_read_themes_waits_even_when_app_is_bound(tmp_path,monkeypatch):
    from shopsource.shopify_collections import ShopifyReadOnlyVerificationService
    db=tmp_path/"g0-scope.sqlite3"; created=create_profile(db)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shopify_connections SET scopes_json='[]' WHERE store_id='001'")
    monkeypatch.setattr("shopsource.shopify_collections.get_shopify_token",lambda *a,**k:("fixture-token","dev"))
    class ReadClient:
        def __init__(self,*args): pass
        def execute(self,query):
            assert "mutation" not in query.casefold()
            if "ShopSourceG0Identity" in query:
                return {"shop":{"id":created["shop_id"],"name":"Cabin Tidy","myshopifyDomain":"renamed.myshopify.com",
                    "primaryDomain":{"host":"cabin-tidy.myshopify.com"}},
                    "currentAppInstallation":{"accessScopes":[]}}
            pytest.fail("No publication/theme read should happen without the scope")
    service=ShopifyReadOnlyVerificationService(db=db,client_factory=ReadClient)
    result=service.verify("001")
    assert result["status"]=="WAITING_FOR_INPUT"
    assert result["theme_status"]=="MISSING_SCOPE"
    assert result["app_binding_status"]=="VERIFIED"


def test_authenticated_profile_token_is_verified_before_cache(tmp_path):
    db=tmp_path/"auth-provider.sqlite3"; result=create_profile(db)
    service=auth.ShopifyAuthService(db=db,opener=grant_opener()[0],client_factory=app_client())
    token=service.token_for("001")
    assert str(token)=="synthetic-token"
    status=service.status("001")
    assert status["production_app_bound"] is True
    assert status["authenticated_app_title"]=="spark"
    assert status["expected_app_gid"]==result["app_id"]


def test_shop_not_permitted_maps_to_external_org_requirement(tmp_path):
    db=tmp_path/"external.sqlite3"; save_connection("003","external.myshopify.com",db=db)
    auth.save_dev_credentials("003","external-id","secret")
    with pytest.raises(auth.ShopifyAuthError) as error:
        auth.ShopifyAuthService(db=db,opener=grant_opener(fail=True)[0]).token_for("003")
    assert error.value.code=="SHOP_NOT_PERMITTED"
    from shopsource.production_runner import ProductionEvidenceRunner
    from shopsource import shopify_collections
    runner=ProductionEvidenceRunner(db=db)
    class DeniedVerification:
        def __init__(self,**kwargs): pass
        def verify(self,store): raise auth.ShopifyAuthError("SHOP_NOT_PERMITTED","org setup required")
    original=shopify_collections.ShopifyReadOnlyVerificationService
    shopify_collections.ShopifyReadOnlyVerificationService=DeniedVerification
    try:
        assert runner._environment("001")["status"]=="EXTERNAL_ORG_OAUTH_REQUIRED"
    finally:
        shopify_collections.ShopifyReadOnlyVerificationService=original


def test_scope_contract_is_versioned_and_navigation_policy_scope_known():
    from shopsource.shopify_scope_contract import load_scope_contract
    contract=load_scope_contract()
    features=contract["features"]
    assert contract["contract_version"]=="2026-07.1"
    assert "read_online_store_navigation" in features["NAVIGATION"]["optional_read"]
    assert "write_online_store_navigation" in features["NAVIGATION"]["future_write"]
    assert features["THEME_WRITE"]["restricted_or_approval_required"]


def test_cli_bootstrap_is_opt_in_and_secret_free():
    readme=open("tools/shopify_app/README.md",encoding="utf-8").read()
    wrapper=open("tools/shopify_app/safe_shopify_cli.ps1",encoding="utf-8").read()
    assert "app config link" in readme and "ConfirmProductionDeploy" in wrapper
    assert "client_secret" in wrapper and "DEPLOY" in wrapper
