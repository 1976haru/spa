"""Local-first featured-product planning, read-only catalog discovery and preview."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from copy import deepcopy

from .db import connect, init_db
from .paths import EXPORT_DIR
from .shopify_products import _install_schema as install_product_schema
from .shopify_collections import ShopifyGraphQLClient, get_connection, get_shopify_token
from .shopify_theme_json import ShopifyJsonDocumentError, parse_shopify_json_document, render_shopify_json_document
from .shopify_theme_ids import is_valid_shopify_instance_id, legacy_featured_id_for_store, legacy_shopify_instance_kind, shopify_instance_id

MODES = {"NEW_ARRIVALS", "BALANCED_CATEGORIES", "MANUAL_SELECTION"}
ELIGIBLE_DECISIONS = {"PRIMARY", "RESERVE_A", "RESERVE_B", "PRODUCTION_CANDIDATE"}
VERIFIED_MAPPING_STATES = {"SYNCED", "VERIFIED", "NO CHANGE", "NO_CHANGE"}
FEATURED_THEME_FILE_QUERY = "query FeaturedCurrentTheme($id: ID!, $filenames: [String!]!) { theme(id: $id) { id name role files(first: 1, filenames: $filenames) { nodes { filename body { __typename ... on OnlineStoreThemeFileBodyText { content } ... on OnlineStoreThemeFileBodyBase64 { contentBase64 } } } userErrors { code filename } } } }"
FEATURED_SCOPES_QUERY = "query FeaturedScopes { currentAppInstallation { accessScopes { handle } } }"


def validate_isolated_featured_diff(before: dict, proposed: dict, section_id: str, *, legacy_id: str | None = None,
                                   store_id: str | None = None) -> dict:
    """Allow only the new featured instance plus an exact proven legacy migration."""
    unexpected, allowed = [], []
    result = {"safe": False, "allowed_changes": allowed, "unexpected_paths": unexpected,
              "legacy_removed": None, "new_section_id": section_id}
    if not isinstance(before, dict) or not isinstance(proposed, dict) or not is_valid_shopify_instance_id(section_id):
        result["unexpected_paths"].append("$"); return result
    left, right = deepcopy(before), deepcopy(proposed)
    left.setdefault("sections", {}); right.setdefault("sections", {})
    left.setdefault("order", []); right.setdefault("order", [])
    ls, rs, lo, ro = left.get("sections"), right.get("sections"), left.get("order"), right.get("order")
    if not isinstance(ls, dict) or not isinstance(rs, dict): result["unexpected_paths"].append("sections"); return result
    if not isinstance(lo, list) or not isinstance(ro, list): result["unexpected_paths"].append("order"); return result
    # Legacy deletion is accepted only for the exact expected Store ID-derived featured ID.
    old_featured = [key for key in ls if legacy_shopify_instance_kind(key) == "featured_products"]
    if legacy_id is None and old_featured:
        result["unexpected_paths"].append("legacy_featured_id"); return result
    if legacy_id is not None:
        if (legacy_id not in old_featured or len(old_featured) != 1
                or (store_id is not None and legacy_id != legacy_featured_id_for_store(store_id))):
            result["unexpected_paths"].append("legacy_featured_id"); return result
        if lo.count(legacy_id) != 1:
            result["unexpected_paths"].append("order.legacy_featured_count"); return result
        del ls[legacy_id]
        if legacy_id in rs: result["unexpected_paths"].append(f"sections.{legacy_id}")
        lo = [x for x in lo if x != legacy_id]
        if legacy_id in ro: result["unexpected_paths"].append("order.legacy_featured_id")
        result["legacy_removed"] = legacy_id
        allowed.extend([f"sections.{legacy_id}(legacy removal)", "order(legacy featured removal)"])
    old_section, new_section = ls.pop(section_id, None), rs.pop(section_id, None)
    old_order = [x for x in lo if x != section_id]
    new_order = [x for x in ro if x != section_id]
    left["order"], right["order"] = old_order, new_order
    if left != right:
        result["unexpected_paths"].append("unrelated_semantic_change")
    if sum(x == section_id for x in ro) != 1: result["unexpected_paths"].append("order.new_section_count")
    if new_section is None: result["unexpected_paths"].append(f"sections.{section_id}")
    if old_section != new_section: allowed.append(f"sections.{section_id}")
    if lo != ro: allowed.append("order(new featured placement)")
    result["allowed_changes"] = allowed
    result["unexpected_paths"] = sorted(set(result["unexpected_paths"]))
    result["safe"] = not result["unexpected_paths"]
    return result


def _now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def _json(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
def _hash(value): return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _install(db=None):
    install_product_schema(db)
    with connect(db) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS homepage_featured_product_plans(
          plan_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,mode TEXT NOT NULL,heading TEXT NOT NULL,
          subheading TEXT NOT NULL DEFAULT '',requested_count INTEGER NOT NULL,collection_key TEXT,
          collection_handle TEXT,status TEXT NOT NULL,source_hash TEXT NOT NULL,preview_hash TEXT,
          remote_verified INTEGER NOT NULL DEFAULT 0,remote_json_verified INTEGER NOT NULL DEFAULT 0,
          storefront_verified INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_featured_product_plan_store
          ON homepage_featured_product_plans(store_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS homepage_featured_product_items(
          plan_id TEXT NOT NULL,position INTEGER NOT NULL,source_kind TEXT NOT NULL DEFAULT 'SHOPSOURCE_MANAGED',
          source_key TEXT NOT NULL DEFAULT '',master_product_id INTEGER,
          shopify_product_id TEXT NOT NULL,shopify_handle TEXT NOT NULL,title TEXT NOT NULL,
          category_key TEXT,image_url TEXT,price REAL,remote_status TEXT NOT NULL,
          selection_reason TEXT NOT NULL,verification_status TEXT NOT NULL,created_at TEXT,updated_at TEXT,
          PRIMARY KEY(plan_id,position),UNIQUE(plan_id,shopify_product_id),UNIQUE(plan_id,shopify_handle));
        CREATE TABLE IF NOT EXISTS homepage_featured_product_remote_cache(
          store_id TEXT PRIMARY KEY,fetched_at TEXT NOT NULL,product_count INTEGER NOT NULL,
          source_hash TEXT NOT NULL,candidates_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS homepage_featured_theme_backups(
          backup_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,theme_id TEXT NOT NULL,filename TEXT NOT NULL,
          plan_id TEXT NOT NULL,preview_hash TEXT NOT NULL,section_id TEXT NOT NULL,folder TEXT NOT NULL,
          before_raw_hash TEXT NOT NULL,proposed_raw_hash TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
        """)
        plan_columns = {row["name"] for row in con.execute("PRAGMA table_info(homepage_featured_product_plans)")}
        if "remote_json_verified" not in plan_columns:
            con.execute("ALTER TABLE homepage_featured_product_plans ADD COLUMN remote_json_verified INTEGER NOT NULL DEFAULT 0")
        if "storefront_verified" not in plan_columns:
            con.execute("ALTER TABLE homepage_featured_product_plans ADD COLUMN storefront_verified INTEGER NOT NULL DEFAULT 0")
        columns = {row["name"] for row in con.execute("PRAGMA table_info(homepage_featured_product_items)")}
        if "source_kind" not in columns:
            con.executescript("""
            ALTER TABLE homepage_featured_product_items RENAME TO homepage_featured_product_items_phase43;
            CREATE TABLE homepage_featured_product_items(
              plan_id TEXT NOT NULL,position INTEGER NOT NULL,source_kind TEXT NOT NULL DEFAULT 'SHOPSOURCE_MANAGED',
              source_key TEXT NOT NULL DEFAULT '',master_product_id INTEGER,shopify_product_id TEXT NOT NULL,
              shopify_handle TEXT NOT NULL,title TEXT NOT NULL,category_key TEXT,image_url TEXT,price REAL,
              remote_status TEXT NOT NULL,selection_reason TEXT NOT NULL,verification_status TEXT NOT NULL,
              created_at TEXT,updated_at TEXT,PRIMARY KEY(plan_id,position),
              UNIQUE(plan_id,shopify_product_id),UNIQUE(plan_id,shopify_handle));
            INSERT INTO homepage_featured_product_items(
              plan_id,position,source_kind,source_key,master_product_id,shopify_product_id,shopify_handle,title,
              category_key,image_url,price,remote_status,selection_reason,verification_status,created_at,updated_at)
            SELECT plan_id,position,'SHOPSOURCE_MANAGED','master:' || master_product_id,master_product_id,
              shopify_product_id,shopify_handle,title,category_key,image_url,price,remote_status,
              selection_reason,verification_status,NULL,NULL FROM homepage_featured_product_items_phase43;
            DROP TABLE homepage_featured_product_items_phase43;
            """)
            columns = {row["name"] for row in con.execute("PRAGMA table_info(homepage_featured_product_items)")}
        if "merchandising_group" not in columns:
            con.execute("ALTER TABLE homepage_featured_product_items ADD COLUMN merchandising_group TEXT NOT NULL DEFAULT 'Other'")
        if "suitability_score" not in columns:
            con.execute("ALTER TABLE homepage_featured_product_items ADD COLUMN suitability_score INTEGER NOT NULL DEFAULT 50")


