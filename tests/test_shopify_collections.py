from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from pathlib import Path

import pytest

from shopsource.collection_images import ManualImageProvider, OpenAIImagesProvider, generate_collection_image, register_image_asset
import shopsource.collection_images as image_module
import shopsource.shopify_collections as shopify_module
from shopsource.shopify_collections import (
    COLLECTIONS_QUERY, CREATE_MUTATION, PUBLISH_MUTATION, ShopifyCollectionPublisher,
    ShopifyFileUploader, condition_source, get_connection, get_shopify_token, save_connection,
    save_shopify_token,
)


@pytest.fixture
def tmp_path():
    """Avoid the locked system TEMP ACL in this Windows managed runner."""
    path=Path.cwd()/"exports"/".test_scratch"/uuid.uuid4().hex
    path.mkdir(parents=True,exist_ok=False)
    return path


@pytest.fixture(autouse=True)
def isolate_exports(tmp_path, monkeypatch):
    root=tmp_path/"isolated-exports"
    monkeypatch.setattr(image_module,"EXPORT_DIR",root)
    monkeypatch.setattr(shopify_module,"EXPORT_DIR",root)


def definition(key="trunk", *, strategy="TITLE_FALLBACK", value="trunk organizer"):
    conditions=[{"field":"TITLE","relation":"CONTAINS","value":value,"group_operator":"OR"}]
    if strategy == "TAG_PREFERRED":
        conditions.insert(0,{"field":"TAG","relation":"EQUALS","value":"shopsource:trunk-organizers","group_operator":"OR"})
    return {"collection_key":key,"title":"Trunk Organizers","handle":"cabin-trunk-organizers",
            "description_html":"Practical organization.","conditions":conditions,"match_mode":"ANY",
            "rule_strategy":strategy,"estimated_product_count":12,"image_alt_text":"Trunk organizers"}


def plan(*definitions):
    return {"plan_id":"plan-test","store_id":"001","collections":list(definitions)}


class FakeShopify:
    def __init__(self, rows=None, *, fail_create=False):
        self.rows=rows or []
        self.calls=[]
        self.writes=[]
        self.fail_create=fail_create

    def __call__(self, domain, token, api_version):
        assert domain == "cabin-tidy.myshopify.com"
        assert token == "never-log-this-token"
        assert api_version == "2026-07"
        return self

    def execute(self, query, variables=None):
        variables=variables or {}; self.calls.append((query,variables))
        if "collections(first" in query:
            return {"collections":{"nodes":self.rows}}
        if "query CollectionCount" in query:
            return {"node":{"productsCount":{"count":12,"precision":"EXACT"}}}
        if "CollectionCreate(" in query:
            self.writes.append("create")
            if self.fail_create: raise RuntimeError("mock create failure")
            payload=variables["collection"]
            remote=self._remote(payload,"gid://shopify/Collection/1")
            self.rows.append(remote)
            return {"collectionCreate":{"collection":{"id":remote["id"],"title":remote["title"],"handle":remote["handle"],"descriptionHtml":remote["descriptionHtml"],"image":None,"productsCount":{"count":12,"precision":"EXACT"}},"userErrors":[]}}
        if "CollectionUpdate(" in query:
            self.writes.append("update")
            payload=variables["collection"]
            target=next(row for row in self.rows if row["id"]==payload["id"])
            target.update({k:v for k,v in payload.items() if k not in {"sourcesToUpdate","id"}})
            updates=payload.get("sourcesToUpdate",[])
            if updates:
                inp=updates[0]["condition"]
                source=next(x for x in target["sources"] if x["id"]==inp["id"])
                source["title"]=inp["title"]
                source["inclusion"]={"matchType":inp["inclusion"]["matchType"],"conditions":[self._condition(x) for x in inp["inclusion"]["conditionsToCreate"]]}
            return {"collectionUpdate":{"collection":{"id":target["id"],"title":target["title"],"handle":target["handle"],"descriptionHtml":target["descriptionHtml"],"image":None,"productsCount":{"count":12,"precision":"EXACT"}},"userErrors":[]}}
        if "stagedUploadsCreate" in query:
            return {"stagedUploadsCreate":{"stagedTargets":[{"url":"https://upload.test","resourceUrl":"https://storage.test/file.png","parameters":[]}],"userErrors":[]}}
        if "fileCreate" in query:
            return {"fileCreate":{"files":[{"id":"gid://shopify/MediaImage/1","status":"READY","image":{"url":"https://cdn.shopify.com/test.png","altText":"alt"}}],"userErrors":[]}}
        if "publishablePublish" in query:
            self.writes.append("publish")
            return {"publishablePublish":{"userErrors":[]}}
        if "currentAppInstallation" in query:
            return {"currentAppInstallation":{"accessScopes":[{"handle":s} for s in ["read_products","write_products","read_publications","write_publications","write_files"]]}}
        if "publications(" in query:
            return {"publications":{"nodes":[{"id":"gid://shopify/Publication/1","name":"Online Store"}]}}
        raise AssertionError(f"Unexpected GraphQL operation: {query}")

    @staticmethod
    def _condition(value):
        typename,payload=next(iter(value.items()))
        return {"id":"gid://shopify/Condition/1","__typename":"CollectionSourceInclusionCondition"+typename[0].upper()+typename[1:],**payload}

    @classmethod
    def _remote(cls,payload,shopify_id):
        source=payload["sources"][0]["source"]
        return {"id":shopify_id,"title":payload["title"],"handle":payload["handle"],"descriptionHtml":payload["descriptionHtml"],"image":None,
                "sources":[{"id":"gid://shopify/CollectionConditionsSource/1","__typename":"CollectionConditionsSource",
                            "inclusion":{"matchType":source["inclusion"]["matchType"],"conditions":[cls._condition(x) for x in source["inclusion"]["conditions"]]}}]}


