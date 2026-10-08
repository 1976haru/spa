"""Local-first featured-product planning for homepage assignments.

The service references MASTER and Shopify mapping rows, produces guarded theme
previews and reports, and deliberately contains no remote-write client.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .db import connect, init_db
from .paths import EXPORT_DIR
from .shopify_products import _install_schema as install_product_schema

MODES = {"NEW_ARRIVALS", "BALANCED_CATEGORIES", "MANUAL_SELECTION"}
ELIGIBLE_DECISIONS = {"PRIMARY", "RESERVE_A", "RESERVE_B", "PRODUCTION_CANDIDATE"}
VERIFIED_MAPPING_STATES = {"SYNCED", "VERIFIED", "NO CHANGE", "NO_CHANGE"}


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
          remote_verified INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_featured_product_plan_store
          ON homepage_featured_product_plans(store_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS homepage_featured_product_items(
          plan_id TEXT NOT NULL,position INTEGER NOT NULL,master_product_id INTEGER NOT NULL,
          shopify_product_id TEXT NOT NULL,shopify_handle TEXT NOT NULL,title TEXT NOT NULL,
          category_key TEXT,image_url TEXT,price REAL,remote_status TEXT NOT NULL,
          selection_reason TEXT NOT NULL,verification_status TEXT NOT NULL,
          PRIMARY KEY(plan_id,position),UNIQUE(plan_id,shopify_product_id),UNIQUE(plan_id,shopify_handle));
        """)


