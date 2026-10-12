"""Owned, exact-four collection prerequisite for collection-backed Featured sections.

Network access is injected or obtained lazily. Importing this module never calls
Shopify; writes require a fresh dry-run and explicit confirmation.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

from .db import connect
from .shopify_collections import SCOPES_QUERY, ShopifyGraphQLClient, get_connection, get_shopify_token

COLLECTION_KEY = "homepage-featured-products"
COLLECTION_HANDLE = "shopsource-homepage-featured"
COLLECTION_TITLE = "New Arrivals"
SOURCE_TITLE = "ShopSource Featured Products"
READ_SCOPES = {"read_products", "read_publications"}
WRITE_SCOPES = {"write_products", "write_publications"}
ONLINE_STORE_PUBLICATIONS_QUERY = "query FeaturedOnlineStorePublications { publications(first:100) { nodes { id name } } }"

COLLECTION_READ_QUERY = """query FeaturedCollection($handle:String!) {
 collectionByHandle(handle:$handle) {
  id handle title productsCount { count precision }
  sources { __typename id title ... on CollectionConditionsSource {
   inclusion { conditions { __typename } selections(first:100) { nodes { product { id } } pageInfo { hasNextPage } }
   } } }
  resourcePublicationsV2(first:100) { nodes { isPublished publication { id name } } }
 }
}"""
COLLECTION_CREATE_MUTATION = """mutation FeaturedCollectionCreate($collection:CollectionCreateInput!) {
 collectionCreate(collection:$collection) { collection { id handle title } userErrors { field message } }
}"""
COLLECTION_UPDATE_MUTATION = """mutation FeaturedCollectionUpdate($collection:CollectionUpdateInput!) {
 collectionUpdate(collection:$collection) { collection { id handle title } userErrors { field message } }
}"""
COLLECTION_PUBLISH_MUTATION = """mutation FeaturedCollectionPublish($id:ID!,$input:[PublicationInput!]!) {
 publishablePublish(id:$id,input:$input) { userErrors { field message } }
}"""
COLLECTION_JOB_QUERY = "query FeaturedCollectionJob($id:ID!) { job(id:$id) { id done } }"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id_hash(ids: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(ids), separators=(",", ":")).encode()).hexdigest()


class FeaturedCollectionPrerequisiteService:
    """Read-only dry-run/proof and explicitly confirmed collection preparation."""

    def __init__(self, *, db=None, client=None, client_factory=ShopifyGraphQLClient):
        self.db, self.client_override, self.client_factory = db, client, client_factory
        with connect(db) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS featured_collection_bindings (
              store_id TEXT NOT NULL, collection_key TEXT NOT NULL, collection_id TEXT NOT NULL,
              handle TEXT NOT NULL, title TEXT NOT NULL, expected_product_ids_hash TEXT NOT NULL,
              last_verified_product_ids_json TEXT NOT NULL, publication_id TEXT,
              publication_status TEXT NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(store_id,collection_key), UNIQUE(store_id,handle))""")

    def _client(self, store_id):
        config = get_connection(store_id, db=self.db)
        if self.client_override is not None:
            return config, self.client_override
        token = get_shopify_token(store_id, db=self.db)[0]
        if not config or not token:
            raise RuntimeError("FEATURED_COLLECTION_CREDENTIAL_MISSING")
        return config, self.client_factory(config["shop_domain"], token, config["api_version"])

    @staticmethod
    def _four_ids(plan):
        ids = [str(item.get("shopify_product_id") or "") for item in plan.get("items", [])]
        if len(ids) != 4 or len(set(ids)) != 4 or any(not x.startswith("gid://shopify/Product/") for x in ids):
            raise ValueError("FEATURED_FOUR_UNIQUE_PRODUCTS_REQUIRED")
        return ids

    def _remote_state(self, store_id, client):
        scopes = {str(row.get("handle")) for row in
                  (client.execute(SCOPES_QUERY).get("currentAppInstallation") or {}).get("accessScopes", [])}
        pubs = client.execute(ONLINE_STORE_PUBLICATIONS_QUERY).get("publications", {}).get("nodes", [])
        online = [row for row in pubs if str(row.get("name", "")).casefold() == "online store"]
        online = online if len(online) == 1 else []
        remote = client.execute(COLLECTION_READ_QUERY, {"handle": COLLECTION_HANDLE}).get("collectionByHandle") or None
        with connect(self.db) as con:
            mapping = con.execute("SELECT * FROM featured_collection_bindings WHERE store_id=? AND collection_key=?",
                                  (str(store_id), COLLECTION_KEY)).fetchone()
        return scopes, online, remote, dict(mapping) if mapping else None

    @staticmethod
    def _membership(remote):
        if not remote:
            return []
        sources = remote.get("sources") or []
        if len(sources) != 1 or sources[0].get("__typename") != "CollectionConditionsSource":
            return None
        source = sources[0]
        inclusion = source.get("inclusion") or {}
        if source.get("title") != SOURCE_TITLE or inclusion.get("conditions"):
            return None
        nodes = (inclusion.get("selections") or {}).get("nodes") or []
        if (inclusion.get("selections") or {}).get("pageInfo", {}).get("hasNextPage"):
            return None
        return [str((node.get("product") or {}).get("id") or "") for node in nodes]

    @staticmethod
    def _published(remote, online_publication):
        if not remote or not online_publication:
            return False
        pub_id = online_publication.get("id")
        return any((row.get("publication") or {}).get("id") == pub_id and row.get("isPublished") is True
                   for row in (remote.get("resourcePublicationsV2") or {}).get("nodes", []))

    def dry_run(self, plan: dict, *, store_id=None) -> dict:
        store_id = str(store_id or plan.get("store_id") or "")
        try:
            expected = self._four_ids(plan)
            _config, client = self._client(store_id)
            scopes, online, remote, mapping = self._remote_state(store_id, client)
        except Exception as exc:
            return {"status": "BLOCKED", "action": "MISSING_SCOPES" if "SCOPE" in str(exc) else "READ_FAILED",
                    "reason_code": str(exc) if str(exc).startswith("FEATURED_") else type(exc).__name__,
                    "write_performed": False}
        missing_read = sorted(READ_SCOPES - scopes)
        if missing_read:
            return {"status": "BLOCKED", "action": "MISSING_SCOPES", "missing_scopes": missing_read,
                    "write_performed": False}
        if not online:
            return {"status": "BLOCKED", "action": "MISSING_SCOPES",
                    "reason_code": "ONLINE_STORE_PUBLICATION_UNAVAILABLE", "write_performed": False}
        if remote and not mapping:
            action, reason = "CONFLICT", "FEATURED_COLLECTION_HANDLE_CONFLICT"
        elif mapping and not remote:
            action, reason = "CONFLICT", "FEATURED_COLLECTION_OWNED_REMOTE_MISSING"
        elif mapping and (mapping["collection_id"] != remote.get("id") or mapping["handle"] != remote.get("handle")):
            action, reason = "CONFLICT", "FEATURED_COLLECTION_OWNERSHIP_IDENTITY_MISMATCH"
        elif not remote and not mapping:
            action, reason = "CREATE", None
        else:
            membership = self._membership(remote)
            if membership is None:
                action, reason = "CONFLICT", "FEATURED_COLLECTION_SOURCE_MISMATCH"
            else:
                matches = len(membership) == 4 and len(set(membership)) == 4 and set(membership) == set(expected)
                published = self._published(remote, online[0])
                if matches and published:
                    action, reason = "NO_CHANGE", None
                elif matches:
                    action, reason = "PUBLISH_ONLY", None
                elif published:
                    action, reason = "UPDATE_MEMBERSHIP", None
                else:
                    action, reason = "UPDATE_AND_PUBLISH", None
        membership = self._membership(remote) if remote else []
        return {"status": "CONFLICT" if action == "CONFLICT" else "DRY_RUN_READY",
                "action": action, "reason_code": reason, "store_id": store_id,
                "collection_key": COLLECTION_KEY, "collection_id": (remote or {}).get("id"),
                "handle": COLLECTION_HANDLE, "title": COLLECTION_TITLE,
                "selected_product_ids": expected, "selected_product_ids_hash": _id_hash(expected),
                "remote_product_ids": membership,
                "remote_product_ids_hash": _id_hash(membership or []) if remote else None,
                "publication_id": online[0]["id"], "published": self._published(remote, online[0]),
                "scopes": sorted(scopes), "missing_write_scopes": sorted(WRITE_SCOPES - scopes),
                "write_performed": False}

    def prepare(self, plan: dict, *, confirmed=False, expected_preview=None, store_id=None) -> dict:
        if confirmed is not True:
            return {"status": "CONFIRMATION_REQUIRED", "write_performed": False}
        store_id = str(store_id or plan.get("store_id") or "")
        preview = self.dry_run(plan, store_id=store_id)
        compare_keys = ("action", "collection_id", "remote_product_ids_hash", "selected_product_ids_hash", "publication_id")
        if expected_preview is not None and any(preview.get(key) != expected_preview.get(key) for key in compare_keys):
            return {"status": "CONFLICT", "action": "CONFLICT",
                    "reason_code": "FEATURED_COLLECTION_CHANGED_ABORT", "write_performed": False}
        if preview.get("status") != "DRY_RUN_READY" or preview.get("action") == "CONFLICT":
            return {**preview, "write_performed": False}
        action = preview.get("action")
        required_write = set()
        if action in {"CREATE", "UPDATE_MEMBERSHIP", "UPDATE_AND_PUBLISH"}:
            required_write.add("write_products")
        if action in {"CREATE", "PUBLISH_ONLY"} or (action in {"UPDATE_MEMBERSHIP", "UPDATE_AND_PUBLISH"}
                                                       and preview.get("published") is not True):
            required_write.add("write_publications")
        missing = sorted(required_write - set(preview.get("scopes") or []))
        if missing:
            return {**preview, "status": "BLOCKED", "action": "MISSING_SCOPES", "missing_scopes": missing,
                    "write_performed": False}
        if preview["action"] == "NO_CHANGE":
            return self.verify_for_plan(plan, store_id=store_id)
        try:
            _config, client = self._client(store_id)
            fresh = self.dry_run(plan, store_id=store_id)
            if any(fresh.get(key) != preview.get(key) for key in compare_keys):
                return {"status": "CONFLICT", "action": "CONFLICT",
                        "reason_code": "FEATURED_COLLECTION_CHANGED_ABORT", "write_performed": False}
            ids = preview["selected_product_ids"]
            action = preview["action"]
            if action == "CREATE":
                source = {"source": {"title": SOURCE_TITLE, "targetType": "PRODUCTS",
                    "inclusion": {"selections": [{"productId": product_id} for product_id in ids]}}}
                response = client.execute(COLLECTION_CREATE_MUTATION, {"collection": {
                    "title": COLLECTION_TITLE, "handle": COLLECTION_HANDLE, "sortOrder": "MANUAL",
                    "sources": [source]}}).get("collectionCreate") or {}
            elif action in {"UPDATE_MEMBERSHIP", "UPDATE_AND_PUBLISH"}:
                _scopes, _online, remote, mapping = self._remote_state(store_id, client)
                if not remote or not mapping or remote.get("id") != mapping["collection_id"]:
                    return {"status": "CONFLICT", "reason_code": "FEATURED_COLLECTION_OWNERSHIP_IDENTITY_MISMATCH",
                            "write_performed": False}
                current = self._membership(remote)
                if current is None:
                    return {"status": "CONFLICT", "reason_code": "FEATURED_COLLECTION_SOURCE_MISMATCH",
                            "write_performed": False}
                condition = {"id": remote["sources"][0]["id"], "inclusion": {
                    "selectionsToAdd": [{"productId": x} for x in sorted(set(ids) - set(current))],
                    "selectionsToRemove": [{"productId": x} for x in sorted(set(current) - set(ids))]}}
                response = client.execute(COLLECTION_UPDATE_MUTATION, {"collection": {
                    "id": remote["id"], "sourcesToUpdate": [{"condition": condition}]}}).get("collectionUpdate") or {}
            else:
                response = {"collection": {"id": preview["collection_id"]}, "userErrors": []}
            if response.get("userErrors"):
                return {"status": "FAILED", "reason_code": "FEATURED_COLLECTION_MUTATION_REJECTED",
                        "write_performed": True}
            job = response.get("job")
            if isinstance(job, dict) and job.get("id") and job.get("done") is not True:
                for _attempt in range(10):
                    time.sleep(1)
                    job_result = client.execute(COLLECTION_JOB_QUERY, {"id": job["id"]}).get("job") or {}
                    if job_result.get("done") is True:
                        break
                else:
                    return {"status": "FAILED", "reason_code": "FEATURED_COLLECTION_UPDATE_TIMEOUT",
                            "write_performed": True}
            collection_id = (response.get("collection") or {}).get("id") or preview.get("collection_id")
            if not collection_id:
                return {"status": "FAILED", "reason_code": "FEATURED_COLLECTION_MUTATION_UNVERIFIED",
                        "write_performed": True}
            if action == "CREATE":
                # Ownership is established only from the ID returned by our own
                # create mutation, never by adopting a matching remote handle.
                with connect(self.db) as con:
                    con.execute("""INSERT INTO featured_collection_bindings
                      (store_id,collection_key,collection_id,handle,title,expected_product_ids_hash,
                       last_verified_product_ids_json,publication_id,publication_status,updated_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(store_id,collection_key) DO NOTHING""",
                      (store_id, COLLECTION_KEY, collection_id, COLLECTION_HANDLE, COLLECTION_TITLE,
                       _id_hash(ids), json.dumps(ids), preview["publication_id"], "PENDING", _now()))
            if action != "PUBLISH_ONLY" and not preview["published"]:
                published = client.execute(COLLECTION_PUBLISH_MUTATION, {"id": collection_id,
                    "input": [{"publicationId": preview["publication_id"]}]}).get("publishablePublish") or {}
                if published.get("userErrors"):
                    return {"status": "FEATURED_COLLECTION_PUBLICATION_FAILED",
                            "reason_code": "FEATURED_COLLECTION_PUBLICATION_FAILED", "write_performed": True}
            verified = self.verify_for_plan(plan, store_id=store_id)
            if verified.get("status") != "VERIFIED":
                return {**verified, "status": "FEATURED_COLLECTION_MEMBERSHIP_VERIFY_FAILED",
                        "reason_code": "FEATURED_COLLECTION_MEMBERSHIP_VERIFY_FAILED", "write_performed": True}
            with connect(self.db) as con:
                con.execute("""INSERT INTO featured_collection_bindings(store_id,collection_key,collection_id,handle,title,
                  expected_product_ids_hash,last_verified_product_ids_json,publication_id,publication_status,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(store_id,collection_key) DO UPDATE SET
                  collection_id=excluded.collection_id,handle=excluded.handle,title=excluded.title,
                  expected_product_ids_hash=excluded.expected_product_ids_hash,
                  last_verified_product_ids_json=excluded.last_verified_product_ids_json,
                  publication_id=excluded.publication_id,publication_status=excluded.publication_status,updated_at=excluded.updated_at""",
                  (store_id, COLLECTION_KEY, verified["collection_id"], COLLECTION_HANDLE, COLLECTION_TITLE,
                   _id_hash(ids), json.dumps(ids), verified["publication_id"], "PUBLISHED", _now()))
                if plan.get("plan_id"):
                    con.execute("UPDATE homepage_featured_product_plans SET collection_handle=?,updated_at=? WHERE plan_id=? AND store_id=?",
                                (COLLECTION_HANDLE, _now(), str(plan["plan_id"]), store_id))
            return {**verified, "status": "VERIFIED", "action": action, "write_performed": True}
        except Exception as exc:
            return {"status": "FAILED", "reason_code": "FEATURED_COLLECTION_OPERATION_FAILED",
                    "error_type": type(exc).__name__, "write_performed": True}

    def verify_for_plan(self, plan: dict, *, store_id=None) -> dict:
        store_id = str(store_id or plan.get("store_id") or "")
        try:
            expected = self._four_ids(plan)
            _config, client = self._client(store_id)
            scopes, online, remote, mapping = self._remote_state(store_id, client)
        except Exception as exc:
            return {"status": "UNVERIFIED", "reason_code": type(exc).__name__, "write_performed": False}
        if READ_SCOPES - scopes:
            return {"status": "UNVERIFIED", "reason_code": "MISSING_READ_SCOPES", "write_performed": False}
        if not remote or not mapping or remote.get("id") != mapping["collection_id"] or remote.get("handle") != mapping["handle"]:
            return {"status": "UNVERIFIED", "reason_code": "FEATURED_COLLECTION_OWNERSHIP_IDENTITY_MISMATCH",
                    "write_performed": False}
        ids = self._membership(remote)
        count = (remote.get("productsCount") or {}).get("count")
        published = self._published(remote, online[0] if online else None)
        exact = ids is not None and len(ids) == 4 and len(set(ids)) == 4 and set(ids) == set(expected) and count == 4
        if not exact:
            return {"status": "UNVERIFIED", "reason_code": "FEATURED_COLLECTION_MEMBERSHIP_UNVERIFIED",
                    "collection_id": remote["id"], "handle": remote["handle"], "write_performed": False}
        if not published:
            return {"status": "UNVERIFIED", "reason_code": "FEATURED_COLLECTION_PUBLICATION_UNVERIFIED",
                    "collection_id": remote["id"], "handle": remote["handle"], "write_performed": False}
        return {"status": "VERIFIED", "verified": True, "collection_id": remote["id"],
                "handle": remote["handle"], "title": remote.get("title"), "product_ids": expected,
                "membership_set_match": True, "membership_hash": _id_hash(ids), "published": True,
                "publication_id": online[0]["id"], "publication_status": "PUBLISHED",
                "verified_at": _now(), "write_performed": False}