@pytest.fixture
def setup_shopify(tmp_path, monkeypatch):
    db=tmp_path/"test.sqlite3"
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN","never-log-this-token")
    save_connection("001","cabin-tidy.myshopify.com",db=db)
    fake=FakeShopify()
    publisher=ShopifyCollectionPublisher(db=db,client_factory=fake)
    return db,fake,publisher


def test_shopify_collection_dry_run(setup_shopify):
    _,fake,publisher=setup_shopify
    result=publisher.dry_run(plan(definition()))
    assert result["counts"]["CREATE"] == 1
    assert fake.writes == []


def test_shopify_sync_rejects_stale_preview_before_any_write(setup_shopify):
    _,fake,publisher=setup_shopify
    p=plan(definition())
    preview=publisher.dry_run(p)
    preview["shop_domain"]="stale-shop.myshopify.com"
    with pytest.raises(RuntimeError,match="state changed"):
        publisher.sync(p,confirmed=True,expected_preview=preview)
    assert fake.writes == []


def test_shopify_collection_create(setup_shopify):
    _,fake,publisher=setup_shopify
    result=publisher.sync(plan(definition()),confirmed=True)
    assert result["summary"]["created"] == 1
    assert fake.writes == ["create"]


def test_shopify_collection_update_idempotent(setup_shopify):
    db,fake,publisher=setup_shopify
    initial=definition()
    publisher.sync(plan(initial),confirmed=True)
    changed={**definition(),"title":"Updated Trunk Organizers","description_html":"Updated description"}
    # Update local mapping remains stable by handle; remote has no manual drift because stored last hash is prior version.
    result=publisher.sync(plan(changed),confirmed=True)
    assert result["summary"]["updated"] == 1, result["items"][0].get("error")
    assert fake.writes == ["create","update"]


def test_shopify_collection_repeat_sync_no_duplicate(setup_shopify):
    _,fake,publisher=setup_shopify
    p=plan(definition())
    publisher.sync(p,confirmed=True)
    result=publisher.sync(p,confirmed=True)
    assert result["summary"]["unchanged"] == 1
    assert fake.writes == ["create"]
    assert len(fake.rows) == 1


def test_shopify_collection_drift_conflict(setup_shopify):
    _,fake,publisher=setup_shopify
    publisher.sync(plan(definition()),confirmed=True)
    fake.rows[0]["title"]="Merchant edited title"
    result=publisher.dry_run(plan(definition()))
    assert result["counts"]["CONFLICT"] == 1


def test_shopify_sources_conditions_mapping():
    mapped=condition_source(definition()["conditions"])
    assert mapped["conditions"][0]["productTitle"]["relation"] == "CONTAINS"
    assert mapped["matchType"] == "ANY"


def test_shopify_title_fallback_rule():
    mapped=condition_source(definition()["conditions"],prefer_tags=False)
    assert "productTitle" in mapped["conditions"][0]


def test_shopify_tag_preferred_rule():
    mapped=condition_source(definition(strategy="TAG_PREFERRED")["conditions"],prefer_tags=True)
    assert "productTag" in mapped["conditions"][0]


def test_collection_image_manual_provider(tmp_path):
    image=tmp_path/"manual.png"; image.write_bytes(b"fake png")
    provider=ManualImageProvider()
    result=provider.register("001","trunk",image,"Trunk storage")
    assert result["provider"] == "MANUAL"
    with pytest.raises(RuntimeError,match="does not generate"):
        provider.generate("prompt","1024x1024",tmp_path/"no.png")


