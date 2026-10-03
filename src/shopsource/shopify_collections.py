"""Shopify collection publishing with explicit dry-run/confirmation boundaries.

The module keeps credentials in the OS credential store and persists only
non-secret connection/mapping metadata. Network operations are injectable for
tests; no operation is invoked merely by importing this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .db import connect, init_db
from .paths import EXPORT_DIR

SHOPIFY_API_VERSION = "2026-07"
REQUIRED_SCOPES = {"write_products", "read_products", "read_publications"}
PUBLISH_SCOPE = "write_publications"
FILE_SCOPES = {"write_files"}
KEYRING_SERVICE = "ShopSourceStudio.Shopify"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_domain(value: str) -> str:
    domain = str(value or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", domain):
        raise ValueError("Shopify domain must be a *.myshopify.com shop domain")
    return domain


def _credential_key(store_id: str) -> str:
    return f"{store_id}:admin-access-token"


def save_shopify_token(store_id: str, token: str) -> None:
    if not token or not token.strip():
        raise ValueError("Shopify access token is empty")
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("keyring optional dependency is required; alternatively set SHOPIFY_ACCESS_TOKEN for a single-store session") from exc
    if os.name == "nt" and "win" not in type(keyring.get_keyring()).__name__.lower():
        raise RuntimeError("Windows Credential Manager backend unavailable; use SHOPIFY_ACCESS_TOKEN for this session")
    keyring.set_password(KEYRING_SERVICE, _credential_key(store_id), token.strip())


def delete_shopify_token(store_id: str) -> None:
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, _credential_key(store_id))
    except Exception:
        return


def get_shopify_token(store_id: str, *, allow_environment: bool = True) -> tuple[str | None, str]:
    if allow_environment and os.environ.get("SHOPIFY_ACCESS_TOKEN"):
        return os.environ["SHOPIFY_ACCESS_TOKEN"], "environment"
    try:
        import keyring
        value = keyring.get_password(KEYRING_SERVICE, _credential_key(store_id))
        if value:
            return value, "windows-credential-manager"
    except Exception:
        pass
    return None, "missing"


def _install_schema(db=None):
    init_db(db)
    with connect(db) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS shopify_connections (
            store_id TEXT PRIMARY KEY, shop_domain TEXT NOT NULL,
            api_version TEXT NOT NULL DEFAULT '2026-07', status TEXT NOT NULL DEFAULT 'NOT_VERIFIED',
            scopes_json TEXT NOT NULL DEFAULT '[]', publications_json TEXT NOT NULL DEFAULT '[]',
            last_verified_at TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shopify_collection_mappings (
            store_id TEXT NOT NULL, collection_key TEXT NOT NULL, handle TEXT NOT NULL,
            shopify_collection_id TEXT NOT NULL, last_synced_hash TEXT NOT NULL,
            last_synced_at TEXT NOT NULL, published_ids_json TEXT NOT NULL DEFAULT '[]',
            image_url TEXT, PRIMARY KEY(store_id, collection_key), UNIQUE(store_id, handle)
        );
        CREATE TABLE IF NOT EXISTS collection_image_assets (
            store_id TEXT NOT NULL, collection_key TEXT NOT NULL, path TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', alt_text TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            PRIMARY KEY(store_id, collection_key)
        );
        CREATE TABLE IF NOT EXISTS collection_sync_runs (
            run_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, plan_id TEXT NOT NULL,
            result_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)


def save_connection(store_id: str, shop_domain: str, *, api_version: str = SHOPIFY_API_VERSION, db=None) -> dict:
    if api_version != SHOPIFY_API_VERSION:
        raise ValueError(f"Only supported pinned Shopify API version {SHOPIFY_API_VERSION} is enabled")
    domain = _safe_domain(shop_domain)
    _install_schema(db)
    with connect(db) as con:
        con.execute("""INSERT INTO shopify_connections(store_id,shop_domain,api_version,updated_at)
          VALUES(?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET shop_domain=excluded.shop_domain,
          api_version=excluded.api_version,updated_at=excluded.updated_at""", (store_id, domain, api_version, _now()))
    return get_connection(store_id, db=db)


def get_connection(store_id: str, *, db=None) -> dict | None:
    _install_schema(db)
    with connect(db) as con:
        row = con.execute("SELECT * FROM shopify_connections WHERE store_id=?", (store_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["scopes"] = json.loads(result.pop("scopes_json"))
    result["publications"] = json.loads(result.pop("publications_json"))
    result["credential_source"] = get_shopify_token(store_id)[1]
    return result


def condition_source(conditions: list[dict], match_mode: str = "ANY", *, prefer_tags: bool = False) -> dict:
    """Map Phase 3.2 canonical rules to Shopify 2026-07 source conditions."""
    mapped = []
    tag_conditions = [c for c in conditions if c.get("field") == "TAG"]
    title_conditions = [c for c in conditions if c.get("field") == "TITLE"]
    selected = (tag_conditions + title_conditions) if prefer_tags and tag_conditions else conditions
    if prefer_tags and not tag_conditions:
        selected = title_conditions or conditions
    for row in selected:
        field = row.get("field")
        if field not in {"TITLE", "TAG", "PRODUCT_TYPE", "VENDOR"}:
            continue
        canonical = str(row.get("relation", "")).upper()
        if field == "TAG":
            relation = {"EQUALS":"TAGGED_WITH", "NOT_EQUALS":"NOT_TAGGED_WITH"}.get(canonical)
        else:
            relation = {"EQUALS":"EQUALS", "NOT_EQUALS":"NOT_EQUALS", "CONTAINS":"CONTAINS",
                        "NOT_CONTAINS":"DOES_NOT_CONTAIN", "STARTS_WITH":"STARTS_WITH", "ENDS_WITH":"ENDS_WITH"}.get(canonical)
        if not relation:
            continue
        key = {"TITLE": "productTitle", "TAG": "productTag", "PRODUCT_TYPE": "productType", "VENDOR": "productVendor"}[field]
        mapped.append({key: {"relation": relation, "values": [str(row.get("value", ""))], "matchType": "ANY"}})
    if not mapped:
        raise ValueError("Collection has no supported Shopify source conditions")
    return {"conditions": mapped, "matchType": "ALL" if str(match_mode).upper() == "ALL" else "ANY"}


def _condition_hash(collection: dict) -> str:
    stable = {"title": collection.get("title"), "descriptionHtml": collection.get("description_html") or "",
              "handle": collection.get("handle"), "sourceMatchType": "ALL" if str(collection.get("match_mode", "ANY")).upper() == "ALL" else "ANY",
              "conditions": _expected_fingerprint(collection)}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _expected_fingerprint(collection: dict) -> list[dict]:
    source = condition_source(collection.get("conditions", []), collection.get("match_mode", "ANY"),
                              prefer_tags=collection.get("rule_strategy") in {"TAG_PREFERRED", "MIXED"})
    result=[]
    for rule in source["conditions"]:
        typename, payload=next(iter(rule.items()))
        result.append({"type":typename,"relation":payload["relation"],"values":payload["values"],"matchType":payload["matchType"]})
    return sorted(result,key=lambda row:json.dumps(row,sort_keys=True))


def _normalized_hash(remote: dict) -> str:
    sources = remote.get("sources") or []
    conditions = []
    for source in sources:
        inclusion = source.get("inclusion") or {}
        for condition in inclusion.get("conditions") or []:
            conditions.append(condition)
    normalized_conditions=[]
    for condition in conditions:
        typename=str(condition.get("__typename", ""))
        type_key=typename.replace("CollectionSourceInclusionCondition", "")
        type_key=type_key[:1].lower()+type_key[1:] if type_key else ""
        relation_key={"productTitle":"titleRelation","productTag":"tagRelation",
                      "productType":"typeRelation","productVendor":"vendorRelation"}.get(type_key,"relation")
        normalized_conditions.append({"type":type_key,"relation":condition.get(relation_key, condition.get("relation")),
                                      "values":sorted(condition.get("values") or []),"matchType":condition.get("matchType", "ANY")})
    inclusion_match = next(((source.get("inclusion") or {}).get("matchType") for source in sources
                            if source.get("__typename") == "CollectionConditionsSource"), "ANY")
    normalized = {"title": remote.get("title"), "descriptionHtml": remote.get("descriptionHtml") or "",
                  "handle": remote.get("handle"), "sourceMatchType": inclusion_match,
                  "conditions": sorted(normalized_conditions,key=lambda row:json.dumps(row,sort_keys=True))}
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ShopifyGraphQLClient:
    """Small stdlib transport; GraphQL errors never include request headers/tokens."""
    def __init__(self, shop_domain: str, token: str, api_version: str = SHOPIFY_API_VERSION,
                 opener: Callable = urllib.request.urlopen):
        self.domain, self.token, self.version, self.opener = _safe_domain(shop_domain), token, api_version, opener

    def execute(self, query: str, variables: dict | None = None) -> dict:
        url = f"https://{self.domain}/admin/api/{self.version}/graphql.json"
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "X-Shopify-Access-Token": self.token})
        for attempt in range(3):
            try:
                with self.opener(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(.25 * (2 ** attempt)); continue
                raise RuntimeError(f"Shopify HTTP {exc.code}; response body suppressed") from None
            except Exception as exc:
                if isinstance(exc, (urllib.error.URLError, TimeoutError)) and attempt < 2:
                    time.sleep(.25 * (2 ** attempt)); continue
                raise RuntimeError(f"Shopify request failed: {type(exc).__name__}") from None
        if payload.get("errors"):
            messages = [str(err.get("message", "GraphQL error"))[:240] for err in payload["errors"]]
            raise RuntimeError("Shopify GraphQL: " + "; ".join(messages))
        return payload.get("data") or {}


SCOPES_QUERY = "query { currentAppInstallation { accessScopes { handle } } }"
PUBLICATIONS_QUERY = "query { publications(first: 100) { nodes { id name catalog { title ... on PublicationCatalog { publication { id } } } } } }"
COLLECTIONS_QUERY = "query { collections(first: 250) { nodes { id title handle descriptionHtml image { url altText } productsCount { count precision } sources { __typename id ... on CollectionConditionsSource { inclusion { matchType conditions { id __typename ... on CollectionSourceInclusionConditionProductTitle { titleRelation: relation values matchType } ... on CollectionSourceInclusionConditionProductTag { tagRelation: relation values matchType } ... on CollectionSourceInclusionConditionProductType { typeRelation: relation values matchType } ... on CollectionSourceInclusionConditionProductVendor { vendorRelation: relation values matchType } } } } } } } }"
CREATE_MUTATION = """mutation CollectionCreate($collection: CollectionCreateInput!) {
 collectionCreate(collection: $collection) { collection { id title handle descriptionHtml image { url altText } productsCount { count precision } } userErrors { field message } }
}"""
UPDATE_MUTATION = """mutation CollectionUpdate($collection: CollectionUpdateInput!) {
 collectionUpdate(collection: $collection) { collection { id title handle descriptionHtml image { url altText } productsCount { count precision } } userErrors { field message } }
}"""
PUBLISH_MUTATION = """mutation PublishablePublish($id: ID!, $input: [PublicationInput!]!) {
 publishablePublish(id: $id, input: $input) { userErrors { field message } }
}"""


class ShopifyCollectionPublisher:
    def __init__(self, *, db=None, client_factory=ShopifyGraphQLClient):
        self.db, self.client_factory = db, client_factory
        _install_schema(db)

    def _client(self, store_id: str):
        config = get_connection(store_id, db=self.db)
        if not config:
            raise RuntimeError("Shopify shop domain is not configured")
        token, source = get_shopify_token(store_id)
        if not token:
            raise RuntimeError("Shopify credential missing; token is never stored in ShopSource DB")
        return config, self.client_factory(config["shop_domain"], token, config["api_version"]), source

    def verify(self, store_id: str) -> dict:
        config, client, _ = self._client(store_id)
        scopes_data = client.execute(SCOPES_QUERY).get("currentAppInstallation") or {}
        scopes = sorted({row.get("handle", "") for row in scopes_data.get("accessScopes", []) if row.get("handle")})
        pubs = client.execute(PUBLICATIONS_QUERY).get("publications", {}).get("nodes", [])
        online = [row for row in pubs if "online store" in str(row.get("name", "")).casefold()]
        missing = sorted(REQUIRED_SCOPES.difference(scopes))
        missing_optional = sorted({PUBLISH_SCOPE, *FILE_SCOPES}.difference(scopes))
        with connect(self.db) as con:
            con.execute("UPDATE shopify_connections SET status=?,scopes_json=?,publications_json=?,last_verified_at=?,updated_at=? WHERE store_id=?",
                        ("CONNECTED" if not missing else "MISSING_SCOPES", json.dumps(scopes), json.dumps(pubs), _now(), _now(), store_id))
        return {"status": "CONNECTED" if not missing else "MISSING_SCOPES", "scopes": scopes,
                "required_scopes": sorted(REQUIRED_SCOPES), "missing_scopes": missing,
                "missing_optional_scopes": missing_optional,
                "publications": pubs, "online_store_publications": online}

    def dry_run(self, plan: dict, *, store_id: str | None = None, publish_online_store: bool = False) -> dict:
        store_id = store_id or plan["store_id"]
        config, client, _ = self._client(store_id)
        remote_data = client.execute(COLLECTIONS_QUERY).get("collections", {}).get("nodes", [])
        by_handle = {str(row.get("handle", "")).casefold(): row for row in remote_data}
        with connect(self.db) as con:
            mappings = {row["handle"].casefold(): dict(row) for row in con.execute("SELECT * FROM shopify_collection_mappings WHERE store_id=?", (store_id,))}
        rows = []
        for definition in plan.get("collections", []):
            handle = definition.get("handle", "")
            remote = by_handle.get(handle.casefold())
            mapping = mappings.get(handle.casefold())
            item = {"collection_key": definition.get("collection_key"), "handle": handle, "title": definition.get("title"),
                    "estimated_product_count": definition.get("estimated_product_count", 0), "conditions": definition.get("conditions", []),
                    "remote_id": (remote or {}).get("id"), "mapped_image_url": (mapping or {}).get("image_url"),
                    "image_status": "READY" if self._image_asset(store_id, definition.get("collection_key")) else "MISSING"}
            if remote and mapping and remote.get("id") != mapping["shopify_collection_id"]:
                item["action"] = "CONFLICT"; item["reason"] = "Handle now resolves to a different Shopify collection ID"
            elif remote and mapping and _normalized_hash(remote) != mapping["last_synced_hash"]:
                item["action"] = "CONFLICT"; item["reason"] = "Shopify content drifted since last ShopSource sync"
            elif remote and mapping and (remote.get("image") or {}).get("url") != mapping.get("image_url"):
                item["action"] = "CONFLICT"; item["reason"] = "Shopify image drifted since last ShopSource sync"
            elif not remote and mapping:
                item["action"] = "CONFLICT"; item["reason"] = "Previously mapped collection is missing remotely; no duplicate will be created"
            elif not remote:
                item["action"] = "CREATE"
            elif _normalized_hash(remote) == _condition_hash(definition):
                item["action"] = "NO CHANGE"
            elif not mapping:
                item["action"] = "CONFLICT"; item["reason"] = "An unmanaged Shopify collection uses this handle; review before adopting it"
            else:
                item["action"] = "UPDATE"
            if item["action"] in {"CREATE", "UPDATE"} and not item["conditions"]:
                item["action"] = "SKIP"; item["reason"] = "No supported inclusion conditions"
            rows.append(item)
        return {"store_id": store_id, "shop_domain": config["shop_domain"], "api_version": config["api_version"],
                "plan_id": plan["plan_id"], "publish_online_store": bool(publish_online_store), "items": rows,
                "counts": {action: sum(row["action"] == action for row in rows) for action in ("CREATE", "UPDATE", "NO CHANGE", "CONFLICT", "SKIP")}}

    def sync(self, plan: dict, *, confirmed: bool = False, publish_online_store: bool = False,
             retry_failed_only: bool = False, expected_preview: dict | None = None) -> dict:
        if not confirmed:
            raise RuntimeError("Explicit UI confirmation is required before Shopify writes")
        store_id = plan["store_id"]
        preview = self.dry_run(plan, store_id=store_id, publish_online_store=publish_online_store)
        if expected_preview is not None:
            signature = lambda value: (value.get("store_id"), value.get("plan_id"), value.get("shop_domain"),
                                       value.get("publish_online_store"),
                                       [(row.get("collection_key"), row.get("handle"), row.get("action"), row.get("remote_id"), row.get("image_status"))
                                        for row in value.get("items", [])])
            if signature(preview) != signature(expected_preview):
                raise RuntimeError("Shopify state changed since preview; review a new dry-run before writing")
        config, client, _ = self._client(store_id)
        verification = self.verify(store_id)
        required_now = set(REQUIRED_SCOPES) | ({PUBLISH_SCOPE} if publish_online_store else set())
        missing_now = sorted(required_now.difference(verification["scopes"]))
        if missing_now:
            raise RuntimeError("Missing Shopify scopes: " + ", ".join(missing_now))
        allowed = {"CREATE", "UPDATE"}
        items = []
        previous_failed = set()
        if retry_failed_only:
            previous_failed = self._last_failed_keys(store_id)
        for definition, decision in zip(plan.get("collections", []), preview["items"]):
            key = decision["collection_key"]
            if decision["action"] not in allowed:
                items.append({**decision, "result": decision["action"]}); continue
            if retry_failed_only and key not in previous_failed:
                items.append({**decision, "result": "SKIP_RETRY_NOT_FAILED"}); continue
            start = time.monotonic()
            result = {**decision, "result": "FAILED", "user_errors": [], "published_ids": [], "image_url": decision.get("mapped_image_url")}
            try:
                source = condition_source(definition["conditions"], definition.get("match_mode", "ANY"), prefer_tags=definition.get("rule_strategy") in {"TAG_PREFERRED", "MIXED"})
                source_input = source
                collection_input = {"title": definition["title"], "handle": definition["handle"],
                                    "descriptionHtml": definition.get("description_html", ""),
                                    "sources": [{"source": {"title": definition["title"], "inclusion": source}}]}
                asset = self._image_asset(store_id, key)
                if asset:
                    if FILE_SCOPES.isdisjoint(verification["scopes"]):
                        result["image_failed"] = True
                        result["image_error"] = "Missing Shopify write_files scope"
                    else:
                        try:
                            uploaded = ShopifyFileUploader(client).upload(asset["path"], definition.get("image_alt_text", ""))
                            collection_input["image"] = {"src": uploaded["url"], "altText": definition.get("image_alt_text", "")}
                            result["image_url"] = uploaded["url"]
                        except Exception as exc:
                            result["image_failed"] = True
                            result["image_error"] = str(exc)[:300]
                if decision["action"] == "CREATE":
                    data = client.execute(CREATE_MUTATION, {"collection": collection_input}).get("collectionCreate", {})
                else:
                    collection_input.pop("sources", None)
                    remote_current = next((row for row in client.execute(COLLECTIONS_QUERY).get("collections", {}).get("nodes", [])
                                           if row.get("id") == decision["remote_id"]), None)
                    if not remote_current:
                        raise RuntimeError("Mapped Shopify collection disappeared before update")
                    managed_source = next((row for row in remote_current.get("sources", []) if row.get("__typename") == "CollectionConditionsSource"), None)
                    if not managed_source:
                        raise RuntimeError("Mapped collection has no managed conditions source; refusing unsafe update")
                    condition_ids = [row.get("id") for row in ((managed_source.get("inclusion") or {}).get("conditions") or []) if row.get("id")]
                    update_condition = {"id": managed_source["id"], "title": definition["title"],
                                        "inclusion": {"matchType": source_input["matchType"],
                                                      "conditionsToDelete": condition_ids,
                                                      "conditionsToCreate": source_input["conditions"]}}
                    collection_input["id"] = decision["remote_id"]
                    collection_input["sourcesToUpdate"] = [{"condition": update_condition}]
                    data = client.execute(UPDATE_MUTATION, {"collection": collection_input}).get("collectionUpdate", {})
                errs = data.get("userErrors") or []
                if errs:
                    result["user_errors"] = [{"field": row.get("field"), "message": str(row.get("message", ""))[:300]} for row in errs]
                    raise RuntimeError("; ".join(str(error.get("message", "Shopify user error")) for error in errs))
                remote = data.get("collection") or {}
                if not remote.get("id"):
                    raise RuntimeError("Shopify response omitted collection ID")
                result["remote_id"] = remote["id"]
                if (remote.get("image") or {}).get("url"):
                    result["image_url"] = remote["image"]["url"]
                product_count = (remote.get("productsCount") or {}).get("count")
                if product_count is None:
                    refreshed = client.execute("query CollectionCount($id: ID!) { node(id:$id) { ... on Collection { productsCount { count precision } } } }", {"id":remote["id"]}).get("node") or {}
                    product_count = (refreshed.get("productsCount") or {}).get("count")
                if product_count is not None:
                    result["actual_product_count"] = product_count
                    result["product_count_precision"] = (remote.get("productsCount") or {}).get("precision")
                if asset and not ((remote.get("image") or {}).get("url") or result.get("image_url")):
                    result["image_failed"] = True
                if publish_online_store:
                    verified = self.verify(store_id)
                    pubs = verified["online_store_publications"]
                    if not pubs:
                        result["publish_failed"] = True
                        result["publish_error"] = "Online Store publication unavailable"
                    else:
                        for pub in pubs[:1]:
                            pub_result = client.execute(PUBLISH_MUTATION, {"id": remote["id"], "input": [{"publicationId": pub["id"]}]}).get("publishablePublish", {})
                            if pub_result.get("userErrors"):
                                result["publish_failed"] = True
                                result["publish_error"] = "; ".join(e.get("message", "") for e in pub_result["userErrors"])
                            else:
                                result["published_ids"].append(pub["id"])
                digest = _condition_hash(definition)
                with connect(self.db) as con:
                    con.execute("""INSERT INTO shopify_collection_mappings(store_id,collection_key,handle,shopify_collection_id,last_synced_hash,last_synced_at,published_ids_json,image_url)
                      VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(store_id,collection_key) DO UPDATE SET handle=excluded.handle,
                      shopify_collection_id=excluded.shopify_collection_id,last_synced_hash=excluded.last_synced_hash,
                      last_synced_at=excluded.last_synced_at,published_ids_json=excluded.published_ids_json,image_url=excluded.image_url""",
                      (store_id,key,definition["handle"],remote["id"],digest,_now(),json.dumps(result["published_ids"]),result.get("image_url")))
                result["result"] = "CREATED" if decision["action"] == "CREATE" else "UPDATED"
            except Exception as exc:
                # Never emit raw request/credential-bearing exception context.
                result["result"] = "FAILED"; result["error"] = str(exc)[:500]
            result["duration_ms"] = round((time.monotonic() - start) * 1000)
            items.append(result)
        summary = {"created": sum(i["result"] == "CREATED" for i in items), "updated": sum(i["result"] == "UPDATED" for i in items),
                   "unchanged": sum(i["result"] == "NO CHANGE" for i in items), "failed": sum(i["result"] == "FAILED" for i in items),
                   "image_failed": sum(bool(i.get("image_failed")) for i in items), "publish_failed": sum(bool(i.get("publish_failed")) for i in items)}
        run_id = "CSR_" + uuid.uuid4().hex[:16]
        report = {"run_id": run_id, "store_id": store_id, "plan_id": plan["plan_id"], "shop_domain": config["shop_domain"],
                  "api_version": config["api_version"], "items": items, "summary": summary, "created_at": _now()}
        self._save_report(report)
        with connect(self.db) as con:
            con.execute("INSERT INTO collection_sync_runs(run_id,store_id,plan_id,result_json,created_at) VALUES(?,?,?,?,?)",
                        (run_id,store_id,plan["plan_id"],json.dumps(report,ensure_ascii=False),report["created_at"]))
        return report

    def _last_failed_keys(self, store_id: str) -> set[str]:
        with connect(self.db) as con:
            row = con.execute("SELECT result_json FROM collection_sync_runs WHERE store_id=? ORDER BY created_at DESC LIMIT 1", (store_id,)).fetchone()
        if not row: return set()
        return {item.get("collection_key") for item in json.loads(row[0]).get("items", []) if item.get("result") == "FAILED"}

    def _image_asset(self, store_id: str, key: str | None) -> dict | None:
        if not key: return None
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM collection_image_assets WHERE store_id=? AND collection_key=?", (store_id,key)).fetchone()
        return dict(row) if row and Path(row["path"]).is_file() else None

    def _save_report(self, report: dict) -> None:
        root = EXPORT_DIR / "collection_sync_reports" / re.sub(r"[^a-zA-Z0-9_-]", "_", report["store_id"]) / report["run_id"]
        root.mkdir(parents=True, exist_ok=True)
        (root / "sync_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        lines = [f"# Shopify collection sync {report['run_id']}", "", f"Store: {report['store_id']}", f"Plan: {report['plan_id']}", "", "## Summary"]
        lines.extend(f"- {name}: {count}" for name,count in report["summary"].items())
        for item in report["items"]:
            lines += ["", f"## {item.get('title')} — {item.get('result')}", f"- Shopify ID: {item.get('remote_id') or 'n/a'}"]
            lines.extend(f"- {c.get('field')} {c.get('relation')} {c.get('value')}" for c in item.get("conditions", []))
            if item.get("error"): lines.append(f"- Error: {item['error']}")
        (root / "sync_report.md").write_text("\n".join(lines),encoding="utf-8")


class ShopifyFileUploader:
    """Shopify stagedUploadsCreate → multipart POST → fileCreate → ready URL."""
    STAGED = """mutation StagedUploadsCreate($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) { stagedTargets { url resourceUrl parameters { name value } } userErrors { field message } }
    }"""
    FILE_CREATE = """mutation FileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) { files { ... on MediaImage { id status image { url altText } } ... on GenericFile { id url } } userErrors { field message } }
    }"""
    FILE_QUERY = "query FileNode($id: ID!) { node(id:$id) { ... on MediaImage { id status image { url altText } } } }"
    def __init__(self, client): self.client = client
    def upload(self, path: str | Path, alt_text: str) -> dict:
        source = Path(path)
        mime = {".png":"image/png", ".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".webp":"image/webp"}.get(source.suffix.lower())
        if not mime: raise ValueError("Shopify collection image must be PNG, JPEG, or WebP")
        staged = self.client.execute(self.STAGED,{"input":[{"filename":source.name,"mimeType":mime,"httpMethod":"POST","resource":"IMAGE","fileSize":str(source.stat().st_size)}]}).get("stagedUploadsCreate",{})
        if staged.get("userErrors"): raise RuntimeError("Shopify staged upload rejected")
        target = (staged.get("stagedTargets") or [None])[0]
        if not target: raise RuntimeError("Shopify did not return staged upload target")
        fields = target.get("parameters",[])
        boundary = "----ShopSource" + hashlib.sha256(os.urandom(16)).hexdigest()[:20]
        chunks=[]
        for field in fields:
            chunks += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field['name']}\"\r\n\r\n{field['value']}\r\n".encode()]
        chunks += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{source.name}\"\r\nContent-Type: {mime}\r\n\r\n".encode(),source.read_bytes(),f"\r\n--{boundary}--\r\n".encode()]
        req=urllib.request.Request(target["url"],data=b"".join(chunks),headers={"Content-Type":f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req,timeout=60) as response:
                if response.status >= 300: raise RuntimeError("Shopify staged upload failed")
        except Exception as exc: raise RuntimeError(f"Shopify staged upload failed: {type(exc).__name__}") from None
        created=self.client.execute(self.FILE_CREATE,{"files":[{"originalSource":target["resourceUrl"],"contentType":"IMAGE","alt":alt_text}]}).get("fileCreate",{})
        if created.get("userErrors"): raise RuntimeError("Shopify fileCreate rejected")
        node=(created.get("files") or [None])[0] or {}
        url=((node.get("image") or {}).get("url")) or node.get("url")
        if not url and node.get("id"):
            node=self.client.execute(self.FILE_QUERY,{"id":node["id"]}).get("node") or {}
            url=((node.get("image") or {}).get("url")) or node.get("url")
        if not url or not str(url).startswith("https://"):
            raise RuntimeError("Shopify uploaded image URL was not ready/valid")
        return {"id":node.get("id"),"url":url,"alt_text":alt_text}
