import io
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse

import pytest

from shopsource import shopify_auth as auth
from shopsource.shopify_collections import (
    DEV_DASHBOARD_CLIENT_CREDENTIALS, LEGACY_ADMIN_TOKEN, ShopifyGraphQLClient,
    get_connection, get_shopify_token, save_connection, save_shopify_token,
)


class FakeKeyring:
    values = {}

    @classmethod
    def set_password(cls, service, user, value): cls.values[(service, user)] = value
    @classmethod
    def get_password(cls, service, user): return cls.values.get((service, user))
    @classmethod
    def delete_password(cls, service, user): cls.values.pop((service, user), None)
    @staticmethod
    def get_keyring(): return type("WinCredentialManager", (), {})()


class Response:
    def __init__(self, payload): self.payload = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return self.payload


def identity_factory(client_id, shop_id="gid://shopify/Shop/1", domain="cabin-tidy.myshopify.com"):
    class IdentityClient:
        def __init__(self,*args): pass
        def execute(self,query):
            assert "query ShopSourceAppIdentity" in query
            return {"app":{"id":"gid://shopify/App/9","title":"Test Production App","apiKey":client_id,
                    "requestedAccessScopes":[{"handle":"read_themes"}],"optionalAccessScopes":[]},
                "currentAppInstallation":{"id":"gid://shopify/AppInstallation/8","accessScopes":[{"handle":"read_themes"}]},
                "shop":{"id":shop_id,"name":"Cabin Tidy","myshopifyDomain":domain,"primaryDomain":{"host":domain}}}
    return IdentityClient


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch):
    FakeKeyring.values = {}
    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring)
    monkeypatch.setattr(os, "name", "nt", raising=False)
    with auth._CACHE_LOCK: auth._CACHE.clear()
    yield
    with auth._CACHE_LOCK: auth._CACHE.clear()


def configured(db, store="001", mode=DEV_DASHBOARD_CLIENT_CREDENTIALS):
    return save_connection(store, "cabin-tidy.myshopify.com", auth_mode=mode, db=db)


def test_dev_dashboard_credentials_saved_securely_and_not_in_sqlite(tmp_path):
    db=tmp_path/"auth.sqlite3"; configured(db)
    auth.save_dev_credentials("001","client-id-private","client-secret-private")
    data=db.read_bytes()
    assert b"client-id-private" not in data and b"client-secret-private" not in data
    assert auth.credential_present("001",db=db,auth_mode=DEV_DASHBOARD_CLIENT_CREDENTIALS)
    assert get_connection("001",db=db)["auth_mode"]==DEV_DASHBOARD_CLIENT_CREDENTIALS


def test_client_credentials_token_request_form(tmp_path):
    db=tmp_path/"auth.sqlite3"; configured(db); auth.save_dev_credentials("001","client-id","client-secret")
    seen=[]
    def opener(request,timeout):
        seen.append((request.full_url,request.get_header("Content-type"),urllib.parse.parse_qs(request.data.decode()),timeout))
        return Response({"access_token":"ephemeral-token","scope":"read_themes read_products","expires_in":86399})
    token=auth.ShopifyAuthService(db=db,opener=opener,clock=lambda:1000,client_factory=identity_factory("client-id")).token_for("001")
    assert str(token)=="ephemeral-token"
    url,content_type,form,timeout=seen[0]
    assert url=="https://cabin-tidy.myshopify.com/admin/oauth/access_token"
    assert content_type=="application/x-www-form-urlencoded"
    assert form=={"grant_type":["client_credentials"],"client_id":["client-id"],"client_secret":["client-secret"]}
    assert timeout==20
    assert b"ephemeral-token" not in db.read_bytes()


def test_token_cached_until_safety_window_then_refreshed(tmp_path):
    db=tmp_path/"auth.sqlite3"; configured(db); auth.save_dev_credentials("001","id","secret")
    now={"value":1000}; calls=[]
    def opener(request,timeout):
        calls.append(1); return Response({"access_token":f"token-{len(calls)}","scope":"read_themes","expires_in":1000})
    service=auth.ShopifyAuthService(db=db,opener=opener,clock=lambda:now["value"],client_factory=identity_factory("id"))
    assert str(service.token_for("001"))==str(service.token_for("001"))=="token-1" and len(calls)==1
    now["value"]+=701
    assert str(service.token_for("001"))=="token-2" and len(calls)==2


def test_restart_requests_new_ephemeral_token(tmp_path):
    db=tmp_path/"auth.sqlite3"; configured(db); auth.save_dev_credentials("001","id","secret")
    calls=[]
    def opener(request,timeout): calls.append(1); return Response({"access_token":f"r{len(calls)}","scope":"read_themes","expires_in":86399})
    assert str(auth.ShopifyAuthService(db=db,opener=opener,client_factory=identity_factory("id")).token_for("001"))=="r1"
    with auth._CACHE_LOCK: auth._CACHE.clear()  # process restart: only OS credentials remain
    assert str(auth.ShopifyAuthService(db=db,opener=opener,client_factory=identity_factory("id")).token_for("001"))=="r2" and len(calls)==2


