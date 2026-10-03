"""Resumable, ownership-bounded direct Shopify product synchronization.

The productSet input intentionally contains only scalar fields that ShopSource
owns. Shopify documents list fields as authoritative/replacing, so tags,
variants, media, metafields and collections are never included here.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlsplit
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .db import connect, init_db
from .shopify_collections import SHOPIFY_API_VERSION, ShopifyGraphQLClient, get_connection, get_shopify_token

ELIGIBLE_STATUSES = {"PRIMARY", "RESERVE_A", "RESERVE_B", "RESERVE_C", "LOW_RESERVE", "HIGH_RESERVE", "REVIEW"}
IDENTITY_NAMESPACE = "shopsource"

PRODUCT_READ_QUERY = """query ShopSourceProduct($identifier: ProductIdentifierInput!) {
 productByIdentifier(identifier: $identifier) { id handle title descriptionHtml vendor productType status tags
  variants(first: 2) { nodes { id price compareAtPrice } }
  variantsCount { count }
 }
}"""
PRODUCT_SET_MUTATION = """mutation ShopSourceProductSet($identifier: ProductSetIdentifiers!, $input: ProductSetInput!) {
 productSet(identifier: $identifier, input: $input, synchronous: true) {
  product { id handle title descriptionHtml vendor productType status tags variants(first: 2) { nodes { id price compareAtPrice } } variantsCount { count } }
  userErrors { field message }
 }
}"""
PRODUCT_CREATE_MEDIA_MUTATION = """mutation ShopSourceProductMedia($productId: ID!, $media: [CreateMediaInput!]!) {
 productCreateMedia(productId: $productId, media: $media) {
  media { id alt status mediaContentType ... on MediaImage { image { url } } }
  mediaUserErrors { field message }
 }
}"""
PRODUCT_MEDIA_QUERY = """query ShopSourceProductMediaRead($id: ID!) {
 product(id: $id) { id media(first: 100) { nodes { id alt status mediaContentType ... on MediaImage { image { url } } } } }
}"""
VARIANT_PRICE_MUTATION = """mutation ShopSourcePrice($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
 productVariantsBulkUpdate(productId: $productId, variants: $variants) {
  productVariants { id price compareAtPrice }
  userErrors { field message }
 }
}"""
TAGS_ADD_MUTATION = """mutation ShopSourceTags($id: ID!, $tags: [String!]!) {
 tagsAdd(id: $id, tags: $tags) { node { id } userErrors { field message } }
}"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(value).casefold()).strip("-")


def source_identity(store_id: str, source_platform: str, source_id: str) -> str:
    # Length-bounded and title-independent; store_id is also part of the DB key.
    return f"{str(source_platform).strip().lower()}:{str(source_id).strip().upper()}"


def identity_handle(source_platform: str, source_id: str) -> str:
    value = source_identity("", source_platform, source_id)
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"ss-{_slug(source_platform)[:18]}-{_slug(source_id)[:28]}-{digest}"[:64].strip("-")


def collection_tag(handle_or_key: str) -> str:
    return "shopsource:collection:" + _slug(handle_or_key)