EXISTING_PRODUCTS_QUERY = """query ShopSourceExistingProducts($first:Int!,$after:String) {
  currentAppInstallation { accessScopes { handle } }
  products(first:$first,after:$after,sortKey:UPDATED_AT,reverse:true) {
    nodes { id handle title status createdAt updatedAt productType tags onlineStoreUrl
      featuredMedia { ... on MediaImage { image { url altText } } }
      variants(first:20) { nodes { price compareAtPrice } }
      variantsCount { count } }
    pageInfo { hasNextPage endCursor }
  }
}"""


class ShopifyExistingProductReader:
    """Bounded, cached Admin GraphQL catalog read with no remote-write method."""
    def __init__(self, *, db=None, client_factory=ShopifyGraphQLClient, cache_minutes=5, page_size=100, max_pages=5):
        self.db, self.client_factory = db, client_factory
        self.cache_minutes, self.page_size, self.max_pages = int(cache_minutes), min(100, int(page_size)), min(20, int(max_pages))
        _install(db)

    @staticmethod
    def _normalize(row: dict) -> dict:
        product_id, handle = str(row.get("id") or ""), str(row.get("handle") or "").strip()
        variants = ((row.get("variants") or {}).get("nodes") or [])
        prices = []
        for variant in variants:
            try:
                value = float(variant.get("price"))
                if value > 0: prices.append(value)
            except (TypeError, ValueError): pass
        media = row.get("featuredMedia") or {}; image = media.get("image") or {}
        image_url = image.get("url")
        status = str(row.get("status") or "").upper(); reasons = []
        if not product_id.startswith("gid://shopify/Product/"): reasons.append("MISSING_SHOPIFY_PRODUCT_ID")
        if not handle: reasons.append("MISSING_REAL_HANDLE")
        if status != "ACTIVE": reasons.append("NOT_STOREFRONT_ELIGIBLE")
        if not prices: reasons.append("MISSING_VALID_RETAIL_PRICE")
        if not image_url: reasons.append("NEEDS_IMAGE")
        if row.get("publishedOnCurrentPublication") is False: reasons.append("NOT_PUBLISHED_ON_CURRENT_PUBLICATION")
        tags = list(row.get("tags") or []); product_type = str(row.get("productType") or "").strip()
        category = product_type or (tags[0] if tags else _title_group(row.get("title")))
        return {"source_kind": "EXISTING_SHOPIFY", "source_key": product_id, "master_product_id": None,
                "shopify_product_id": product_id, "shopify_handle": handle, "title": str(row.get("title") or ""),
                "remote_status": status, "price": min(prices) if prices else None, "image_url": image_url,
                "image_alt": image.get("altText"), "product_type": product_type, "tags": tags,
                "created_at": row.get("createdAt"), "updated_at": row.get("updatedAt"),
                "synced_at": None, "category_key": category or "uncategorized",
                "product_link": row.get("onlineStoreUrl") or (f"/products/{handle}" if handle else None),
                "storefront_eligible": not reasons, "eligible": not reasons, "eligibility_reasons": reasons,
                "verification_status": "REMOTE_READ_VERIFIED"}

    def read(self, store_id: str, *, force=False) -> dict:
        now = datetime.now(timezone.utc)
        with connect(self.db) as con:
            cached = con.execute("SELECT * FROM homepage_featured_product_remote_cache WHERE store_id=?", (store_id,)).fetchone()
        if cached and not force:
            fetched = datetime.fromisoformat(cached["fetched_at"])
            if now - fetched <= timedelta(minutes=self.cache_minutes):
                return {"status": "READ_PRODUCTS_READY", "store_id": store_id, "products": json.loads(cached["candidates_json"]),
                        "product_count": cached["product_count"], "fetched_at": cached["fetched_at"], "cache_used": True, "write_performed": False}
        config = get_connection(store_id, db=self.db); token, _ = get_shopify_token(store_id, db=self.db)
        if not config or not token:
            return {"status": "MISSING_READ_PRODUCTS_SCOPE", "reason": "Shopify connection or credential missing", "products": [], "write_performed": False}
        client = self.client_factory(config["shop_domain"], token, config["api_version"])
        products, after = [], None
        for _ in range(self.max_pages):
            data = client.execute(EXISTING_PRODUCTS_QUERY, {"first": self.page_size, "after": after})
            scopes = {x.get("handle") for x in (data.get("currentAppInstallation") or {}).get("accessScopes", [])}
            if "read_products" not in scopes:
                return {"status": "MISSING_READ_PRODUCTS_SCOPE", "reason": "Shopify 기존 상품을 읽으려면 read_products 권한이 필요합니다.",
                        "products": [], "write_performed": False}
            connection = data.get("products") or {}; products.extend(self._normalize(x) for x in connection.get("nodes", []))
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"): break
            after = page.get("endCursor")
            if not after: break
        fetched_at, source_hash = now.isoformat(timespec="seconds"), _hash(products)
        with connect(self.db) as con:
            con.execute("""INSERT INTO homepage_featured_product_remote_cache VALUES(?,?,?,?,?)
              ON CONFLICT(store_id) DO UPDATE SET fetched_at=excluded.fetched_at,product_count=excluded.product_count,
              source_hash=excluded.source_hash,candidates_json=excluded.candidates_json""",
              (store_id, fetched_at, len(products), source_hash, _json(products)))
        return {"status": "READ_PRODUCTS_READY", "store_id": store_id, "products": products,
                "product_count": len(products), "fetched_at": fetched_at, "cache_used": False, "write_performed": False}