def http_error(code=401, body=b""):
    return urllib.error.HTTPError("https://shop.invalid",code,"error",None,io.BytesIO(body))


def test_401_refreshes_once_and_uses_new_token():
    calls=[]
    def opener(request,timeout):
        calls.append(request.get_header("X-shopify-access-token"))
        if len(calls)==1: raise http_error()
        return Response({"data":{"ok":True}})
    token=auth.ShopifyAccessToken("old-token",lambda:auth.ShopifyAccessToken("new-token",lambda:None))
    assert ShopifyGraphQLClient("fixture.myshopify.com",token,opener=opener).execute("query { shop { name } }")=={"ok":True}
    assert calls==["old-token","new-token"]


def test_auth_retry_is_bounded_to_one_refresh():
    calls=[]
    def opener(request,timeout): calls.append(1); raise http_error()
    token=auth.ShopifyAccessToken("old",lambda:auth.ShopifyAccessToken("new",lambda:None))
    with pytest.raises(RuntimeError,match="인증이 거부"):
        ShopifyGraphQLClient("fixture.myshopify.com",token,opener=opener).execute("query { shop { name } }")
    assert len(calls)==2


def test_legacy_401_does_not_retry():
    calls=[]
    def opener(request,timeout): calls.append(1); raise http_error()
    with pytest.raises(RuntimeError,match="인증이 거부"):
        ShopifyGraphQLClient("fixture.myshopify.com","static-legacy-token",opener=opener).execute("query { shop { name } }")
    assert len(calls)==1


def test_shop_not_permitted_has_friendly_safe_error(tmp_path):
    db=tmp_path/"auth.sqlite3"; configured(db); auth.save_dev_credentials("001","id","secret")
    def opener(request,timeout): raise http_error(400,b'{"error":"shop_not_permitted"}')
    with pytest.raises(auth.ShopifyAuthError,match="조직/설치") as error:
        auth.ShopifyAuthService(db=db,opener=opener).token_for("001")
    assert error.value.code=="SHOP_NOT_PERMITTED" and "secret" not in str(error.value)


def test_missing_dev_credentials_is_distinct(tmp_path):
    db=tmp_path/"auth.sqlite3"; configured(db)
    with pytest.raises(auth.ShopifyAuthError) as error: auth.ShopifyAuthService(db=db,opener=lambda *_:pytest.fail("network called")).token_for("001")
    assert error.value.code=="MISSING_CREDENTIALS"


def test_scope_readback_and_missing_scope_summary(tmp_path):
    db=tmp_path/"auth.sqlite3"; configured(db); auth.save_dev_credentials("001","id","secret")
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shopify_connections SET scopes_json=? WHERE store_id=?",(json.dumps(["read_themes","read_products"]),"001"))
    status=auth.ShopifyAuthService(db=db).status("001")
    assert status["granted_scopes"]==["read_themes","read_products"]
    assert status["missing_required_scopes"]==[]
    assert "write_products" in status["scope_preflight"]["future_write"]


def test_legacy_token_still_works(tmp_path):
    db=tmp_path/"auth.sqlite3"; save_connection("legacy","fixture.myshopify.com",db=db)
    save_shopify_token("legacy","static-admin-token")
    token,source=get_shopify_token("legacy",allow_environment=False,db=db)
    assert token=="static-admin-token" and source=="windows-credential-manager"
    assert get_connection("legacy",db=db)["auth_mode"]==LEGACY_ADMIN_TOKEN


def test_legacy_schema_migrates_without_losing_token(tmp_path):
    db=tmp_path/"old.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE shopify_connections (store_id TEXT PRIMARY KEY, shop_domain TEXT NOT NULL, api_version TEXT NOT NULL DEFAULT '2026-07', status TEXT NOT NULL DEFAULT 'NOT_VERIFIED', scopes_json TEXT NOT NULL DEFAULT '[]', publications_json TEXT NOT NULL DEFAULT '[]', last_verified_at TEXT, updated_at TEXT NOT NULL)")
        con.execute("INSERT INTO shopify_connections(store_id,shop_domain,updated_at) VALUES(?,?,?)",("legacy","fixture.myshopify.com","then"))
    save_shopify_token("legacy","old-token")
    connection=get_connection("legacy",db=db)
    assert connection["auth_mode"]==LEGACY_ADMIN_TOKEN
    assert get_shopify_token("legacy",allow_environment=False,db=db)[0]=="old-token"


def test_auth_health_never_contains_secrets(tmp_path):
    db=tmp_path/"auth.sqlite3"; configured(db); auth.save_dev_credentials("001","sensitive-client-id","sensitive-secret")
    status=auth.ShopifyAuthService(db=db).status("001")
    rendered=json.dumps(status)
    assert "sensitive-client-id" not in rendered and "sensitive-secret" not in rendered
    assert status["auth_mode"]==DEV_DASHBOARD_CLIENT_CREDENTIALS and status["credential_present"] is True