def discover_featured_product_schema(section_files: dict[str, str]) -> dict:
    """Use only proven schema field types/ids; never infer from a theme name."""
    candidates = []
    for filename, raw in sorted(section_files.items()):
        match = re.search(r"\{%[- ]*schema[- ]*%\}(.*?)\{%[- ]*endschema[- ]*%\}", raw, re.S | re.I)
        if not match: continue
        try: schema = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, TypeError): continue
        settings = schema.get("settings") or []
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
        return {"status": "MANUAL_ACTION_REQUIRED", "confidence": "LOW", "reason": "No proven product-list or featured-collection schema"}
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
                       category_key=row.get("category") or (tags[0] if tags else "uncategorized"),
                       storefront_status=storefront, eligibility_reasons=reasons, eligible=not reasons,
                       product_link=f"/products/{row['shopify_handle']}" if row.get("shopify_handle") else None)
            result.append(row)
        return result

    def create_plan(self, store_id: str, *, mode="BALANCED_CATEGORIES", requested_count=4,
                    heading="New Arrivals", subheading="", manual_product_ids=None,
                    collection_key=None, collection_handle=None) -> dict:
        mode = str(mode).upper()
        if mode not in MODES: raise ValueError(f"Unsupported featured-product mode: {mode}")
        requested_count = max(1, int(requested_count)); candidates = self.eligible_products(store_id)
        eligible = [x for x in candidates if x["eligible"]]
        if mode == "MANUAL_SELECTION":
            wanted = [int(x) for x in (manual_product_ids or [])]
            by_id = {x["master_product_id"]: x for x in eligible}
            selected = [by_id[x] for x in wanted if x in by_id][:requested_count]
        elif mode == "NEW_ARRIVALS":
            selected = sorted(eligible, key=lambda x: (x.get("synced_at") or "", x["master_product_id"]), reverse=True)[:requested_count]
        else:
            selected, seen = [], set()
            for row in eligible:
                key = str(row.get("category_key") or "uncategorized").casefold()
                if key not in seen: selected.append(row); seen.add(key)
                if len(selected) == requested_count: break
            if len(selected) < requested_count:
                selected.extend(x for x in eligible if x not in selected)
                selected = selected[:requested_count]
        ids, handles = [x["shopify_product_id"] for x in selected], [x["shopify_handle"].casefold() for x in selected]
        reasons = []
        if len(selected) < requested_count: reasons.append(f"ONLY_{len(selected)}_OF_{requested_count}_ELIGIBLE_PRODUCTS")
        if len(ids) != len(set(ids)): reasons.append("DUPLICATE_SHOPIFY_PRODUCT_ID")
        if len(handles) != len(set(handles)): reasons.append("DUPLICATE_SHOPIFY_HANDLE")
        status = "READY" if not reasons else "MANUAL_ACTION_REQUIRED"
        plan_id, now = "HFP_" + secrets.token_hex(10), _now()
        fingerprint = {"store_id": store_id, "mode": mode, "requested_count": requested_count,
                       "selected": [(x["master_product_id"], x["shopify_product_id"], x["shopify_handle"], x["synced_at"]) for x in selected]}
        with connect(self.db) as con:
            con.execute("INSERT INTO homepage_featured_product_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                        (plan_id, store_id, mode, heading, subheading, requested_count, collection_key,
                         collection_handle, status, _hash(fingerprint), None, now, now))
            for position, row in enumerate(selected, 1):
                why = "manual order" if mode == "MANUAL_SELECTION" else "recently synced" if mode == "NEW_ARRIVALS" else f"category diversity: {row['category_key']}"
                con.execute("INSERT INTO homepage_featured_product_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (plan_id, position, row["master_product_id"], row["shopify_product_id"], row["shopify_handle"],
                             row["title"], row["category_key"], row["image_url"], row["price"], row["storefront_status"],
                             why, "LOCAL_ELIGIBILITY_VERIFIED"))
        return self.get_plan(plan_id, reasons=reasons)

    def get_plan(self, plan_id: str, *, reasons=None) -> dict:
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM homepage_featured_product_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if not row: raise KeyError(plan_id)
            items = [dict(x) for x in con.execute("SELECT * FROM homepage_featured_product_items WHERE plan_id=? ORDER BY position", (plan_id,))]
        return {**dict(row), "items": items, "reasons": list(reasons or []), "write_performed": False}

    def checklist(self, plan: dict, *, section_visible=False, remote_verified=False) -> dict:
        items, required = plan.get("items", []), int(plan.get("requested_count", 4))
        checks = {"featured_products_count": len(items) >= required,
                  "featured_products_unique": len({x.get('shopify_product_id') for x in items}) == len(items) and len({x.get('shopify_handle') for x in items}) == len(items),
                  "featured_products_real_remote_ids": bool(items) and all(str(x.get("shopify_product_id", "")).startswith("gid://shopify/Product/") for x in items),
                  "featured_products_real_links": bool(items) and all(x.get("shopify_handle") for x in items),
                  "featured_products_price_valid": bool(items) and all(float(x.get("price") or 0) > 0 for x in items),
                  "featured_products_images_ready": bool(items) and all(x.get("image_url") for x in items),
                  "featured_products_storefront_eligible": bool(items) and all(x.get("remote_status") == "ACTIVE" for x in items),
                  "featured_products_section_visible": bool(section_visible),
                  "featured_products_remote_verified": bool(remote_verified)}
        status = "ASSIGNMENT_READY" if all(checks.values()) else "REVIEW_REQUIRED"
        return {"status": status, "ready": status == "ASSIGNMENT_READY", "checks": checks}

    def build_theme_preview(self, plan: dict, snapshot: dict) -> dict:
        capability = discover_featured_product_schema(snapshot.get("theme_files") or {})
        if capability["status"] != "READY":
            return {"status": "MANUAL_ACTION_REQUIRED", "reason": capability["reason"], "instructions": [
                "Open Shopify Theme Editor or PageFly manually.", "Add a product grid or Featured collection section.",
                "Use the selected products in the saved ShopSource plan; do not activate DRAFT products automatically."], "write_performed": False}
        current = snapshot.get("template")
        if not isinstance(current, dict): return {"status": "BLOCKED", "reason": "Homepage JSON template unavailable", "write_performed": False}
        proposed = json.loads(json.dumps(current)); sections = proposed.setdefault("sections", {}); order = proposed.setdefault("order", [])
        section_id = "ss_featured_products_" + hashlib.sha1(plan["store_id"].encode()).hexdigest()[:8]
        settings = {}
        if capability.get("heading_field"): settings[capability["heading_field"]["id"]] = plan["heading"]
        if capability.get("subheading_field") and plan.get("subheading"): settings[capability["subheading_field"]["id"]] = plan["subheading"]
        if capability["mode"] == "DIRECT_PRODUCTS": settings[capability["product_field"]["id"]] = [x["shopify_product_id"] for x in plan["items"]]
        elif plan.get("collection_handle"): settings[capability["collection_field"]["id"]] = plan["collection_handle"]
        else: return {"status": "MANUAL_ACTION_REQUIRED", "reason": "Managed New Arrivals collection mapping required", "fallback": {
            "collection_key": plan.get("collection_key") or "homepage-new-arrivals", "title": plan["heading"],
            "owned_tag": "shopsource:homepage:featured-products", "preserve_merchant_tags": True}, "write_performed": False}
        before = sections.get(section_id); desired = {"type": capability["type"], "settings": settings}
        sections[section_id] = desired
        if section_id not in order:
            footer = next((i for i, x in enumerate(order) if "footer" in str(x).casefold()), len(order)); order.insert(footer, section_id)
        preview_hash = _hash({"source": plan["source_hash"], "theme": snapshot.get("theme"), "current": current, "proposed": proposed})
        with connect(self.db) as con: con.execute("UPDATE homepage_featured_product_plans SET preview_hash=?,updated_at=? WHERE plan_id=?", (preview_hash, _now(), plan["plan_id"]))
        return {"status": "PREVIEW", "plan_id": plan["plan_id"], "capability": capability, "current": current,
                "proposed": proposed, "section_id": section_id, "action": "NO_CHANGE" if before == desired else "UPDATE" if before else "CREATE",
                "preview_hash": preview_hash, "write_performed": False, "unrelated_sections_preserved": True}

    def verify_remote(self, plan_id: str, *, preview_hash: str, remote_section: dict | None) -> dict:
        plan = self.get_plan(plan_id)
        if not plan.get("preview_hash") or plan["preview_hash"] != preview_hash:
            return {"status": "CONFLICT", "reason": "STALE_PREVIEW", "write_performed": False}
        visible = bool(remote_section and remote_section.get("visible", True))
        remote_ids = list((remote_section or {}).get("product_ids") or [])
        expected = [x["shopify_product_id"] for x in plan["items"]]
        verified = visible and remote_ids == expected
        if verified:
            with connect(self.db) as con: con.execute("UPDATE homepage_featured_product_plans SET status='VERIFIED',remote_verified=1,updated_at=? WHERE plan_id=?", (_now(), plan_id))
        return {"status": "VERIFIED" if verified else "REVIEW_REQUIRED", "section_visible": visible,
                "product_ids_match": remote_ids == expected, "write_performed": False}

    def export_report(self, plan_id: str) -> Path:
        plan = self.get_plan(plan_id); folder = self.export_dir / "homepage_reports" / plan["store_id"] / plan_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "featured_products.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# Featured Products assignment", "", f"- Store: {plan['store_id']}", f"- Heading: {plan['heading']}",
                 f"- Section status: {plan['status']}", "", "## Selected products", ""]
        for item in plan["items"]: lines.append(f"- {item['position']}. {item['title']} — {item['selection_reason']} — /products/{item['shopify_handle']}")
        lines += ["", "## Screenshot checklist", "", "- Desktop section and product links", "- Mobile crop and cards", "- Price and image visibility", "",
                  "## 제출 메모", "", "실제 검증된 상품만 사용해 추천 상품 영역을 구성했습니다. 선택된 카테고리와 링크는 위 목록을 기준으로 확인합니다."]
        (folder / "assignment_featured_products.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return folder