def _title_group(title) -> str:
    words = re.findall(r"[a-z0-9]+", str(title or "").casefold())
    return " ".join(words[:2]) or "uncategorized"


def normalize_merchandising_group(candidate: dict) -> str:
    """Return a short display/diversity group without changing source taxonomy."""
    text = " ".join(str(candidate.get(key) or "") for key in
                     ("collection_name", "collection", "category_key", "product_type", "tags", "title")).casefold()
    rules = (
        ("Trash & Cleanup", ("trash", "garbage", "waste", "cleanup")),
        ("Trunk & Cargo", ("trunk", "cargo")),
        ("Seat & Backseat", ("backseat", "seat back", "seat-back", "seat organizer")),
        ("Console & Small Storage", ("console organizer", "console storage", "center console organizer", "console")),
        ("Cup Holder & Convenience", ("cup holder", "cupholder")),
        ("Document & Visor", ("visor", "document holder", "registration holder")),
        ("General Organization", ("organizer", "organization", "storage", "holder")),
    )
    for label, needles in rules:
        if any(needle in text for needle in needles): return label
    return "Other"


def merchandising_suitability(candidate: dict) -> int:
    """Rank broad organizing products ahead of fitment-specific replacement parts."""
    title = str(candidate.get("title") or "").casefold()
    context = " ".join([title, str(candidate.get("product_type") or ""), " ".join(map(str, candidate.get("tags") or []))]).casefold()
    score = 50
    for word, points in (("organizer", 24), ("storage", 18), ("trash", 18), ("holder", 14), ("cargo", 14),
                         ("trunk", 12), ("backseat", 14), ("seat back", 12), ("travel", 8), ("cup holder", 8), ("visor", 8)):
        if word in context: score += points
    for word, points in (("replacement", 45), ("trim", 24), ("lid", 20), ("panel", 28), ("oem", 35),
                         ("direct-fit", 30), ("direct fit", 30), ("repair", 24), ("assembly", 18), ("replacement part", 35)):
        if word in context: score -= points
    if re.search(r"\b(?:19|20)\d{2}\s*(?:-|–|to)\s*(?:19|20)\d{2}\b", context): score -= 45
    if re.search(r"\b(?:fits?|for)\s+\d{4}\b", context): score -= 24
    if re.search(r"\b[A-Z0-9]{2,}[- ]\d{3,}\b", str(candidate.get("title") or ""), re.I): score -= 18
    return score


def discover_featured_product_schema(section_files: dict[str, str]) -> dict:
    """Use only proven schema field types/ids; never infer from a theme name."""
    if not isinstance(section_files, dict):
        return {"status": "MANUAL_ACTION_REQUIRED", "confidence": "LOW", "reason": "테마 section schema 목록 형식이 올바르지 않습니다."}
    candidates = []
    for filename, raw in sorted(section_files.items()):
        if not isinstance(filename, str) or not isinstance(raw, str): continue
        match = re.search(r"\{%[- ]*schema[- ]*%\}(.*?)\{%[- ]*endschema[- ]*%\}", raw, re.S | re.I)
        if not match: continue
        try: schema = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, TypeError): continue
        settings = schema.get("settings") or []
        if not isinstance(settings, list) or any(not isinstance(field, dict) for field in settings): continue
        collection = next((x for x in settings if x.get("type") == "collection"), None)
        product_list = next((x for x in settings if x.get("type") in {"product_list", "product"}), None)
        if not collection and not product_list: continue
        searchable = f"{filename} {schema.get('name', '')}".casefold()
        if not any(word in searchable for word in ("featured", "product", "collection")): continue
        def field(kind):
            return next((x for x in settings if kind in f"{x.get('id','')} {x.get('label','')}".casefold()), None)
        mode = "DIRECT_PRODUCTS" if product_list else "FEATURED_COLLECTION"
        candidates.append({"filename": filename, "type": filename.rsplit("/", 1)[-1].removesuffix(".liquid"),
                           "mode": mode, "confidence": "HIGH", "schema": schema,
                           "product_field": product_list, "collection_field": collection,
                           "heading_field": field("heading") or field("title"),
                           "subheading_field": field("subheading") or field("description"),
                           "count_field": field("product") if collection else None,
                           "columns_desktop_field": field("columns desktop"),
                           "columns_mobile_field": field("columns mobile"), "image_ratio_field": field("image ratio")})
    if not candidates:
        return {"status": "MANUAL_ACTION_REQUIRED", "confidence": "LOW", "reason": "현재 테마에 안전하게 확인된 상품 리스트 섹션이 없습니다."}
    candidates.sort(key=lambda x: (x["mode"] != "DIRECT_PRODUCTS", x["filename"]))
    return {"status": "READY", **candidates[0]}