def _install_schema(db=None):
    init_db(db)
    with connect(db) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS shopify_product_mappings (
          store_id TEXT NOT NULL, master_product_id INTEGER NOT NULL,
          source_platform TEXT NOT NULL, source_id TEXT NOT NULL,
          shopify_product_id TEXT NOT NULL, shopify_handle TEXT NOT NULL,
          last_payload_hash TEXT NOT NULL DEFAULT '', last_remote_hash TEXT NOT NULL DEFAULT '',
          sync_status TEXT NOT NULL, synced_at TEXT NOT NULL,
          PRIMARY KEY(store_id,master_product_id), UNIQUE(store_id,source_platform,source_id),
          UNIQUE(store_id,shopify_product_id)
        );
        CREATE TABLE IF NOT EXISTS shopify_product_sync_runs (
          run_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, status TEXT NOT NULL,
          checkpoint INTEGER NOT NULL DEFAULT 0, input_hash TEXT NOT NULL,
          counts_json TEXT NOT NULL DEFAULT '{}', retry_failed_only INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shopify_product_sync_items (
          run_id TEXT NOT NULL, master_product_id INTEGER NOT NULL, source_id TEXT NOT NULL,
          action TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL,
          error TEXT NOT NULL DEFAULT '', remote_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(run_id,master_product_id)
        );
        CREATE TABLE IF NOT EXISTS shopify_product_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, store_id TEXT NOT NULL,
          master_product_id INTEGER NOT NULL, remote_id TEXT, action TEXT NOT NULL,
          result TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shopify_product_settings (
          store_id TEXT PRIMARY KEY, media_mode TEXT NOT NULL DEFAULT 'MANUAL_MEDIA',
          source_media_rights_confirmed INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shopify_product_media_mappings (
          store_id TEXT NOT NULL,master_product_id INTEGER NOT NULL,source_image_hash TEXT NOT NULL,
          shopify_media_id TEXT NOT NULL,status TEXT NOT NULL,alt_text TEXT NOT NULL DEFAULT '',
          image_url_verified INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,
          PRIMARY KEY(store_id,master_product_id,source_image_hash)
        );
        """)
        columns = {row["name"] for row in con.execute("PRAGMA table_info(shopify_product_settings)")}
        if "source_media_rights_confirmed" not in columns:
            con.execute("ALTER TABLE shopify_product_settings ADD COLUMN source_media_rights_confirmed INTEGER NOT NULL DEFAULT 0")


class ProductPublisher:
    provider_name = "ABSTRACT"
    def preview(self, *args, **kwargs): raise NotImplementedError
    def sync(self, *args, **kwargs): raise NotImplementedError
    def retry_failed(self, *args, **kwargs): raise NotImplementedError
    def verify(self, *args, **kwargs): raise NotImplementedError


class DirectShopifyProductPublisher(ProductPublisher):
    provider_name = "DIRECT_SHOPIFY"

    def __init__(self, *, db=None, client_factory=ShopifyGraphQLClient, wait: Callable[[float], None] = time.sleep,
                 batch_size: int = 100, max_attempts: int = 4, media_handler: Callable | None = None):
        self.db, self.client_factory, self.wait = db, client_factory, wait
        self.media_handler = media_handler
        self.batch_size, self.max_attempts = max(1, min(500, int(batch_size))), max(1, min(6, int(max_attempts)))
        self._rules_cache = {}
        _install_schema(db)

    def set_media_mode(self, store_id: str, mode: str, *, source_media_rights_confirmed=False) -> None:
        if mode not in {"SOURCE_MEDIA", "GENERATED_MEDIA", "MANUAL_MEDIA", "MIXED"}:
            raise ValueError("Unsupported product media mode")
        with connect(self.db) as con:
            con.execute("INSERT INTO shopify_product_settings(store_id,media_mode,source_media_rights_confirmed,updated_at) VALUES(?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET media_mode=excluded.media_mode,source_media_rights_confirmed=excluded.source_media_rights_confirmed,updated_at=excluded.updated_at",
                        (store_id, mode, int(bool(source_media_rights_confirmed)), _now()))

    def media_mode(self, store_id: str) -> str:
        with connect(self.db) as con:
            row = con.execute("SELECT media_mode FROM shopify_product_settings WHERE store_id=?", (store_id,)).fetchone()
        return row["media_mode"] if row else "MANUAL_MEDIA"

    def source_media_rights_confirmed(self, store_id: str) -> bool:
        with connect(self.db) as con:
            row = con.execute("SELECT source_media_rights_confirmed FROM shopify_product_settings WHERE store_id=?", (store_id,)).fetchone()
        return bool(row["source_media_rights_confirmed"]) if row else False

    def _client(self, store_id):
        config = get_connection(store_id, db=self.db)
        if not config:
            raise RuntimeError("Shopify connection is not configured")
        token, _ = get_shopify_token(store_id)
        if not token:
            raise RuntimeError("Shopify credential missing")
        return config, self.client_factory(config["shop_domain"], token, config["api_version"])

    @staticmethod
    def build_payload(product: dict, *, store_id: str, collection_handles: Iterable[str] = (), publish_status="DRAFT") -> dict:
        status = str(product.get("final_status") or "").upper()
        if status not in ELIGIBLE_STATUSES or int(product.get("archived") or 0):
            raise ValueError("PRODUCT_NOT_ELIGIBLE")
        title = str(product.get("title") or "").strip()
        # Amazon acquisition price is not a Shopify selling price. Require an explicit store price.
        selling_price = product.get("selling_price")
        if not title or selling_price is None:
            raise ValueError("MISSING_TITLE_OR_CONFIGURED_SELLING_PRICE")
        try:
            amount = round(float(selling_price), 2)
        except (TypeError, ValueError):
            raise ValueError("INVALID_STORE_SELLING_PRICE") from None
        if amount <= 0:
            raise ValueError("INVALID_STORE_SELLING_PRICE")
        compare_at = product.get("compare_at_price")
        platform = str(product.get("source") or product.get("source_kind") or "unknown").casefold()
        source_id = str(product.get("source_id") or product.get("asin") or "").strip()
        if not source_id:
            raise ValueError("MISSING_SOURCE_IDENTITY")
        identity = source_identity(store_id, platform, source_id)
        handle = identity_handle(platform, source_id)
        owned_tags = sorted({value if str(value).startswith("shopsource-") else collection_tag(value)
                             for value in collection_handles})
        payload = {"handle": handle, "title": title, "status": publish_status if publish_status in {"DRAFT", "ACTIVE"} else "DRAFT",
                   "inventory_policy": "UNMANAGED", "identity": identity, "owned_tags": owned_tags,
                   "price": f"{amount:.2f}", "currency": str(product.get("selling_currency") or "USD")}
        try:
            if compare_at is not None and float(compare_at) > amount:
                payload["compare_at_price"] = f"{float(compare_at):.2f}"
        except (TypeError, ValueError):
            pass
        # Only emit text scalars if ShopSource actually has them. Never pass authoritative list fields.
        for source, destination in (("description_html", "descriptionHtml"), ("brand", "vendor"), ("product_type", "productType")):
            if product.get(source) not in (None, ""):
                payload[destination] = str(product[source])
        return payload

    @staticmethod
    def _graphql_input(payload):
        # Deliberately excludes list fields: variants, tags, media/files, metafields, collections.
        return {key: value for key, value in payload.items() if key in {"handle", "title", "descriptionHtml", "vendor", "productType", "status"}}

    @staticmethod
    def _remote_hash(remote: dict) -> str:
        keys = ("title", "descriptionHtml", "vendor", "productType", "status")
        variants = ((remote.get("variants") or {}).get("nodes") or [])
        managed_price = variants[0].get("price") if len(variants) == 1 else None
        managed_tags = sorted(tag for tag in (remote.get("tags") or [])
                              if str(tag).startswith("shopsource:") or str(tag).startswith("shopsource-"))
        return _stable_hash({**{key: remote.get(key) for key in keys}, "managed_price": managed_price,
                             "managed_tags": managed_tags})

    def _catalog_rows(self, store_id, *, db=None):
        with connect(db if db is not None else self.db) as con:
            cursor = con.execute("""SELECT p.id AS master_product_id,p.asin,p.source,p.source_kind,p.title,p.brand,
                p.category,p.price AS source_price,p.archived,d.final_status,p.raw_json,p.images_json
                FROM products p LEFT JOIN store_product_decisions d ON d.product_id=p.id AND d.store_id=?
                ORDER BY p.id""", (store_id,))
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows: break
                for item in rows:
                    row = dict(item)
                    try: row["images"] = json.loads(row.pop("images_json") or "[]")
                    except (json.JSONDecodeError, TypeError): row["images"] = []
                    try: raw = json.loads(row.pop("raw_json") or "{}")
                    except (json.JSONDecodeError, TypeError): raw = {}
                    # An explicit Shopify/store retail price only; source acquisition price is never re-used.
                    row["selling_price"] = raw.get("shopify_selling_price", raw.get("store_selling_price"))
                    row["selling_currency"] = raw.get("shopify_currency", "USD")
                    row["compare_at_price"] = raw.get("shopify_compare_at_price", raw.get("store_compare_at_price"))
                    row["product_type"] = row.get("category")
                    row["description_html"] = raw.get("descriptionHtml", raw.get("description_html"))
                    yield row

    def _current_input_hash(self, store_id, publish_status="DRAFT"):
        fingerprints = []
        for row in self._catalog_rows(store_id):
            final = str(row.get("final_status") or "").upper()
            if final not in ELIGIBLE_STATUSES or row.get("archived"):
                fingerprints.append((row["master_product_id"], final, None))
                continue
            try:
                tags = self._planned_tags(store_id, row)
                payload = self.build_payload(row, store_id=store_id, collection_handles=tags, publish_status=publish_status)
                if self.media_mode(store_id) != "MANUAL_MEDIA": payload["media"] = row.get("images") or []
                fingerprints.append((row["master_product_id"], _stable_hash(payload), final))
            except ValueError as exc:
                fingerprints.append((row["master_product_id"], "SKIP:" + str(exc), final))
        return _stable_hash({"fingerprints": fingerprints, "publish_status": publish_status, "media_mode": self.media_mode(store_id),
                             "source_media_rights_confirmed": self.source_media_rights_confirmed(store_id)})

    def _planned_tags(self, store_id, product):
        try:
            if store_id not in self._rules_cache:
                from .collection_planner import CollectionPlanner
                with connect(self.db) as con:
                    latest = con.execute("SELECT plan_id FROM store_collection_plans WHERE store_id=? ORDER BY version DESC LIMIT 1", (store_id,)).fetchone()
                plan = CollectionPlanner(self.db).get_plan(latest["plan_id"]) if latest else {"collections": []}
                self._rules_cache[store_id] = plan["collections"]
        except Exception:
            return []
        title = str(product.get("title", "")).casefold()
        tags = {str(tag).casefold() for tag in product.get("tags", [])}
        chosen = []
        for definition in self._rules_cache[store_id]:
            checks = []
            for condition in definition.get("conditions", []):
                field, value = condition.get("field"), str(condition.get("value", "")).casefold()
                if field == "TITLE" and condition.get("relation") == "CONTAINS": checks.append(value in title)
                elif field == "TAG" and condition.get("relation") == "EQUALS": checks.append(value in tags)
            if checks and any(checks):
                chosen.append(definition.get("handle") or definition.get("collection_key"))
                # Existing Phase 3.2 generated TAG rules use this legacy-compatible key.
                chosen.append(f"shopsource-{_slug(store_id)}-{_slug(definition.get('collection_key'))}")
        return sorted(set(chosen))

    def preview(self, store_id: str, *, publish_status="DRAFT", limit=100, db=None) -> dict:
        _install_schema(db if db is not None else self.db)
        actions, prepared, source_fingerprints = {key: 0 for key in ("CREATE", "UPDATE", "NO CHANGE", "CONFLICT", "SKIP")}, [], []
        skip_reasons = {}
        mapping_by_master = {}
        with connect(db if db is not None else self.db) as con:
            for mapping in con.execute("SELECT * FROM shopify_product_mappings WHERE store_id=?", (store_id,)):
                mapping_by_master[mapping["master_product_id"]] = dict(mapping)
        remote_client = None
        for row in self._catalog_rows(store_id, db=db):
            final = str(row.get("final_status") or "").upper()
            if final not in ELIGIBLE_STATUSES or row.get("archived"):
                actions["SKIP"] += 1
                reason = final or "NO_STORE_DECISION"
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                item = {"master_product_id": row["master_product_id"], "source_id": row.get("asin"), "action": "SKIP", "reason": reason}
                source_fingerprints.append((row["master_product_id"], final, row.get("last_seen_at")))
            else:
                try:
                    tags = self._planned_tags(store_id, row)
                    payload = self.build_payload(row, store_id=store_id, collection_handles=tags, publish_status=publish_status)
                    if self.media_mode(store_id) != "MANUAL_MEDIA":
                        payload["media"] = row.get("images") or []
                    phash = _stable_hash(payload)
                    mapping = mapping_by_master.get(row["master_product_id"])
                    action, remote, reason = "CREATE", None, ""
                    if mapping:
                        if (mapping["source_platform"], mapping["source_id"]) != (str(row.get("source") or row.get("source_kind") or "unknown").casefold(), str(row["asin"]).upper()):
                            action, reason = "CONFLICT", "Persisted mapping identity does not match this MASTER product"
                        else:
                            if remote_client is None: _cfg, remote_client = self._client(store_id)
                            remote = self._fetch_product(remote_client, {"id": mapping["shopify_product_id"]})
                            if not remote:
                                action, reason = "CONFLICT", "Mapped Shopify product is missing"
                            elif int(((remote.get("variantsCount") or {}).get("count") or 0)) != 1:
                                action, reason = "CONFLICT", "A single explicitly managed variant is required for configured store pricing"
                            elif self._remote_hash(remote) != mapping["last_remote_hash"]:
                                action, reason = "CONFLICT", "Shopify-owned product fields drifted since ShopSource sync"
                            elif phash == mapping["last_payload_hash"]:
                                action = "NO CHANGE"
                            else: action = "UPDATE"
                    else:
                        # An existing deterministic handle without a local mapping is never adopted silently.
                        if remote_client is None: _cfg, remote_client = self._client(store_id)
                        remote = self._fetch_product(remote_client, {"handle": payload["handle"]})
                        if remote:
                            action, reason = "CONFLICT", "Shopify product uses the source-derived handle but has no ShopSource mapping"
                    actions[action] += 1
                    item = {"master_product_id": row["master_product_id"], "source_id": row["asin"], "payload": payload,
                            "payload_hash": phash, "action": action, "remote_id": (remote or {}).get("id"), "reason": reason}
                    prepared.append(item)
                    source_fingerprints.append((row["master_product_id"], phash, final))
                except (ValueError, RuntimeError) as exc:
                    actions["SKIP"] += 1
                    skip_reasons[str(exc)] = skip_reasons.get(str(exc), 0) + 1
                    item = {"master_product_id": row["master_product_id"], "source_id": row.get("asin"), "action": "SKIP", "reason": str(exc)}
                    source_fingerprints.append((row["master_product_id"], "SKIP:" + str(exc), final))
            if len(prepared) < int(limit):
                # Only show a bounded sample; all eligible actions are retained persistently below on run creation.
                pass
        # Add ineligible rows as queue items too, then persist bounded batches and checkpoints.
        input_hash = _stable_hash({"fingerprints": source_fingerprints, "publish_status": publish_status, "media_mode": self.media_mode(store_id),
                                   "source_media_rights_confirmed": self.source_media_rights_confirmed(store_id)})
        run_id = "PSR_" + secrets.token_hex(10)
        now = _now()
        with connect(db if db is not None else self.db) as con:
            con.execute("INSERT INTO shopify_product_sync_runs(run_id,store_id,status,checkpoint,input_hash,counts_json,created_at,updated_at) VALUES(?,?,'PREVIEW',0,?,?,?,?)",
                (run_id, store_id, input_hash, json.dumps({"actions": actions, "publish_status": publish_status, "media_mode": self.media_mode(store_id),
                                                           "source_media_rights_confirmed": self.source_media_rights_confirmed(store_id)}), now, now))
            # The product queue is stored on disk; the returned UI preview contains only the requested sample.
            for row in self._catalog_rows(store_id, db=db):
                item = next((candidate for candidate in prepared if candidate["master_product_id"] == row["master_product_id"]), None)
                if item is None:
                    status = str(row.get("final_status") or "").upper()
                    action, payload, error = "SKIP", {}, status if status not in ELIGIBLE_STATUSES else "MISSING_OR_INVALID_DATA"
                else: action, payload, error = item["action"], item.get("payload", {}), item.get("reason", "")
                con.execute("INSERT INTO shopify_product_sync_items(run_id,master_product_id,source_id,action,status,payload_json,error) VALUES(?,?,?,?,?,?,?)",
                            (run_id, row["master_product_id"], row.get("asin", ""), action, "PENDING" if action in {"CREATE", "UPDATE"} else action,
                             json.dumps(payload, ensure_ascii=False), error))
        return {"run_id": run_id, "store_id": store_id, "provider": self.provider_name, "status": "PREVIEW",
                "counts": actions, "requested": len(source_fingerprints), "eligible": sum(actions[x] for x in ("CREATE", "UPDATE", "NO CHANGE")),
                "preview_items": prepared[:max(0, int(limit))], "input_hash": input_hash,
                "estimated_api_operations": 3 * (actions["CREATE"] + actions["UPDATE"]) + sum(len(row.get("payload", {}).get("owned_tags", [])) > 0 for row in prepared), "inventory": "UNMANAGED",
                "restricted": skip_reasons.get("RESTRICTED", 0), "archived": skip_reasons.get("ARCHIVED", 0),
                "missing_data": sum(value for reason, value in skip_reasons.items() if "MISSING_" in reason or "INVALID_" in reason),
                "skip_breakdown": skip_reasons,
                "media_mode": self.media_mode(store_id), "source_media_rights_confirmed": self.source_media_rights_confirmed(store_id),
                "media": "MANUAL_MEDIA / rights review required by default" if self.media_mode(store_id) == "MANUAL_MEDIA" else ("CONFIGURED_ADAPTER" if self.media_handler and (self.media_mode(store_id) not in {"SOURCE_MEDIA", "MIXED"} or self.source_media_rights_confirmed(store_id)) else "RIGHTS_REVIEW_REQUIRED" if self.media_mode(store_id) in {"SOURCE_MEDIA", "MIXED"} and not self.source_media_rights_confirmed(store_id) else "REQUIRES_CONFIGURATION"),
                "missing_store_price": actions["SKIP"]}

    @staticmethod
    def _fetch_product(client, identifier):
        key, value = next(iter(identifier.items()))
        lookup = {key: value} if key == "id" else {"handle": value}
        if key == "id":
            query = "query ShopSourceProductById($id: ID!) { product(id:$id) { id handle title descriptionHtml vendor productType status tags variants(first:2){nodes{id price compareAtPrice}} variantsCount{count} } }"
            data = client.execute(query, {"id": value})
            return data.get("product")
        data = client.execute(PRODUCT_READ_QUERY, {"identifier": lookup})
        return data.get("productByIdentifier")

    def sync(self, run_id: str, *, confirmed=False, expected_input_hash: str | None = None, batch_size: int | None = None) -> dict:
        if not confirmed:
            raise RuntimeError("Explicit user confirmation is required before Shopify product writes")
        with connect(self.db) as con:
            run = con.execute("SELECT * FROM shopify_product_sync_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run: raise KeyError(run_id)
            if run["status"] not in {"PREVIEW", "PAUSED", "RUNNING", "FAILED"}: raise RuntimeError("Run is not resumable")
            if expected_input_hash and expected_input_hash != run["input_hash"]: raise RuntimeError("Product preview is stale; create a new preview")
        try: run_options = json.loads(run["counts_json"] or "{}")
        except json.JSONDecodeError: run_options = {}
        if self._current_input_hash(run["store_id"], run_options.get("publish_status", "DRAFT")) != run["input_hash"]:
            raise RuntimeError("ShopSource products or Store Decisions changed after preview; create a new preview")
        config, client = self._client(run["store_id"])
        scopes = client.execute("query ShopSourceScopes { currentAppInstallation { accessScopes { handle } } }")
        granted = {row.get("handle") for row in (scopes.get("currentAppInstallation") or {}).get("accessScopes", [])}
        missing = {"read_products", "write_products"} - granted
        if missing: raise RuntimeError("Missing Shopify scopes: " + ", ".join(sorted(missing)))
        chunk_size = max(1, min(500, int(batch_size or self.batch_size)))
        with connect(self.db) as con:
            con.execute("UPDATE shopify_product_sync_runs SET status='RUNNING',updated_at=? WHERE run_id=?", (_now(), run_id))
        processed = 0
        while True:
            with connect(self.db) as con:
                rows = con.execute("SELECT * FROM shopify_product_sync_items WHERE run_id=? AND status='PENDING' ORDER BY master_product_id LIMIT ?", (run_id, chunk_size)).fetchall()
            if not rows: break
            for item in rows:
                if self._is_paused(run_id):
                    return self._run_summary(run_id)
                try:
                    result = self._sync_one(run["store_id"], item, client)
                except Exception as exc:
                    result = {"status": "FAILED", "error": self._safe_error(exc)}
                self._record_result(run_id, run["store_id"], item, result)
                processed += 1
            with connect(self.db) as con:
                con.execute("UPDATE shopify_product_sync_runs SET checkpoint=checkpoint+?,updated_at=? WHERE run_id=?", (len(rows), _now(), run_id))
        with connect(self.db) as con:
            failed = con.execute("SELECT COUNT(*) FROM shopify_product_sync_items WHERE run_id=? AND status IN ('FAILED','SYNCED_WITH_WARNINGS')", (run_id,)).fetchone()[0]
            con.execute("UPDATE shopify_product_sync_runs SET status=?,updated_at=? WHERE run_id=?", ("COMPLETE_WITH_WARNINGS" if failed else "COMPLETE", _now(), run_id))
        summary = self._run_summary(run_id); summary["processed_this_call"] = processed
        return summary

    def _sync_one(self, store_id, item, client):
        payload = json.loads(item["payload_json"] or "{}")
        if item["action"] == "NO CHANGE": return {"status": "NO CHANGE", "remote_id": item["remote_id"]}
        with connect(self.db) as con:
            mapping = con.execute("SELECT * FROM shopify_product_mappings WHERE store_id=? AND master_product_id=?", (store_id, item["master_product_id"])).fetchone()
        if mapping:
            remote_before = self._fetch_product(client, {"id": mapping["shopify_product_id"]})
            if not remote_before or remote_before.get("id") != mapping["shopify_product_id"]:
                return {"status": "FAILED", "error": "Mapped Shopify product missing before write; conflict needs review"}
            if self._remote_hash(remote_before) != mapping["last_remote_hash"]:
                return {"status": "FAILED", "error": "Shopify owned fields drifted after preview; conflict needs review"}
        else:
            remote_before = self._fetch_product(client, {"handle": payload["handle"]})
            if remote_before:
                return {"status": "FAILED", "error": "Unmapped Shopify handle appeared after preview; conflict needs review"}
        identity = {"handle": payload["handle"]}
        graphql_input = self._graphql_input(payload)
        last_error = None
        for attempt in range(self.max_attempts):
            try:
                response = client.execute(PRODUCT_SET_MUTATION, {"identifier": identity, "input": graphql_input}).get("productSet") or {}
                errors = response.get("userErrors") or []
                if errors: raise RuntimeError("Shopify userErrors: " + "; ".join(str(row.get("message", "error"))[:180] for row in errors))
                product = response.get("product") or {}
                if not product.get("id"): raise RuntimeError("Shopify productSet returned no product ID")
                variants = (product.get("variants") or {}).get("nodes") or []
                if int((product.get("variantsCount") or {}).get("count") or 0) != 1 or len(variants) != 1:
                    raise RuntimeError("Shopify price was not applied: product does not have exactly one variant")
                variant_input = {"id": variants[0]["id"], "price": payload["price"]}
                if payload.get("compare_at_price"):
                    variant_input["compareAtPrice"] = payload["compare_at_price"]
                variant_response = client.execute(VARIANT_PRICE_MUTATION, {"productId": product["id"], "variants": [variant_input]}).get("productVariantsBulkUpdate") or {}
                if variant_response.get("userErrors"):
                    raise RuntimeError("Shopify variant price userErrors: " + "; ".join(str(row.get("message", "error"))[:160] for row in variant_response["userErrors"]))
                if payload.get("owned_tags"):
                    tags_result = client.execute(TAGS_ADD_MUTATION, {"id": product["id"], "tags": payload["owned_tags"]}).get("tagsAdd") or {}
                    if tags_result.get("userErrors"): raise RuntimeError("Shopify tagsAdd returned userErrors")
                # This targeted one-variant update cannot replace or delete other variants.
                media_error = ""
                media_mode = self.media_mode(store_id)
                if media_mode in {"SOURCE_MEDIA", "MIXED"} and not self.source_media_rights_confirmed(store_id):
                    media_error = "SOURCE_IMAGE_RIGHTS_REVIEW_REQUIRED"
                elif media_mode in {"SOURCE_MEDIA", "MIXED"}:
                    try:
                        media_result = (self.media_handler(payload, product, media_mode) if self.media_handler
                                        else self._sync_source_media(store_id, item["master_product_id"], payload, product, client))
                        media_error = "; ".join(media_result.get("warnings", [])) if isinstance(media_result, dict) else ""
                    except Exception as exc: media_error = self._safe_error(exc)
                elif media_mode == "GENERATED_MEDIA":
                    if self.media_handler:
                        try: self.media_handler(payload, product, media_mode)
                        except Exception as exc: media_error = self._safe_error(exc)
                    else: media_error = "GENERATED_PRODUCT_MEDIA_PROVIDER_REQUIRES_CONFIGURATION"
                verified = self.verify(product["id"], expected_payload=payload, client=client)
                if not verified["verified"]: raise RuntimeError("Shopify read-after-write verification did not match owned scalar fields")
                remote_hash = self._remote_hash(verified["product"])
                with connect(self.db) as con:
                    con.execute("""INSERT INTO shopify_product_mappings(store_id,master_product_id,source_platform,source_id,
                      shopify_product_id,shopify_handle,last_payload_hash,last_remote_hash,sync_status,synced_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(store_id,master_product_id) DO UPDATE SET
                      shopify_product_id=excluded.shopify_product_id,shopify_handle=excluded.shopify_handle,
                      last_payload_hash=excluded.last_payload_hash,last_remote_hash=excluded.last_remote_hash,
                      sync_status=excluded.sync_status,synced_at=excluded.synced_at""",
                      (store_id, item["master_product_id"], payload["identity"].split(":", 1)[0], item["source_id"].upper(),
                       product["id"], product.get("handle", payload["handle"]), _stable_hash(payload), remote_hash, "SYNCED", _now()))
                return {"status": "SYNCED_WITH_WARNINGS" if media_error else "SYNCED", "remote_id": product["id"], "action": item["action"], "error": media_error}
            except Exception as exc:
                last_error = self._safe_error(exc)
                if not self._retryable(last_error) or attempt + 1 >= self.max_attempts: break
                self.wait(min(8.0, .25 * (2 ** attempt)))
        return {"status": "FAILED", "error": last_error or "Shopify write failed"}

    def _sync_source_media(self, store_id, master_product_id, payload, product, client):
        """Append explicitly rights-approved source images; never sends a media list to productSet."""
        images = payload.get("media") or []
        pending = []
        with connect(self.db) as con:
            known = {row["source_image_hash"]: dict(row) for row in con.execute(
                "SELECT * FROM shopify_product_media_mappings WHERE store_id=? AND master_product_id=?", (store_id, master_product_id))}
        for item in images:
            url = item if isinstance(item, str) else (item.get("url") or item.get("src") or item.get("originalSource") or item.get("original_source") or "")
            alt = "" if isinstance(item, str) else str(item.get("alt") or item.get("altText") or item.get("alt_text") or "")
            if not str(url).startswith("https://"):
                continue
            digest = hashlib.sha256(str(url).encode()).hexdigest()
            if digest not in known or known[digest].get("status") == "FAILED":
                pending.append({"url": str(url), "alt": alt, "hash": digest})
        warnings = []
        if pending:
            response = client.execute(PRODUCT_CREATE_MEDIA_MUTATION, {"productId": product["id"], "media": [
                {"mediaContentType": "IMAGE", "originalSource": row["url"], "alt": row["alt"] or payload.get("title", "")} for row in pending[:50]
            ]}).get("productCreateMedia") or {}
            media_errors = response.get("mediaUserErrors") or []
            created = response.get("media") or []
            # Map Shopify-returned successful assets by their alt text, preserving response order.
            for image, asset in zip(pending, created):
                if asset.get("id"):
                    with connect(self.db) as con:
                        con.execute("""INSERT INTO shopify_product_media_mappings(store_id,master_product_id,source_image_hash,
                          shopify_media_id,status,alt_text,image_url_verified,updated_at) VALUES(?,?,?,?,?,?,0,?)
                          ON CONFLICT(store_id,master_product_id,source_image_hash) DO UPDATE SET shopify_media_id=excluded.shopify_media_id,
                          status=excluded.status,alt_text=excluded.alt_text,updated_at=excluded.updated_at""",
                          (store_id, master_product_id, image["hash"], asset["id"], asset.get("status", "UPLOADED"), asset.get("alt") or image["alt"], _now()))
            if media_errors:
                warnings.extend(str(row.get("message", "media error"))[:180] for row in media_errors)
        with connect(self.db) as con:
            mappings = [dict(row) for row in con.execute("SELECT * FROM shopify_product_media_mappings WHERE store_id=? AND master_product_id=?", (store_id, master_product_id))]
        if not mappings:
            return {"warnings": ["No valid HTTPS source image was available"] if images else []}
        observed = client.execute(PRODUCT_MEDIA_QUERY, {"id": product["id"]}).get("product") or {}
        remote_media = {row.get("id"): row for row in ((observed.get("media") or {}).get("nodes") or [])}
        for mapping in mappings:
            asset = remote_media.get(mapping["shopify_media_id"])
            if not asset:
                warnings.append("Uploaded product image is not visible on Shopify yet")
                continue
            image_url = ((asset.get("image") or {}).get("url"))
            with connect(self.db) as con:
                con.execute("UPDATE shopify_product_media_mappings SET status=?,alt_text=?,image_url_verified=?,updated_at=? WHERE store_id=? AND master_product_id=? AND source_image_hash=?",
                            (asset.get("status", "PROCESSING"), asset.get("alt") or "", int(bool(image_url)), _now(), store_id, master_product_id, mapping["source_image_hash"]))
            if asset.get("status") == "FAILED": warnings.append("Shopify image processing failed")
            elif asset.get("status") != "READY" or not image_url: warnings.append("Shopify image is still processing; URL not ready")
        return {"warnings": sorted(set(warnings)), "ready": sum(bool(row.get("image") and row["image"].get("url")) for row in remote_media.values())}

    def verify(self, remote_id: str, *, expected_payload: dict | None = None, client=None) -> dict:
        if client is None:
            raise RuntimeError("Provide a configured read-only Shopify client")
        product = self._fetch_product(client, {"id": remote_id})
        if not product: return {"verified": False, "reason": "NOT_FOUND", "product": None}
        expected_payload = expected_payload or {}
        expected_fields = {key: expected_payload[key] for key in ("title", "descriptionHtml", "vendor", "productType", "status") if key in expected_payload}
        variants = (product.get("variants") or {}).get("nodes") or []
        verified = (all(product.get(key) == value for key, value in expected_fields.items())
                    and len(variants) == 1 and variants[0].get("price") == expected_payload.get("price")
                    and (not expected_payload.get("compare_at_price") or variants[0].get("compareAtPrice") == expected_payload.get("compare_at_price"))
                    and set(expected_payload.get("owned_tags", [])).issubset(set(product.get("tags") or [])))
        return {"verified": verified, "product": product, "inventory": "UNMANAGED", "media": "MANUAL_MEDIA"}

    def retry_failed(self, run_id: str, *, confirmed=False, expected_input_hash=None) -> dict:
        if not confirmed: raise RuntimeError("Explicit confirmation is required before retrying Shopify writes")
        with connect(self.db) as con:
            con.execute("UPDATE shopify_product_sync_items SET status='PENDING',error='',action='CREATE' WHERE run_id=? AND status='FAILED'", (run_id,))
            con.execute("UPDATE shopify_product_sync_runs SET status='PAUSED',retry_failed_only=1,updated_at=? WHERE run_id=?", (_now(), run_id))
        return self.sync(run_id, confirmed=True, expected_input_hash=expected_input_hash)

    def pause(self, run_id: str):
        with connect(self.db) as con:
            changed = con.execute("UPDATE shopify_product_sync_runs SET status='PAUSED',updated_at=? WHERE run_id=? AND status='RUNNING'", (_now(), run_id)).rowcount
        return changed > 0

    def _is_paused(self, run_id):
        with connect(self.db) as con:
            row = con.execute("SELECT status FROM shopify_product_sync_runs WHERE run_id=?", (run_id,)).fetchone()
        return not row or row["status"] == "PAUSED"

    def _record_result(self, run_id, store_id, item, result):
        status = result.get("status", "FAILED")
        with connect(self.db) as con:
            con.execute("UPDATE shopify_product_sync_items SET status=?,error=?,remote_id=COALESCE(?,remote_id),attempts=attempts+1 WHERE run_id=? AND master_product_id=?",
                        (status, result.get("error", ""), result.get("remote_id"), run_id, item["master_product_id"]))
            con.execute("INSERT INTO shopify_product_audit(run_id,store_id,master_product_id,remote_id,action,result,error,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, store_id, item["master_product_id"], result.get("remote_id"), item["action"], status, result.get("error", ""), _now()))

    @staticmethod
    def _retryable(error):
        value = str(error).casefold()
        return any(word in value for word in ("throttl", "timeout", "temporar", "502", "503", "504", "429", "rate limit"))

    @staticmethod
    def _safe_error(exc):
        # GraphQL transport suppresses headers; additionally redact anything token-shaped.
        message = str(exc)
        message = re.sub(r"shpat_[A-Za-z0-9]+|[A-Za-z0-9_-]{35,}", "[REDACTED]", message)
        return message[:400]

    def _run_summary(self, run_id):
        with connect(self.db) as con:
            run = con.execute("SELECT * FROM shopify_product_sync_runs WHERE run_id=?", (run_id,)).fetchone()
            counts = {row["status"]: row["count"] for row in con.execute("SELECT status,COUNT(*) count FROM shopify_product_sync_items WHERE run_id=? GROUP BY status", (run_id,))}
        return {"run_id": run_id, "store_id": run["store_id"], "status": run["status"], "checkpoint": run["checkpoint"], "counts": counts,
                "input_hash": run["input_hash"], "retry_failed_only": bool(run["retry_failed_only"])}


class SparkFallbackPublisher(ProductPublisher):
    provider_name = "SPARK_FALLBACK"

    def preview(self, store_id, *, db=None, statuses=None, limit=100):
        from .connectors.spark_center_package import SparkCenterPackageService
        allowed = statuses or sorted(ELIGIBLE_STATUSES)
        return {"provider": self.provider_name, "store_id": store_id, "status": "MANUAL_ACTION_REQUIRED",
                "package_preview": SparkCenterPackageService().preview(store_id=store_id, statuses=allowed, limit=limit, db=db),
                "manual_gate": "Spark Desktop staging and user-confirmed Shopify upload are required; no GUI automation."}

    def sync(self, *args, **kwargs): raise RuntimeError("Spark fallback has a required manual upload gate")
    def retry_failed(self, *args, **kwargs): raise RuntimeError("Retry after confirming SparkShopify upload in the UI")
    def verify(self, *args, **kwargs): return {"status": "MANUAL_ACTION_REQUIRED"}