def test_collection_image_openai_provider_mock(tmp_path):
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return json.dumps({"created":1,"data":[{"b64_json":base64.b64encode(b"image-bytes").decode()}]}).encode()
    seen={}
    def opener(request,timeout):
        seen["url"]=request.full_url
        assert b"image-bytes" not in request.data
        return Response()
    provider=OpenAIImagesProvider("test-api-key",opener=opener)
    out=provider.generate("lifestyle","1024x1024",tmp_path/"generated.png",enabled=True)
    assert Path(out["path"]).read_bytes()==b"image-bytes"
    assert seen["url"].endswith("/images/generations")


def test_collection_image_not_generated_without_opt_in(tmp_path):
    provider=OpenAIImagesProvider("secret")
    with pytest.raises(RuntimeError,match="opt-in"):
        provider.generate("prompt","1024x1024",tmp_path/"x.png")


def test_shopify_file_upload_mock(tmp_path, monkeypatch):
    image=tmp_path/"x.png"; image.write_bytes(b"img")
    fake=FakeShopify()
    class Response:
        status=204
        def __enter__(self): return self
        def __exit__(self,*args): pass
    monkeypatch.setattr("urllib.request.urlopen",lambda request,timeout: Response())
    result=ShopifyFileUploader(fake).upload(image,"alt text")
    assert result["url"].startswith("https://cdn.shopify.com/")


def test_collection_publish_online_store_mock(setup_shopify):
    _,fake,publisher=setup_shopify
    result=publisher.sync(plan(definition()),confirmed=True,publish_online_store=True)
    assert result["items"][0]["published_ids"] == ["gid://shopify/Publication/1"]
    assert "publish" in fake.writes


def test_one_failure_does_not_abort_batch(tmp_path,monkeypatch):
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN","never-log-this-token")
    db=tmp_path/"failure.sqlite3"; save_connection("001","cabin-tidy.myshopify.com",db=db)
    fake=FakeShopify(fail_create=True); publisher=ShopifyCollectionPublisher(db=db,client_factory=fake)
    rows=[definition("a"),{**definition("b"),"handle":"second"}]
    result=publisher.sync(plan(*rows),confirmed=True)
    assert result["summary"]["failed"] == 2
    assert len(result["items"]) == 2


def test_retry_failed_collections_only(tmp_path,monkeypatch):
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN","never-log-this-token")
    db=tmp_path/"retry.sqlite3"; save_connection("001","cabin-tidy.myshopify.com",db=db)
    fake=FakeShopify(fail_create=True); publisher=ShopifyCollectionPublisher(db=db,client_factory=fake)
    rows=[definition("a"),{**definition("b"),"handle":"second"}]
    publisher.sync(plan(*rows),confirmed=True)
    result=publisher.sync(plan(*rows),confirmed=True,retry_failed_only=True)
    assert result["summary"]["failed"] == 2
    assert len(fake.writes) == 4


def test_tokens_not_logged(setup_shopify, caplog):
    _,_,publisher=setup_shopify
    with caplog.at_level(logging.DEBUG):
        publisher.dry_run(plan(definition()))
    assert "never-log-this-token" not in caplog.text


def test_tokens_not_stored_plaintext(setup_shopify):
    db,_,_=setup_shopify
    content=db.read_bytes()
    assert b"never-log-this-token" not in content


def test_no_real_shopify_write_in_tests(setup_shopify):
    _,fake,publisher=setup_shopify
    result=publisher.dry_run(plan(definition()))
    assert result["counts"]["CREATE"] == 1
    assert not fake.writes
    assert all(not ("collectionCreate" in query or "collectionUpdate" in query or "publishablePublish" in query) for query,_ in fake.calls)


def test_secret_token_only_credential_store(tmp_path,monkeypatch):
    class Keyring:
        values={}
        @classmethod
        def set_password(cls,service,user,value): cls.values[(service,user)]=value
        @classmethod
        def get_password(cls,service,user): return cls.values.get((service,user))
        @staticmethod
        def get_keyring(): return type("WinCredentialManager",(),{})()
    monkeypatch.setitem(__import__("sys").modules,"keyring",Keyring)
    monkeypatch.setattr(os,"name","nt",raising=False)
    db=tmp_path/"secret.sqlite3"; save_connection("001","cabin-tidy.myshopify.com",db=db)
    save_shopify_token("001","secret-value")
    assert get_shopify_token("001",allow_environment=False)[0] == "secret-value"
    assert b"secret-value" not in db.read_bytes()