class FeaturedProductAssignmentService:
    def __init__(self, *, db=None, export_dir: str | Path | None = None):
        self.db, self.export_dir = db, Path(export_dir) if export_dir else EXPORT_DIR
        _install(db)

    def eligible_products(self, store_id: str) -> list[dict]:
        with connect(self.db) as con:
            rows = con.execute("""SELECT p.id master_product_id,p.title,p.category,p.tags_json,p.raw_json,p.images_json,
                d.final_status,m.shopify_product_id,m.shopify_handle,m.sync_status,m.synced_at,m.last_remote_hash
                FROM products p JOIN store_product_decisions d ON d.product_id=p.id AND d.store_id=?
                JOIN shopify_product_mappings m ON m.master_product_id=p.id AND m.store_id=?
                WHERE p.archived=0 ORDER BY m.synced_at DESC,p.id""", (store_id, store_id)).fetchall()
        result = []
        for source in rows:
            row = dict(source); reasons = []
            try: raw = json.loads(row.pop("raw_json") or "{}")
            except json.JSONDecodeError: raw = {}
            try: images = json.loads(row.pop("images_json") or "[]")
            except json.JSONDecodeError: images = []
            try: tags = json.loads(row.pop("tags_json") or "[]")
            except json.JSONDecodeError: tags = []
            price = raw.get("shopify_selling_price", raw.get("store_selling_price"))
            image = next((x.get("url") if isinstance(x, dict) else x for x in images if x), None)
            if row["final_status"] not in ELIGIBLE_DECISIONS: reasons.append("STORE_DECISION_NOT_ELIGIBLE")
            if not row.get("shopify_product_id"): reasons.append("MISSING_SHOPIFY_PRODUCT_ID")
            if not str(row.get("shopify_handle") or "").strip(): reasons.append("MISSING_REAL_HANDLE")
            if row.get("sync_status") not in VERIFIED_MAPPING_STATES: reasons.append("REMOTE_IDENTITY_NOT_VERIFIED")
            try: valid_price = float(price) > 0
            except (TypeError, ValueError): valid_price = False
            if not valid_price: reasons.append("MISSING_VALID_RETAIL_PRICE")
            if not image: reasons.append("NEEDS_IMAGE")
            storefront = str(raw.get("shopify_status") or raw.get("storefront_status") or "DRAFT").upper()
            if storefront != "ACTIVE": reasons.append("NOT_STOREFRONT_ELIGIBLE")
            row.update(price=float(price) if valid_price else None, image_url=image,
                       source_kind="SHOPSOURCE_MANAGED", source_key=f"master:{row['master_product_id']}",
                       category_key=row.get("category") or (tags[0] if tags else "uncategorized"),
                       storefront_status=storefront, eligibility_reasons=reasons, eligible=not reasons,
                       remote_status=storefront, created_at=raw.get("shopify_created_at"),
                       updated_at=raw.get("shopify_updated_at"),
                       product_link=f"/products/{row['shopify_handle']}" if row.get("shopify_handle") else None)
            result.append(row)
        return result

    def candidate_union(self, store_id: str, *, force_remote=False, reader=None) -> dict:
        managed = self.eligible_products(store_id)
        remote_result = (reader or ShopifyExistingProductReader(db=self.db)).read(store_id, force=force_remote)
        remote = remote_result.get("products", []) if remote_result.get("status") == "READ_PRODUCTS_READY" else []
        combined, seen_ids, seen_handles, duplicates = [], set(), set(), 0
        for row in [*managed, *remote]:
            product_id = str(row.get("shopify_product_id") or "")
            handle = str(row.get("shopify_handle") or "").strip().casefold()
            if product_id in seen_ids or (handle and handle in seen_handles): duplicates += 1; continue
            seen_ids.add(product_id)
            if handle: seen_handles.add(handle)
            combined.append(row)
        reason_counts = {}
        for row in combined:
            for reason in row.get("eligibility_reasons", []): reason_counts[reason] = reason_counts.get(reason, 0) + 1
        eligible = [x for x in combined if x.get("eligible")]
        return {"status": remote_result.get("status"), "managed_count": len([x for x in managed if x.get('eligible')]),
                "remote_product_count": len(remote), "remote_eligible_count": len([x for x in remote if x.get('eligible')]),
                "eligible_count": len(eligible), "duplicate_count": duplicates, "reason_counts": reason_counts,
                "candidates": combined, "eligible": eligible, "cache_used": remote_result.get("cache_used", False),
                "scope_reason": remote_result.get("reason"), "write_performed": False}

    def create_plan(self, store_id: str, *, mode="BALANCED_CATEGORIES", requested_count=4,
                    heading="New Arrivals", subheading="", manual_product_ids=None,
                    collection_key=None, collection_handle=None, include_existing=False,
                    force_remote=False, reader=None, exclude_shopify_ids=None) -> dict:
        mode = str(mode).upper()
        if mode not in MODES: raise ValueError(f"Unsupported featured-product mode: {mode}")
        requested_count = max(1, int(requested_count))
        union = self.candidate_union(store_id, force_remote=force_remote, reader=reader) if include_existing else None
        candidates = union["candidates"] if union else self.eligible_products(store_id)
        eligible = [x for x in candidates if x["eligible"]]
        excluded = set(map(str, exclude_shopify_ids or []))
        alternatives = [x for x in eligible if str(x.get("shopify_product_id")) not in excluded]
        reused_previous = bool(excluded and len(alternatives) < requested_count)
        if excluded and not reused_previous: eligible = alternatives
        for row in eligible:
            row["merchandising_group"] = normalize_merchandising_group(row)
            row["suitability_score"] = merchandising_suitability(row)
        if mode == "MANUAL_SELECTION":
            wanted = list(manual_product_ids or [])
            by_id = {(x["master_product_id"] if x.get("master_product_id") is not None else x["source_key"]): x for x in eligible}
            selected = [by_id[x] for x in wanted if x in by_id][:requested_count]
        elif mode == "NEW_ARRIVALS":
            selected = sorted(eligible, key=lambda x: (x.get("created_at") or x.get("synced_at") or "", x["source_key"]), reverse=True)[:requested_count]
        else:
            ranked = sorted(eligible, key=lambda x: (x["suitability_score"],
                str(x.get("created_at") or x.get("synced_at") or ""), str(x.get("shopify_product_id") or "")), reverse=True)
            selected, seen = [], set()
            for row in ranked:
                key = row["merchandising_group"]
                if key not in seen: selected.append(row); seen.add(key)
                if len(selected) == requested_count: break
            if len(selected) < requested_count:
                selected.extend(x for x in ranked if x not in selected)
                selected = selected[:requested_count]
        ids, handles = [x["shopify_product_id"] for x in selected], [x["shopify_handle"].casefold() for x in selected]
        reasons = []
        if len(selected) < requested_count: reasons.append(f"ONLY_{len(selected)}_OF_{requested_count}_ELIGIBLE_PRODUCTS")
        if len(ids) != len(set(ids)): reasons.append("DUPLICATE_SHOPIFY_PRODUCT_ID")
        if len(handles) != len(set(handles)): reasons.append("DUPLICATE_SHOPIFY_HANDLE")
        status = "READY" if not reasons else "MANUAL_ACTION_REQUIRED"
        plan_id, now = "HFP_" + secrets.token_hex(10), _now()
        from .homepage_automation import invalidate_homepage_previews
        invalidate_homepage_previews(store_id, reason="Featured product selection changed", db=self.db)
        fingerprint = {"store_id": store_id, "mode": mode, "requested_count": requested_count,
                       "selected": [(x.get("source_kind"), x.get("source_key"), x["shopify_product_id"], x["shopify_handle"], x.get("created_at") or x.get("synced_at")) for x in selected]}
        with connect(self.db) as con:
            con.execute("""INSERT INTO homepage_featured_product_plans(
                plan_id,store_id,mode,heading,subheading,requested_count,collection_key,collection_handle,
                status,source_hash,preview_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (plan_id, store_id, mode, heading, subheading, requested_count, collection_key,
                         collection_handle, status, _hash(fingerprint), None, now, now))
            for position, row in enumerate(selected, 1):
                why = "manual order" if mode == "MANUAL_SELECTION" else ("newly created" if row.get("source_kind") == "EXISTING_SHOPIFY" else "recently synced") if mode == "NEW_ARRIVALS" else f"{row['merchandising_group']} · suitability {row['suitability_score']} · balanced ranking"
                con.execute("""INSERT INTO homepage_featured_product_items(
                    plan_id,position,source_kind,source_key,master_product_id,shopify_product_id,shopify_handle,title,
                    category_key,image_url,price,remote_status,selection_reason,verification_status,created_at,updated_at,
                    merchandising_group,suitability_score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (plan_id, position, row.get("source_kind", "SHOPSOURCE_MANAGED"), row.get("source_key", ""),
                             row.get("master_product_id"), row["shopify_product_id"], row["shopify_handle"], row["title"],
                             row["category_key"], row["image_url"], row["price"], row.get("remote_status") or row.get("storefront_status"),
                             why, row.get("verification_status", "LOCAL_ELIGIBILITY_VERIFIED"), now, now,
                             row["merchandising_group"], row["suitability_score"]))
        plan = self.get_plan(plan_id, reasons=reasons)
        plan["reselection_reused_previous"] = reused_previous
        plan["diagnostics"] = union or {"managed_count": len(eligible), "remote_product_count": 0, "remote_eligible_count": 0,
                                        "eligible_count": len(eligible), "reason_counts": {}, "duplicate_count": 0}
        return plan

    def reselect(self, previous_plan: dict, *, reader=None) -> dict:
        """Create a new deterministic plan, avoiding the immediately previous IDs when possible."""
        old_ids = [x.get("shopify_product_id") for x in previous_plan.get("items", [])]
        from .homepage_automation import invalidate_homepage_previews
        invalidate_homepage_previews(previous_plan["store_id"], reason="Featured product selection changed", db=self.db)
        if old_ids:
            with connect(self.db) as con:
                con.execute("UPDATE homepage_featured_product_plans SET preview_hash=NULL,updated_at=? WHERE plan_id=?",
                            (_now(), previous_plan["plan_id"]))
        return self.create_plan(previous_plan["store_id"], mode=previous_plan.get("mode") or "BALANCED_CATEGORIES",
            requested_count=previous_plan.get("requested_count", 4), heading=previous_plan.get("heading", "New Arrivals"),
            subheading=previous_plan.get("subheading", ""), collection_key=previous_plan.get("collection_key"),
            collection_handle=previous_plan.get("collection_handle"), include_existing=True, reader=reader,
            exclude_shopify_ids=old_ids)

    def get_plan(self, plan_id: str, *, reasons=None) -> dict:
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM homepage_featured_product_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if not row: raise KeyError(plan_id)
            items = [dict(x) for x in con.execute("SELECT * FROM homepage_featured_product_items WHERE plan_id=? ORDER BY position", (plan_id,))]
        for item in items:
            candidate = {"title": item.get("title"), "category_key": item.get("category_key")}
            item["merchandising_group"] = item.get("merchandising_group") or normalize_merchandising_group(candidate)
            if item.get("suitability_score") == 50:
                item["suitability_score"] = merchandising_suitability(candidate)
        return {**dict(row), "items": items, "reasons": list(reasons or []), "write_performed": False}

    def checklist(self, plan: dict, *, section_visible=False, remote_verified=False, storefront_verified=False) -> dict:
        items, required = plan.get("items", []), int(plan.get("requested_count", 4))
        checks = {"featured_products_count": len(items) >= required,
                  "featured_products_unique": len({x.get('shopify_product_id') for x in items}) == len(items) and len({x.get('shopify_handle') for x in items}) == len(items),
                  "featured_products_real_remote_ids": bool(items) and all(str(x.get("shopify_product_id", "")).startswith("gid://shopify/Product/") for x in items),
                  "featured_products_real_links": bool(items) and all(x.get("shopify_handle") for x in items),
                  "featured_products_price_valid": bool(items) and all(float(x.get("price") or 0) > 0 for x in items),
                  "featured_products_images_ready": bool(items) and all(x.get("image_url") for x in items),
                  "featured_products_storefront_eligible": bool(items) and all(x.get("remote_status") == "ACTIVE" for x in items),
                  "featured_products_section_visible": bool(section_visible or storefront_verified or plan.get("storefront_verified")),
                  "featured_products_remote_json_verified": bool(remote_verified or plan.get("remote_json_verified")),
                  "featured_products_storefront_verified": bool(storefront_verified or plan.get("storefront_verified"))}
        status = "ASSIGNMENT_READY" if all(checks.values()) else "REVIEW_REQUIRED"
        return {"status": status, "ready": status == "ASSIGNMENT_READY", "checks": checks}

    def build_theme_preview(self, plan: dict, snapshot: dict) -> dict:
        template_status = snapshot.get("template_status", "READY" if isinstance(snapshot.get("template"), dict) else "TEMPLATE_BODY_MISSING")
        if template_status != "READY":
            return {"status": "BLOCKED", "reason": (snapshot.get("template_error") or {}).get("message") or template_status,
                    "template_status": template_status, "write_performed": False}
        capability = discover_featured_product_schema(snapshot.get("theme_files") or {})
        if capability["status"] != "READY":
            return {"status": "MANUAL_ACTION_REQUIRED", "reason": capability["reason"], "instructions": [
                "Open Shopify Theme Editor or PageFly manually.", "Add a product grid or Featured collection section.",
                "Use the selected products in the saved ShopSource plan; do not activate DRAFT products automatically."], "write_performed": False}
        current = snapshot.get("template")
        if not isinstance(current, dict): return {"status": "BLOCKED", "reason": "Homepage JSON template unavailable", "write_performed": False}
        filename = snapshot.get("template_filename")
        raw = (snapshot.get("theme_files") or {}).get(filename) if filename else None
        document = None
        if raw is not None:
            try:
                document = parse_shopify_json_document(raw)
            except ShopifyJsonDocumentError as exc:
                return {"status": "BLOCKED", "reason": exc.message, "template_status": exc.code, "write_performed": False}
            if not isinstance(document.parsed, dict):
                return {"status": "BLOCKED", "reason": "Homepage source document root is not an object.",
                        "template_status": "INVALID_THEME_JSON", "write_performed": False}
        proposed = json.loads(json.dumps(current)); sections = proposed.setdefault("sections", {}); order = proposed.setdefault("order", [])
        section_id = shopify_instance_id("featuredproducts", plan["store_id"])
        expected_legacy_id = legacy_featured_id_for_store(plan["store_id"])
        legacy_ids = [key for key in sections if legacy_shopify_instance_kind(key) == "featured_products"]
        if legacy_ids and legacy_ids != [expected_legacy_id]:
            return {"status": "CONFLICT", "reason": "Legacy featured section does not match current store identity", "write_performed": False}
        legacy_id = expected_legacy_id if expected_legacy_id in sections else None
        settings = {}
        if capability.get("heading_field"): settings[capability["heading_field"]["id"]] = plan["heading"]
        if capability.get("subheading_field") and plan.get("subheading"): settings[capability["subheading_field"]["id"]] = plan["subheading"]
        if capability["mode"] == "DIRECT_PRODUCTS": settings[capability["product_field"]["id"]] = [x["shopify_product_id"] for x in plan["items"]]
        elif plan.get("collection_handle"): settings[capability["collection_field"]["id"]] = plan["collection_handle"]
        else: return {"status": "MANUAL_ACTION_REQUIRED", "reason": "추천 컬렉션 handle이 연결되지 않았습니다. 컬렉션 연결을 먼저 확인하세요.", "fallback": {
            "collection_key": plan.get("collection_key") or "homepage-new-arrivals", "title": plan["heading"],
            "owned_tag": "shopsource:homepage:featured-products", "preserve_merchant_tags": True}, "write_performed": False}
        before = sections.get(section_id); desired = {"type": capability["type"], "settings": settings}
        if legacy_id:
            legacy_section = sections.get(legacy_id) or {}
            legacy_settings = legacy_section.get("settings") or {}
            legacy_products = legacy_settings.get((capability.get("product_field") or {}).get("id"))
            expected_products = [x["shopify_product_id"] for x in plan["items"]]
            if legacy_section.get("type") != capability["type"] or (legacy_products is not None and legacy_products != expected_products):
                return {"status": "CONFLICT", "reason": "Legacy section type or product selection differs from current plan", "write_performed": False}
            if order.count(legacy_id) != 1:
                return {"status": "CONFLICT", "reason": "Legacy featured section order is missing or duplicated", "write_performed": False}
            del sections[legacy_id]
            order[:] = [value for value in order if value != legacy_id]
            if legacy_id in order or order.count(section_id) > 1:
                return {"status": "CONFLICT", "reason": "Legacy featured section order is ambiguous", "write_performed": False}
        sections[section_id] = desired
        if section_id not in order:
            footer = next((i for i, x in enumerate(order) if "footer" in str(x).casefold()), len(order)); order.insert(footer, section_id)
        preview_hash = _hash({"source": plan["source_hash"], "theme": snapshot.get("theme"), "current": current, "proposed": proposed,
                              "raw_hash": document.raw_hash if document else None,
                              "semantic_hash": document.semantic_hash if document else _hash(current)})
        with connect(self.db) as con: con.execute("UPDATE homepage_featured_product_plans SET preview_hash=?,updated_at=? WHERE plan_id=?", (preview_hash, _now(), plan["plan_id"]))
        source_document = ({"filename": filename, "before_raw_hash": document.raw_hash,
                           "before_semantic_hash": document.semantic_hash,
                           "proposed_raw_hash": hashlib.sha256(render_shopify_json_document(document, proposed).encode("utf-8")).hexdigest(),
                           "had_leading_comment": document.had_leading_comment,
                           "prefix_hash": hashlib.sha256(document.prefix.encode("utf-8")).hexdigest(),
                           "suffix_hash": hashlib.sha256(document.suffix.encode("utf-8")).hexdigest()} if document else None)
        return {"status": "PREVIEW", "plan_id": plan["plan_id"], "capability": capability, "current": current,
                "proposed": proposed, "section_id": section_id, "action": "NO_CHANGE" if before == desired else "UPDATE" if before else "CREATE",
                "migration": {"from": legacy_id, "to": section_id} if legacy_id else None,
                "preview_hash": preview_hash, "source_document": source_document,
                "write_performed": False, "unrelated_sections_preserved": True}

    def verify_remote(self, plan_id: str, *, preview_hash: str, remote_section: dict | None) -> dict:
        plan = self.get_plan(plan_id)
        if not plan.get("preview_hash") or plan["preview_hash"] != preview_hash:
            return {"status": "CONFLICT", "reason": "STALE_PREVIEW", "write_performed": False}
        visible = bool(remote_section and remote_section.get("visible", True))
        remote_ids = list((remote_section or {}).get("product_ids") or [])
        expected = [x["shopify_product_id"] for x in plan["items"]]
        verified = remote_section is not None and remote_ids == expected
        if verified:
            with connect(self.db) as con:
                con.execute("UPDATE homepage_featured_product_plans SET status='REMOTE_JSON_VERIFIED',remote_json_verified=1,storefront_verified=0,remote_verified=0,updated_at=? WHERE plan_id=?", (_now(), plan_id))
        return {"status": "REMOTE_JSON_VERIFIED" if verified else "REVIEW_REQUIRED", "section_visible": False,
                "product_ids_match": remote_ids == expected, "write_performed": False}

    def confirm_storefront(self, plan_id: str, *, checks: dict, confirmed: bool = False) -> dict:
        """Persist an explicit local human observation of the rendered storefront."""
        required = {"desktop_title", "four_cards", "product_links", "mobile_section"}
        if confirmed is not True or not isinstance(checks, dict) or not required.issubset(checks) or not all(checks.get(key) is True for key in required):
            return {"status": "REVIEW_REQUIRED", "reason": "All four storefront checks and explicit confirmation are required", "write_performed": False}
        plan = self.get_plan(plan_id)
        content_checks = self.checklist(plan)
        required_content = ("featured_products_count", "featured_products_unique", "featured_products_real_remote_ids",
                            "featured_products_real_links", "featured_products_price_valid", "featured_products_images_ready",
                            "featured_products_storefront_eligible", "featured_products_remote_json_verified")
        if not all(content_checks["checks"].get(key) for key in required_content):
            return {"status": "REVIEW_REQUIRED", "reason": "Featured product prerequisites are not ready", "write_performed": False}
        with connect(self.db) as con:
            row = con.execute("SELECT remote_json_verified FROM homepage_featured_product_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if not row or not row["remote_json_verified"]:
                return {"status": "REVIEW_REQUIRED", "reason": "Remote JSON must be verified before storefront confirmation", "write_performed": False}
            con.execute("UPDATE homepage_featured_product_plans SET status='STOREFRONT_VERIFIED',storefront_verified=1,updated_at=? WHERE plan_id=?", (_now(), plan_id))
        return {"status": "STOREFRONT_VERIFIED", "assignment_ready": True, "write_performed": False}

    def export_report(self, plan_id: str) -> Path:
        plan = self.get_plan(plan_id); folder = self.export_dir / "homepage_reports" / plan["store_id"] / plan_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "featured_products.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# Featured Products assignment", "", f"- Store: {plan['store_id']}", f"- Heading: {plan['heading']}",
                 f"- Section status: {plan['status']}", "", "## Selected products", ""]
        for item in plan["items"]: lines.append(f"- {item['position']}. {item['title']} — {item['selection_reason']} — /products/{item['shopify_handle']}")
        lines = lines[:8] + [f"- {item['position']}. [{item.get('source_kind')}] {item['title']} — {item['category_key']} — "
                            f"${item['price']:.2f} — {item['remote_status']} — /products/{item['shopify_handle']} — {item['selection_reason']}"
                            for item in plan["items"]]
        lines += ["", "## Screenshot checklist", "", "- Desktop section and product links", "- Mobile crop and cards", "- Price and image visibility", "",
                  "## 제출 메모", "", "실제 검증된 상품만 사용해 추천 상품 영역을 구성했습니다. 선택된 카테고리와 링크는 위 목록을 기준으로 확인합니다."]
        (folder / "assignment_featured_products.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return folder


class FeaturedProductThemeApplyService:
    """Guarded one-section theme writer; deliberately separate from homepage apply."""
    def __init__(self, *, db=None, export_dir: str | Path | None = None, client_factory=ShopifyGraphQLClient):
        self.db, self.export_dir, self.client_factory = db, Path(export_dir) if export_dir else EXPORT_DIR, client_factory
        _install(db)

    @staticmethod
    def _remote_document(theme: dict, filename: str):
        import base64
        nodes = ((theme.get("files") or {}).get("nodes") or [])
        row = next((item for item in nodes if item.get("filename") == filename), None)
        if row is None: raise ValueError("TEMPLATE_BODY_MISSING")
        body = row.get("body") or {}; raw = body.get("content")
        if raw is None and body.get("contentBase64"):
            try: raw = base64.b64decode(body["contentBase64"], validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError): raw = None
        if raw is None: raise ValueError("TEMPLATE_BODY_MISSING")
        return raw, parse_shopify_json_document(raw)

    @staticmethod
    def _four_ready(plan: dict) -> bool:
        items = plan.get("items") or []
        ids = [str(x.get("shopify_product_id") or "") for x in items]
        handles = [str(x.get("shopify_handle") or "") for x in items]
        if not (plan.get("status") == "READY" and plan.get("requested_count") == 4 and len(items) == 4
                and len(set(ids)) == 4 and len(set(handles)) == 4
                and all(x.startswith("gid://shopify/Product/") for x in ids)):
            return False
        for item in items:
            try: priced = float(item.get("price") or 0) > 0
            except (TypeError, ValueError): priced = False
            if item.get("remote_status") != "ACTIVE" or not priced or not item.get("image_url") or not item.get("shopify_handle"):
                return False
        return True

    def apply(self, plan_id: str, preview: dict, *, store_id: str, confirmed: bool = False, client=None) -> dict:
        if confirmed is not True: return {"status": "MANUAL_ACTION_REQUIRED", "reason": "Explicit user confirmation required", "write_performed": False}
        if not isinstance(preview, dict) or preview.get("status") != "PREVIEW" or preview.get("plan_id") != plan_id:
            return {"status": "CONFLICT", "reason": "Current featured preview is missing or stale", "write_performed": False}
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM homepage_featured_product_plans WHERE plan_id=?", (plan_id,)).fetchone()
            latest = con.execute("SELECT plan_id FROM homepage_featured_product_plans WHERE store_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (str(store_id),)).fetchone()
        if not row or row["store_id"] != str(store_id) or not latest or latest["plan_id"] != plan_id:
            return {"status": "CONFLICT", "reason": "Store or latest featured plan changed", "write_performed": False}
        plan = FeaturedProductAssignmentService(db=self.db).get_plan(plan_id)
        if not self._four_ready(plan): return {"status": "MANUAL_ACTION_REQUIRED", "reason": "Exactly four unique ACTIVE products with price, image and handle are required", "write_performed": False}
        source = preview.get("source_document") or {}; theme = preview.get("theme") or {}
        filename, section_id = preview.get("template_filename"), preview.get("section_id")
        expected_section = shopify_instance_id("featuredproducts", str(store_id))
        expected_legacy = legacy_featured_id_for_store(store_id)
        if (preview.get("store_id") != str(store_id) or preview.get("featured_products_plan_id") != plan_id
                or source.get("filename") != filename or preview.get("capability", {}).get("mode") != "DIRECT_PRODUCTS" or section_id != expected_section
                or not is_valid_shopify_instance_id(section_id)
                or filename != "templates/index.json" or not theme.get("id") or theme.get("role") != "MAIN"
                or not source.get("before_raw_hash") or not source.get("before_semantic_hash")
                or not row["preview_hash"] or row["preview_hash"] != preview.get("preview_hash")):
            return {"status": "CONFLICT", "reason": "Unsupported capability or preview identity/hash mismatch", "write_performed": False}
        config, token = get_connection(store_id, db=self.db), get_shopify_token(store_id, db=self.db)[0]
        if not config or not token: return {"status": "MANUAL_ACTION_REQUIRED", "reason": "Shopify connection or credential missing", "write_performed": False}
        client = client or self.client_factory(config["shop_domain"], token, config["api_version"])
        scopes = {x.get("handle") for x in (client.execute(FEATURED_SCOPES_QUERY).get("currentAppInstallation") or {}).get("accessScopes", [])}
        if not {"read_themes", "write_themes"}.issubset(scopes):
            return {"status": "MANUAL_ACTION_REQUIRED", "reason": "read_themes and write_themes scopes are required", "write_performed": False}
        remote = client.execute(FEATURED_THEME_FILE_QUERY, {"id": theme["id"], "filenames": [filename]}).get("theme") or {}
        if remote.get("id") != theme["id"] or remote.get("role") != "MAIN":
            return {"status": "CONFLICT", "reason": "MAIN theme identity changed", "write_performed": False}
        try: before_raw, document = self._remote_document(remote, filename)
        except (ValueError, ShopifyJsonDocumentError) as exc:
            return {"status": "CONFLICT", "reason": str(exc), "write_performed": False}
        if document.raw_hash != source["before_raw_hash"] or document.semantic_hash != source["before_semantic_hash"]:
            return {"status": "CONFLICT", "reason": "Remote source raw/semantic hash drifted", "write_performed": False}
        proposed = preview.get("proposed")
        migration = preview.get("migration") or {}
        legacy_id = migration.get("from")
        if legacy_id and (legacy_id != expected_legacy or legacy_shopify_instance_kind(legacy_id) != "featured_products"):
            return {"status": "CONFLICT", "reason": "Unrecognized legacy migration identity", "write_performed": False}
        guard = validate_isolated_featured_diff(document.parsed, proposed, section_id, legacy_id=legacy_id, store_id=store_id)
        if not guard["safe"]: return {"status": "CONFLICT", "reason": "Unexpected semantic changes", "unexpected_paths": guard["unexpected_paths"], "write_performed": False}
        expected_ids = [x["shopify_product_id"] for x in plan["items"]]
        selected_section = ((proposed.get("sections") or {}).get(section_id) or {}) if isinstance(proposed, dict) else {}
        field_id = (preview.get("capability", {}).get("product_field") or {}).get("id")
        proposed_ids = (selected_section.get("settings") or {}).get(field_id) if field_id else None
        if proposed_ids != expected_ids:
            return {"status": "CONFLICT", "reason": "Featured preview product IDs no longer match the current four-item plan", "write_performed": False}
        proposed_raw = render_shopify_json_document(document, proposed)
        proposed_doc = parse_shopify_json_document(proposed_raw)
        if (proposed_doc.prefix != document.prefix or proposed_doc.suffix != document.suffix
                or (source.get("proposed_raw_hash") and proposed_doc.raw_hash != source["proposed_raw_hash"])):
            return {"status": "CONFLICT", "reason": "Comment-aware raw render mismatch", "write_performed": False}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        folder = self.export_dir / "theme_backups" / str(store_id) / (stamp + "-featured-products")
        folder.mkdir(parents=True, exist_ok=False)
        items = [{key: item.get(key) for key in ("shopify_product_id", "shopify_handle", "title", "price", "image_url")} for item in plan["items"]]
        metadata = {"store_id": str(store_id), "theme_id": theme["id"], "theme_role": "MAIN", "template_filename": filename,
            "plan_id": plan_id, "preview_hash": preview["preview_hash"], "section_id": section_id,
            "before_raw_hash": document.raw_hash, "before_semantic_hash": document.semantic_hash,
            "proposed_raw_hash": proposed_doc.raw_hash, "selected_products": items, "timestamp": _now()}
        (folder / "before.raw.json").write_text(before_raw, encoding="utf-8", newline="")
        (folder / "proposed.raw.json").write_text(proposed_raw, encoding="utf-8", newline="")
        (folder / "before.parsed.json").write_text(json.dumps(document.parsed, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "proposed.parsed.json").write_text(json.dumps(proposed, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "selected_products.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "diff.md").write_text("# Isolated featured-products change\n\n" + "\n".join(f"- {x}" for x in guard["allowed_changes"]) + "\n", encoding="utf-8")
        (folder / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        backup_id = "HFPB_" + secrets.token_hex(8)
        with connect(self.db) as con:
            con.execute("INSERT INTO homepage_featured_theme_backups VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (backup_id, str(store_id), theme["id"], filename, plan_id, preview["preview_hash"], section_id,
                 str(folder), document.raw_hash, proposed_doc.raw_hash, "BACKED_UP", _now()))
        try:
            from .homepage_automation import UPSERT_THEME_FILES
            response = client.execute(UPSERT_THEME_FILES, {"themeId": theme["id"], "files": [{"filename": filename, "body": {"type": "TEXT", "value": proposed_raw}}]}).get("themeFilesUpsert") or {}
        except RuntimeError as exc:
            return {"status": "MANUAL_ACTION_REQUIRED" if "access denied" in str(exc).casefold() else "FAILED", "reason": "Shopify rejected the isolated theme write", "backup_id": backup_id, "write_performed": True}
        if response.get("userErrors"):
            return {"status": "FAILED", "backup_id": backup_id, "errors": response["userErrors"], "write_performed": True}
        after_theme = client.execute(FEATURED_THEME_FILE_QUERY, {"id": theme["id"], "filenames": [filename]}).get("theme") or {}
        try: after_raw, after_doc = self._remote_document(after_theme, filename)
        except (ValueError, ShopifyJsonDocumentError): after_raw, after_doc = None, None
        after = after_doc.parsed if after_doc else None
        after_guard = validate_isolated_featured_diff(document.parsed, after, section_id, legacy_id=legacy_id, store_id=store_id) if isinstance(after, dict) else {"safe": False, "unexpected_paths": ["$"], "allowed_changes": []}
        actual_ids = (((after or {}).get("sections") or {}).get(section_id) or {}).get("settings", {}).get(preview["capability"]["product_field"]["id"], [])
        verified = bool(after_theme.get("id") == theme["id"] and after_theme.get("role") == "MAIN" and after == proposed
                        and after_doc and after_doc.raw_hash == proposed_doc.raw_hash and after_doc.prefix == document.prefix
                        and after_doc.suffix == document.suffix and actual_ids == expected_ids and len(actual_ids) == 4 and after_guard["safe"])
        status = "REMOTE_JSON_VERIFIED" if verified else "VERIFY_FAILED"
        with connect(self.db) as con:
            con.execute("UPDATE homepage_featured_theme_backups SET status=? WHERE backup_id=?", (status, backup_id))
            if verified: con.execute("UPDATE homepage_featured_product_plans SET status='REMOTE_JSON_VERIFIED',remote_verified=0,remote_json_verified=1,storefront_verified=0,updated_at=? WHERE plan_id=?", (_now(), plan_id))
        return {"status": status, "backup_id": backup_id, "theme_id": theme["id"], "product_ids_match": actual_ids == expected_ids,
                "unrelated_sections_unchanged": after_guard["safe"], "write_performed": True}

    def rollback(self, backup_id: str, *, confirmed: bool = False, client=None) -> dict:
        if confirmed is not True: return {"status": "MANUAL_ACTION_REQUIRED", "reason": "Explicit rollback confirmation required", "write_performed": False}
        with connect(self.db) as con: row = con.execute("SELECT * FROM homepage_featured_theme_backups WHERE backup_id=?", (backup_id,)).fetchone()
        if not row: return {"status": "NOT_FOUND", "write_performed": False}
        folder = Path(row["folder"]); before_raw = (folder / "before.raw.json").read_text(encoding="utf-8")
        proposed_raw = (folder / "proposed.raw.json").read_text(encoding="utf-8")
        config, token = get_connection(row["store_id"], db=self.db), get_shopify_token(row["store_id"], db=self.db)[0]
        if not config or not token: return {"status": "MANUAL_ACTION_REQUIRED", "write_performed": False}
        client = client or self.client_factory(config["shop_domain"], token, config["api_version"])
        scopes = {x.get("handle") for x in (client.execute(FEATURED_SCOPES_QUERY).get("currentAppInstallation") or {}).get("accessScopes", [])}
        if not {"read_themes", "write_themes"}.issubset(scopes): return {"status": "MANUAL_ACTION_REQUIRED", "write_performed": False}
        current_theme = client.execute(FEATURED_THEME_FILE_QUERY, {"id": row["theme_id"], "filenames": [row["filename"]]}).get("theme") or {}
        try: current_raw, current_doc = self._remote_document(current_theme, row["filename"])
        except (ValueError, ShopifyJsonDocumentError): return {"status": "CONFLICT", "reason": "Current template is unreadable", "write_performed": False}
        if current_theme.get("id") != row["theme_id"] or current_theme.get("role") != "MAIN" or current_raw != proposed_raw:
            return {"status": "CONFLICT", "reason": "Merchant changes detected; rollback will not overwrite them", "write_performed": False}
        from .homepage_automation import UPSERT_THEME_FILES
        result = client.execute(UPSERT_THEME_FILES, {"themeId": row["theme_id"], "files": [{"filename": row["filename"], "body": {"type": "TEXT", "value": before_raw}}]}).get("themeFilesUpsert") or {}
        if result.get("userErrors"): return {"status": "FAILED", "backup_id": backup_id, "write_performed": True}
        restored_theme = client.execute(FEATURED_THEME_FILE_QUERY, {"id": row["theme_id"], "filenames": [row["filename"]]}).get("theme") or {}
        try: restored_raw, _ = self._remote_document(restored_theme, row["filename"])
        except (ValueError, ShopifyJsonDocumentError): restored_raw = None
        verified = restored_theme.get("role") == "MAIN" and restored_theme.get("id") == row["theme_id"] and restored_raw == before_raw
        with connect(self.db) as con:
            con.execute("UPDATE homepage_featured_theme_backups SET status=? WHERE backup_id=?", ("ROLLED_BACK" if verified else "ROLLBACK_VERIFY_FAILED", backup_id))
            if verified: con.execute("UPDATE homepage_featured_product_plans SET status='READY',remote_verified=0,remote_json_verified=0,storefront_verified=0,updated_at=? WHERE plan_id=?", (_now(), row["plan_id"]))
        return {"status": "ROLLED_BACK" if verified else "ROLLBACK_VERIFY_FAILED", "backup_id": backup_id, "write_performed": True}