def test_production_g0_uses_read_only_identity_and_theme(tmp_path,monkeypatch):
    db=tmp_path/"g0.sqlite3"; configured(db); auth.save_dev_credentials("001","id","secret")
    from datetime import datetime,timezone
    stamp=datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO shopify_app_profiles(profile_id,display_name,expected_app_gid,expected_app_title,client_id_fingerprint,api_version,required_scopes_json,optional_scopes_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("profile-test","Test App","gid://shopify/App/9","Test App","fingerprint","2026-07",'["read_themes"]','[]',"VERIFIED",stamp,stamp))
        con.execute("UPDATE shopify_connections SET app_profile_id=? WHERE store_id=?",("profile-test","001"))
    monkeypatch.setattr("shopsource.shopify_collections.get_shopify_token",lambda *a,**k:("mock-token","dev-dashboard-client-credentials"))
    class Client:
        def __init__(self,*args): pass
        def execute(self,query):
            assert "query ShopSourceG0Identity" in query
            return {"shop":{"id":"gid://shopify/Shop/42","name":"Cabin Tidy",
                            "myshopifyDomain":"cabin-tidy.myshopify.com",
                            "primaryDomain":{"host":"cabin-tidy.myshopify.com","id":"gid://shopify/Domain/1"}},
                    "currentAppInstallation":{"accessScopes":[{"handle":"read_themes"}]}}
    monkeypatch.setattr("shopsource.shopify_collections.ShopifyGraphQLClient",Client)
    read_only_service = __import__("shopsource.shopify_collections", fromlist=["ShopifyReadOnlyVerificationService"]).ShopifyReadOnlyVerificationService
    monkeypatch.setattr("shopsource.shopify_collections.ShopifyReadOnlyVerificationService",
                        lambda **kwargs: read_only_service(client_factory=Client, **kwargs))
    monkeypatch.setattr("shopsource.homepage_collections.ShopifyThemeReader.discover",
                        lambda self,store:{"status":"CONNECTED","theme":{"name":"Published theme"},"scopes":["read_themes"]})
    from shopsource.production_runner import ProductionEvidenceRunner
    result=ProductionEvidenceRunner(db=db)._environment("001")
    assert result["status"]=="VERIFIED" and result["auth_mode"]==DEV_DASHBOARD_CLIENT_CREDENTIALS
    assert result["granted_scopes"]==["read_themes"] and result["secret_values_exposed"] is False


def test_production_g0_auto_continues_same_run_after_connection(tmp_path):
    from shopsource.production_runner import ProductionEvidenceRunner
    seen=[]
    collectors={key:(lambda store,run,key=key: seen.append(key) or {"status":"VERIFIED","verified":True})
                for key in ("ENVIRONMENT_STORE_IDENTITY","SOURCING_QUALITY","SOURCE_SAFETY","PRODUCT_CONTENT","PRODUCT_MEDIA",
                            "PRICING_MARGIN","COLLECTION_ARCHITECTURE","COLLECTION_CATEGORY_MEDIA",
                            "BRAND_HEADER_NAVIGATION","HOMEPAGE","PRODUCT_COLLECTION_TEMPLATES",
                            "PAGES_POLICIES","SEO_ACCESSIBILITY_MOBILE","COMMERCE_READINESS")}
    runner=ProductionEvidenceRunner(db=tmp_path/"continue.sqlite3",collectors=collectors)
    first=runner.start_or_resume("001")
    result=runner.run("001",run_id=first["run_id"])
    assert result["run_id"]==first["run_id"] and "SOURCING_QUALITY" in seen


def test_production_g0_ui_embeds_connection_setup_and_same_run_continue():
    source=open("src/shopsource/ui/v2.py",encoding="utf-8").read()
    assert "ENVIRONMENT_STORE_IDENTITY" in source and "save_and_continue_g0" in source
    assert "연결 정보 저장 후 Shopify 읽기 확인 / 같은 점검 계속" in source
    assert "Client Secret" in source


def test_no_real_network_and_protected_file_untouched(tmp_path,monkeypatch):
    from pathlib import Path
    protected=Path("stores/001_cabin_tidy.json")
    before=protected.read_bytes()
    db=tmp_path/"offline.sqlite3"; configured(db); auth.save_dev_credentials("001","id","secret")
    calls=[]
    def mock_urlopen(request,timeout):
        calls.append(request.full_url)
        return Response({"access_token":"fixture-token","scope":"read_themes","expires_in":86399})
    monkeypatch.setattr(auth.urllib.request,"urlopen",mock_urlopen)
    assert str(auth.ShopifyAuthService(db=db,client_factory=identity_factory("id")).token_for("001"))=="fixture-token"
    assert calls==["https://cabin-tidy.myshopify.com/admin/oauth/access_token"]
    assert protected.read_bytes()==before
