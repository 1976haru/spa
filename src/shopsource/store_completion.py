"""Phase 4.0 store-completion planning and launch-readiness gates.

The module is deliberately read-only with respect to remote systems.  Callers
provide an inspected/synthetic snapshot; existing Phase 3.x database rows are
referenced by ID instead of copied into completion plans.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, TypedDict

from .db import connect, init_db
from .paths import EXPORT_DIR

DOMAINS = (
    "PRODUCTS", "COLLECTIONS", "PRICING", "INVENTORY_POLICY", "BRAND",
    "NAVIGATION", "HOMEPAGE", "PRODUCT_TEMPLATE", "COLLECTION_TEMPLATE",
    "SEARCH", "CART", "FOOTER", "STATIC_PAGES", "POLICIES", "CONTACT_SUPPORT",
    "SEO", "ACCESSIBILITY", "MOBILE", "BROKEN_LINKS", "MEDIA_QUALITY",
    "MARKETS_CURRENCY", "DOMAIN_SSL", "SHIPPING", "TAX", "PAYMENT", "CHECKOUT",
    "ANALYTICS", "LAUNCH_STATE", "FINAL_VERIFICATION",
)
FINAL_DOMAIN_STATUSES = {"VERIFIED", "MANUAL_ACTION_REQUIRED", "BLOCKED"}
REQUIRED_BLOCKERS = {"PRODUCTS", "NAVIGATION", "SEARCH", "CART", "POLICIES", "SHIPPING", "PAYMENT", "CHECKOUT"}
AUTO_SAFE_AREAS = {"FOOTER", "STATIC_PAGES", "SEO", "ACCESSIBILITY", "MOBILE", "BROKEN_LINKS", "MEDIA_QUALITY"}
WEIGHTS = {
    "PRODUCTS": 8, "COLLECTIONS": 4, "PRICING": 4, "INVENTORY_POLICY": 3, "BRAND": 6,
    "NAVIGATION": 6, "HOMEPAGE": 5, "PRODUCT_TEMPLATE": 5, "COLLECTION_TEMPLATE": 4,
    "SEARCH": 3, "CART": 4, "FOOTER": 2, "STATIC_PAGES": 3, "POLICIES": 5,
    "CONTACT_SUPPORT": 2, "SEO": 4, "ACCESSIBILITY": 3, "MOBILE": 3,
    "BROKEN_LINKS": 4, "MEDIA_QUALITY": 3, "MARKETS_CURRENCY": 3, "DOMAIN_SSL": 2,
    "SHIPPING": 6, "TAX": 2, "PAYMENT": 6, "CHECKOUT": 5, "ANALYTICS": 1,
    "LAUNCH_STATE": 2, "FINAL_VERIFICATION": 1,
}


class StoreCompletionPlan(TypedDict, total=False):
    plan_id: str
    store_id: str
    status: str
    readiness_score: int
    blocking_count: int
    warning_count: int
    references: dict
    summary: dict
    items: list[dict]


class ProductTemplatePlan(TypedDict, total=False):
    status: str
    checks: dict[str, bool]
    missing_core: list[str]


class FooterPlan(TypedDict, total=False):
    existing: list[dict]
    proposed: list[dict]
    actions: list[dict]
    merchant_links_preserved: bool


class PagePlan(TypedDict, total=False):
    pages: list[dict]
    unknown_business_fields: list[str]
    legal_disclaimer: str


class StoreSEOPlan(TypedDict, total=False):
    current: dict
    proposed: dict
    actions: list[dict]
    warnings: list[str]


class MobileReadinessPlan(TypedDict, total=False):
    status: str
    checks: dict[str, bool]
    browser_device_test: str


class CheckoutReadiness(TypedDict, total=False):
    status: str
    checks: dict[str, bool]
    order_or_payment_performed: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=lambda item: sorted(item) if isinstance(item, (set, frozenset)) else str(item))


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _install(db=None) -> None:
    init_db(db)
    with connect(db) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS store_completion_plans(
          plan_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,planner_version TEXT NOT NULL,
          source_hash TEXT NOT NULL,status TEXT NOT NULL,readiness_score INTEGER NOT NULL,
          blocking_count INTEGER NOT NULL,warning_count INTEGER NOT NULL,
          references_json TEXT NOT NULL DEFAULT '{}',summary_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_completion_store ON store_completion_plans(store_id,created_at);
        CREATE TABLE IF NOT EXISTS store_completion_items(
          id TEXT PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES store_completion_plans(plan_id) ON DELETE CASCADE,
          area TEXT NOT NULL,item_key TEXT NOT NULL,title TEXT NOT NULL,required_level TEXT NOT NULL,
          automation_mode TEXT NOT NULL,status TEXT NOT NULL,source_component TEXT NOT NULL,
          remote_reference TEXT,details_json TEXT NOT NULL DEFAULT '{}',blocking_reason TEXT,
          updated_at TEXT NOT NULL,UNIQUE(plan_id,area,item_key)
        );
        """)


def _schemas(theme_files: dict[str, str]) -> list[tuple[str, dict]]:
    found = []
    for filename, text in (theme_files or {}).items():
        match = re.search(r"{%\s*schema\s*%}(.*?){%\s*endschema\s*%}", text or "", re.S)
        if not match:
            continue
        try:
            found.append((filename, json.loads(match.group(1))))
        except json.JSONDecodeError:
            continue
    return found


def _theme_terms(theme_files: dict[str, str]) -> str:
    parts = []
    for filename, schema in _schemas(theme_files):
        parts.append(filename)
        parts.append(_json(schema))
    parts.extend((theme_files or {}).keys())
    return " ".join(parts).casefold().replace("_", "-")


def inspect_product_template(theme_files: dict[str, str]) -> dict:
    terms = _theme_terms(theme_files)
    checks = {
        "title": ("product-title", "title"), "media": ("product-media", "gallery", "media"),
        "price": ("price",), "variant_selector": ("variant", "product-form"),
        "quantity": ("quantity",), "add_to_cart": ("add-to-cart", "buy-buttons", "product-form"),
        "description": ("description",), "vendor": ("vendor",), "availability": ("inventory", "availability"),
        "related_products": ("related-products", "recommendations", "complementary"),
        "collapsible_rows": ("collapsible", "accordion", "tab"),
    }
    present = {key: any(term in terms for term in aliases) for key, aliases in checks.items()}
    core = ("title", "media", "price", "variant_selector", "add_to_cart", "description")
    missing = [key for key in core if not present[key]]
    return {"status": "PRESENT" if not missing else "MISSING_SECTION", "checks": present,
            "missing_core": missing, "recommendations": [key for key in ("vendor", "availability", "related_products", "collapsible_rows") if not present[key]],
            "prohibited_content_generated": False}


def inspect_collection_template(theme_files: dict[str, str], links: Iterable[dict] = ()) -> dict:
    terms = _theme_terms(theme_files)
    checks = {key: any(term in terms for term in aliases) for key, aliases in {
        "title": ("collection-title", "title"), "description": ("collection-description", "description"),
        "image": ("collection-image", "banner"), "product_grid": ("product-grid", "main-collection", "grid"),
        "sort": ("sort",), "filter": ("filter", "facets"), "pagination": ("pagination", "paginate"),
        "mobile_grid": ("columns-mobile", "mobile-columns", "mobile"), "empty_state": ("empty", "no-products"),
    }.items()}
    required = ("title", "product_grid", "pagination", "empty_state")
    missing = [key for key in required if not checks[key]]
    broken = [row for row in links if row.get("kind") == "collection" and not row.get("exists", False)]
    return {"status": "PRESENT" if not missing and not broken else "MISSING_SECTION",
            "checks": checks, "missing_core": missing, "broken_collection_links": broken,
            "filtering": "READY" if checks["filter"] else "MANUAL_ACTION_REQUIRED"}


def inspect_search(theme_files: dict[str, str], routes: Iterable[str] = ()) -> dict:
    terms, routes = _theme_terms(theme_files), set(routes or ())
    checks = {
        "route": "/search" in routes or "templates/search" in terms,
        "header_search": any(x in terms for x in ("header-search", "search-modal", "search-icon")),
        "template": "search" in terms,
        "empty_results": any(x in terms for x in ("no-results", "empty", "results_count")),
        "product_results": any(x in terms for x in ("product-card", "card-product", "product-grid")),
        "predictive_search": "predictive-search" in terms,
    }
    basic = all(checks[key] for key in ("route", "header_search", "template", "empty_results", "product_results"))
    return {"status": "VERIFIED" if basic else "BLOCKED", "checks": checks,
            "predictive_search_required": False, "warnings": [] if checks["predictive_search"] else ["Predictive search is optional"]}


def inspect_cart(theme_files: dict[str, str], routes: Iterable[str] = ()) -> dict:
    terms, routes = _theme_terms(theme_files), set(routes or ())
    checks = {
        "add_to_cart": any(x in terms for x in ("add-to-cart", "buy-buttons", "product-form")),
        "cart_surface": "/cart" in routes or any(x in terms for x in ("cart-drawer", "main-cart", "cart-items")),
        "quantity": "quantity" in terms, "remove": any(x in terms for x in ("cart-remove", "remove")),
        "subtotal": any(x in terms for x in ("subtotal", "totals")), "checkout_button": "checkout" in terms,
        "empty_cart": any(x in terms for x in ("cart-empty", "empty")), "mobile": "mobile" in terms or "cart-drawer" in terms,
    }
    return {"status": "VERIFIED" if all(checks.values()) else "BLOCKED", "checks": checks,
            "checkout_performed": False}


UNKNOWN_BUSINESS_FIELDS = (
    "support_email", "return_window", "return_address", "processing_time", "shipping_time",
    "shipping_fee", "company_legal_name", "company_address", "phone", "governing_law",
)
PAGE_TYPES = ("ABOUT_US", "CONTACT", "FAQ", "SHIPPING", "RETURNS", "PRIVACY", "TERMS", "REFUND_POLICY")


class ContentPageService:
    """Builds local drafts only.  It never writes Shopify pages or policies."""
    def build_plan(self, brand: dict | None, business: dict | None = None, existing: dict | None = None) -> dict:
        brand, business, existing = brand or {}, business or {}, existing or {}
        brand_name = brand.get("brand_name") or brand.get("store_name") or "Your store"
        missing = [field for field in UNKNOWN_BUSINESS_FIELDS if not business.get(field)]
        pages = []
        for page_type in PAGE_TYPES:
            known = {key: business[key] for key in UNKNOWN_BUSINESS_FIELDS if business.get(key)}
            needs = []
            if page_type == "CONTACT": needs = [x for x in ("support_email",) if not business.get(x)]
            elif page_type in {"SHIPPING"}: needs = [x for x in ("processing_time", "shipping_time", "shipping_fee") if not business.get(x)]
            elif page_type in {"RETURNS", "REFUND_POLICY"}: needs = [x for x in ("return_window", "return_address") if not business.get(x)]
            elif page_type in {"PRIVACY", "TERMS"}: needs = [x for x in ("company_legal_name", "company_address", "governing_law") if not business.get(x)]
            status = "CONFIGURED" if page_type in existing else ("REQUIRES_BUSINESS_INPUT" if needs else "DRAFT")
            content = {"heading": page_type.replace("_", " ").title(), "brand_name": brand_name,
                       "known_facts": known, "body": f"Draft content plan for {brand_name}."}
            if page_type == "CONTACT": content["contact_form"] = True
            if page_type == "FAQ":
                content["topics"] = [{"topic": topic, "answer_status": "NEEDS_BUSINESS_INPUT"}
                                     for topic in ("ordering", "shipping", "returns", "product questions", "contact")]
            pages.append({"page_type": page_type, "status": status, "requires_business_input": needs,
                          "content": content, "existing_reference": existing.get(page_type)})
        return {"pages": pages, "unknown_business_fields": missing, "live_write": False,
                "legal_disclaimer": "Drafts require merchant and, where appropriate, legal review."}


def build_footer_plan(existing_links: list[dict], page_plan: dict, collections: list[dict]) -> dict:
    destinations = {p["page_type"]: p for p in page_plan.get("pages", []) if p["status"] in {"CONFIGURED", "DRAFT"}}
    proposed = list(existing_links or [])
    wanted = {
        "Customer Care": (("Contact Us", "CONTACT"), ("FAQ", "FAQ"), ("Shipping", "SHIPPING"), ("Returns", "RETURNS")),
        "Company": (("About Us", "ABOUT_US"),),
        "Legal": (("Privacy Policy", "PRIVACY"), ("Terms of Service", "TERMS"), ("Refund / Return Policy", "REFUND_POLICY")),
    }
    actions = []
    existing_targets = {row.get("target") for row in proposed}
    for group, rows in wanted.items():
        for title, key in rows:
            page = destinations.get(key)
            if not page:
                actions.append({"action": "SKIP", "title": title, "reason": "Destination does not exist or is not safely planned"})
                continue
            target = page.get("existing_reference") or f"planned:{key}"
            if target not in existing_targets:
                proposed.append({"group": group, "title": title, "target": target, "managed": True})
                actions.append({"action": "CREATE", "title": title, "target": target})
                existing_targets.add(target)
    ready_collections = [row for row in collections or [] if row.get("handle")]
    if ready_collections and not any(row.get("group") == "Shop" for row in proposed):
        proposed.append({"group": "Shop", "title": "Shop", "target": "/collections", "managed": True})
        actions.append({"action": "CREATE", "title": "Shop", "target": "/collections"})
    return {"existing": existing_links or [], "proposed": proposed, "actions": actions,
            "merchant_links_preserved": all(row in proposed for row in existing_links or []), "live_write": False}


def build_seo_plan(current: dict | None, suggestions: dict | None = None) -> dict:
    current, suggestions = current or {}, suggestions or {}
    proposed, actions = dict(current), []
    for key, value in suggestions.items():
        if current.get(key) and not current.get(f"{key}_managed", False):
            actions.append({"field": key, "action": "PRESERVE_UNMANAGED"})
        elif value:
            proposed[key] = value
            actions.append({"field": key, "action": "PROPOSE"})
    warnings = []
    if not current.get("social_image") and not proposed.get("social_image"): warnings.append("Social sharing image missing")
    if current.get("duplicate_handles"): warnings.append("Duplicate handle detected")
    return {"current": current, "proposed": proposed, "actions": actions, "warnings": warnings,
            "rules": ["No keyword stuffing", "No fabricated claims", "No unsupported location claims"]}


def inspect_accessibility(snapshot: dict) -> dict:
    issues = []
    for image in snapshot.get("images", []):
        if not image.get("alt"): issues.append({"code": "MISSING_ALT", "reference": image.get("reference")})
    for control in snapshot.get("controls", []):
        if control.get("icon_only") and not control.get("accessible_label"): issues.append({"code": "MISSING_CONTROL_LABEL"})
    issues.extend({"code": code} for code in snapshot.get("heading_issues", []))
    status = "WARNINGS" if issues else "BASIC_CHECK_PASS"
    return {"status": status, "issues": issues, "manual_audit": "MANUAL_AUDIT_REQUIRED",
            "disclaimer": "Automated heuristics do not establish WCAG compliance."}


def inspect_mobile(snapshot: dict) -> dict:
    checks = {key: bool(snapshot.get(key)) for key in (
        "hero_mobile_crop", "logo_width", "mobile_navigation", "collection_columns", "product_media",
        "text_overflow_review", "cta_length_review", "category_shortcuts", "footer_stacking",
    )}
    return {"status": "VERIFIED" if all(checks.values()) else "MANUAL_ACTION_REQUIRED", "checks": checks,
            "browser_device_test": "DEFERRED_TO_PHASE_4_1"}


def inspect_media(assets: list[dict], *, rights_policy_blocks=False) -> dict:
    warnings, blockers = [], []
    for asset in assets or []:
        if not asset.get("reference"): warnings.append({"code": "MISSING_IMAGE", "asset": asset.get("id")})
        if asset.get("local") and not asset.get("exists", False): warnings.append({"code": "BROKEN_REFERENCE", "asset": asset.get("id")})
        if not asset.get("alt"): warnings.append({"code": "MISSING_ALT", "asset": asset.get("id")})
        rights = asset.get("rights_status") or "RIGHTS_REVIEW_REQUIRED"
        if rights not in {"RIGHTS_CONFIRMED", "RIGHTS_REVIEW_REQUIRED", "GENERATED", "MANUAL"}: rights = "RIGHTS_REVIEW_REQUIRED"
        asset["rights_status"] = rights
        if rights == "RIGHTS_REVIEW_REQUIRED" and rights_policy_blocks: blockers.append(asset.get("id"))
    return {"status": "BLOCKED" if blockers else ("MANUAL_ACTION_REQUIRED" if warnings else "VERIFIED"),
            "assets": assets or [], "warnings": warnings, "rights_blockers": blockers}


class StoreLinkInspector:
    def inspect(self, links: list[dict], resources: dict[str, set[str]] | None = None) -> dict:
        resources, issues, seen = resources or {}, [], {}
        for link in links or []:
            target, kind = str(link.get("target") or "").strip(), link.get("kind", "page")
            key = link.get("key") or link.get("title") or target
            if not target or target == "#" or "placeholder" in target.casefold():
                issues.append({"code": "PLACEHOLDER", "key": key, "target": target}); continue
            if link.get("stale_handle"): issues.append({"code": "STALE_HANDLE", "key": key, "target": target})
            if link.get("remote_required") and not link.get("remote_id"): issues.append({"code": "MISSING_REMOTE_ID", "key": key})
            expected = resources.get(kind)
            if expected is not None and target not in expected: issues.append({"code": "MISSING_TARGET", "key": key, "target": target})
            if key in seen and seen[key] != target: issues.append({"code": "DUPLICATE_WRONG_TARGET", "key": key, "targets": [seen[key], target]})
            seen[key] = target
        return {"status": "BLOCKED" if issues else "VERIFIED", "issues": issues, "checked": len(links or [])}


def inspect_commerce(snapshot: dict) -> dict:
    target = snapshot.get("target_market") or "US"
    primary = snapshot.get("primary_market")
    market = {"status": "MATCH" if primary == target and snapshot.get("store_currency") else "NEEDS_REVIEW",
              "target_market": target, "primary_market": primary, "store_currency": snapshot.get("store_currency"),
              "presentment_currencies": snapshot.get("presentment_currencies", [])}
    domain_value = snapshot.get("primary_domain") or snapshot.get("myshopify_domain")
    domain = {"status": "VERIFIED" if domain_value and snapshot.get("published_theme") else "MANUAL_ACTION_REQUIRED",
              "domain": domain_value, "custom_domain_recommended": bool(snapshot.get("myshopify_domain") and not snapshot.get("primary_domain")),
              "ssl": snapshot.get("ssl_state", "UNKNOWN"), "published_theme": bool(snapshot.get("published_theme"))}
    shipping_ok = bool(snapshot.get("shipping_origin") and target in snapshot.get("shipping_markets", []) and snapshot.get("shipping_rates"))
    shipping = {"status": "VERIFIED" if shipping_ok else "BLOCKED", "target_market": target,
                "origin": snapshot.get("shipping_origin"), "applicable_rates": snapshot.get("shipping_rates", []),
                "content_config_match": snapshot.get("shipping_content_config_match", True)}
    if snapshot.get("shipping_content_config_match") is False:
        shipping.update(status="BLOCKED", reason="Shipping page content conflicts with configured rates")
    tax_value = snapshot.get("tax_status", "UNKNOWN")
    tax = {"status": "VERIFIED" if tax_value == "CONFIGURED" else "MANUAL_ACTION_REQUIRED", "tax_status": tax_value,
           "disclaimer": "Tax readiness is not tax or legal advice."}
    payment = {"status": "VERIFIED" if snapshot.get("payment_confirmed") else "BLOCKED",
               "confirmed": bool(snapshot.get("payment_confirmed")), "methods": snapshot.get("payment_methods", []),
               "real_payment_performed": False}
    checkout_checks = {"cart_to_checkout": bool(snapshot.get("cart_to_checkout")), "shipping": shipping["status"] == "VERIFIED",
                       "payment": payment["status"] == "VERIFIED", "market_currency": market["status"] == "MATCH",
                       "store_access": not bool(snapshot.get("launch_blocked"))}
    checkout = {"status": "VERIFIED" if all(checkout_checks.values()) else "BLOCKED", "checks": checkout_checks,
                "order_or_payment_performed": False}
    return {"markets": market, "domain": domain, "shipping": shipping, "tax": tax, "payment": payment, "checkout": checkout}


def inspect_brand_consistency(brand_name: str | None, values: dict[str, str]) -> dict:
    stale = []
    if brand_name:
        for component, text in (values or {}).items():
            if text and brand_name.casefold() not in text.casefold(): stale.append({"component": component, "observed": text, "expected": brand_name})
    return {"status": "VERIFIED" if not stale else "MANUAL_ACTION_REQUIRED", "diff": stale, "auto_overwrite": False}


class StoreCompletionService:
    planner_version = "4.0"

    def __init__(self, *, db=None, export_dir: str | Path | None = None):
        self.db = db
        self.export_dir = Path(export_dir) if export_dir else EXPORT_DIR
        _install(db)

    def _references(self, store_id: str) -> dict:
        queries = {
            "brand_profile_id": ("brand_profiles", "profile_id"), "collection_plan_id": ("collection_plans", "plan_id"),
            "navigation_plan_id": ("navigation_plans", "plan_id"), "homepage_plan_id": ("store_homepage_plans", "plan_id"),
            "product_sync_run_id": ("shopify_product_sync_runs", "run_id"),
        }
        refs = {}
        with connect(self.db) as con:
            for key, (table, column) in queries.items():
                if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
                if "store_id" not in columns or column not in columns: continue
                row = con.execute(f"SELECT {column} AS value FROM {table} WHERE store_id=? ORDER BY rowid DESC LIMIT 1", (store_id,)).fetchone()
                if row: refs[key] = row["value"]
        return refs

    def inspect(self, store_id: str, snapshot: dict | None = None) -> dict:
        snapshot = dict(snapshot or {})
        theme_files, routes = snapshot.get("theme_files", {}), snapshot.get("routes", [])
        product_template = inspect_product_template(theme_files)
        collection_template = inspect_collection_template(theme_files, snapshot.get("links", []))
        search, cart = inspect_search(theme_files, routes), inspect_cart(theme_files, routes)
        page_plan = ContentPageService().build_plan(snapshot.get("brand"), snapshot.get("business"), snapshot.get("pages"))
        footer = build_footer_plan(snapshot.get("footer_links", []), page_plan, snapshot.get("collections", []))
        links = StoreLinkInspector().inspect(snapshot.get("links", []), snapshot.get("resources"))
        commerce = inspect_commerce(snapshot)
        accessibility = inspect_accessibility(snapshot.get("accessibility", {}))
        mobile = inspect_mobile(snapshot.get("mobile", {}))
        media = inspect_media(snapshot.get("media", []), rights_policy_blocks=bool(snapshot.get("rights_policy_blocks")))
        seo = build_seo_plan(snapshot.get("seo"), snapshot.get("seo_suggestions"))
        brand_name = (snapshot.get("brand") or {}).get("brand_name") or (snapshot.get("brand") or {}).get("store_name")
        consistency = inspect_brand_consistency(brand_name, snapshot.get("brand_references", {}))
        counts = self._counts(store_id)
        policies = {}
        for page in page_plan["pages"]:
            if page["page_type"] not in {"PRIVACY", "TERMS", "REFUND_POLICY", "SHIPPING"}: continue
            if page["status"] == "CONFIGURED": policies[page["page_type"]] = "CONFIGURED"
            elif page["requires_business_input"]: policies[page["page_type"]] = "NEEDS_REVIEW"
            else: policies[page["page_type"]] = "DRAFT"
        policy_blocked = any(value not in {"CONFIGURED", "VERIFIED"} for value in policies.values())
        def final(ok=False, manual=False, reason=None, details=None):
            status = "VERIFIED" if ok else ("MANUAL_ACTION_REQUIRED" if manual else "BLOCKED")
            return {"status": status, "blocking_reason": reason, "details": details or {}}
        items = {
            "PRODUCTS": final(counts["products"] > 0, reason="No usable product path", details={"count": counts["products"]}),
            "COLLECTIONS": final(counts["collections"] > 0 or bool(snapshot.get("collections")), manual=True, details={"count": counts["collections"]}),
            "PRICING": final(bool(snapshot.get("pricing_ready")), manual=True),
            "INVENTORY_POLICY": final(bool(snapshot.get("inventory_policy_ready")), manual=True),
            "BRAND": final(bool(self._references(store_id).get("brand_profile_id") or snapshot.get("brand")), manual=True, details=consistency),
            "NAVIGATION": final(bool(snapshot.get("navigation_ready")) and links["status"] == "VERIFIED", reason="Broken or unverified main navigation", details=links),
            "HOMEPAGE": final(bool(snapshot.get("homepage_ready")), manual=True),
            "PRODUCT_TEMPLATE": final(product_template["status"] == "PRESENT", manual=True, details=product_template),
            "COLLECTION_TEMPLATE": final(collection_template["status"] == "PRESENT", manual=True, details=collection_template),
            "SEARCH": final(search["status"] == "VERIFIED", reason="Basic storefront search is incomplete", details=search),
            "CART": final(cart["status"] == "VERIFIED", reason="Cart core controls are incomplete", details=cart),
            "FOOTER": final(bool(snapshot.get("footer_ready")), manual=True, details=footer),
            "STATIC_PAGES": final(all(p["status"] == "CONFIGURED" for p in page_plan["pages"] if p["page_type"] in {"ABOUT_US", "CONTACT", "FAQ"}), manual=True, details=page_plan),
            "POLICIES": final(not policy_blocked, reason="Critical policies require merchant review", details={"policies": policies, "legal_certified": False}),
            "CONTACT_SUPPORT": final(any(p["page_type"] == "CONTACT" and p["status"] == "CONFIGURED" for p in page_plan["pages"]), manual=True),
            "SEO": final(not seo["warnings"], manual=True, details=seo),
            "ACCESSIBILITY": final(accessibility["status"] == "BASIC_CHECK_PASS", manual=True, details=accessibility),
            "MOBILE": final(mobile["status"] == "VERIFIED", manual=True, details=mobile),
            "BROKEN_LINKS": final(links["status"] == "VERIFIED", reason="Broken internal links found", details=links),
            "MEDIA_QUALITY": final(media["status"] == "VERIFIED", manual=media["status"] != "BLOCKED", reason="Media rights policy blocker", details=media),
            "MARKETS_CURRENCY": final(commerce["markets"]["status"] == "MATCH", manual=True, details=commerce["markets"]),
            "DOMAIN_SSL": final(commerce["domain"]["status"] == "VERIFIED", manual=True, details=commerce["domain"]),
            "SHIPPING": final(commerce["shipping"]["status"] == "VERIFIED", reason=commerce["shipping"].get("reason") or "No shipping path for target market", details=commerce["shipping"]),
            "TAX": final(commerce["tax"]["status"] == "VERIFIED", manual=True, details=commerce["tax"]),
            "PAYMENT": final(commerce["payment"]["status"] == "VERIFIED", reason="No confirmed usable payment method", details=commerce["payment"]),
            "CHECKOUT": final(commerce["checkout"]["status"] == "VERIFIED", reason="Checkout prerequisites are incomplete", details=commerce["checkout"]),
            "ANALYTICS": final(bool(snapshot.get("shopify_analytics", True)), manual=True, details={"external_tracking_required": False}),
            "LAUNCH_STATE": final(bool(snapshot.get("published_theme")) and not bool(snapshot.get("launch_blocked")), manual=True),
            "FINAL_VERIFICATION": final(bool(snapshot.get("final_verification")), manual=True),
        }
        assert set(items) == set(DOMAINS)
        return {"store_id": store_id, "references": self._references(store_id), "items": items,
                "page_plan": page_plan, "footer_plan": footer, "commerce": commerce, "counts": counts,
                "target_market": snapshot.get("target_market") or "US", "snapshot_hash": _hash(snapshot)}

    def _counts(self, store_id: str) -> dict:
        with connect(self.db) as con:
            products = con.execute("SELECT COUNT(*) FROM store_product_decisions WHERE store_id=? AND final_status IN ('PRIMARY','RESERVE_A','RESERVE_B')", (store_id,)).fetchone()[0]
            collections = 0
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_plan_items'").fetchone():
                columns = {row["name"] for row in con.execute("PRAGMA table_info(collection_plan_items)")}
                if "store_id" in columns: collections = con.execute("SELECT COUNT(*) FROM collection_plan_items WHERE store_id=?", (store_id,)).fetchone()[0]
                elif con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_plans'").fetchone():
                    row = con.execute("SELECT plan_id FROM collection_plans WHERE store_id=? ORDER BY rowid DESC LIMIT 1", (store_id,)).fetchone()
                    if row and "plan_id" in columns: collections = con.execute("SELECT COUNT(*) FROM collection_plan_items WHERE plan_id=?", (row["plan_id"],)).fetchone()[0]
        return {"products": products, "collections": collections}

    def build_plan(self, store_id: str, snapshot: dict | None = None) -> dict:
        inspected = self.inspect(store_id, snapshot)
        items = inspected["items"]
        blockers = [area for area, item in items.items() if item["status"] == "BLOCKED"]
        manuals = [area for area, item in items.items() if item["status"] == "MANUAL_ACTION_REQUIRED"]
        score = round(100 * sum(WEIGHTS[a] for a, item in items.items() if item["status"] == "VERIFIED") / sum(WEIGHTS.values()))
        status = "NOT_READY" if blockers else ("READY_WITH_WARNINGS" if manuals else "READY")
        plan_id, now = "SCP_" + secrets.token_hex(10), _now()
        summary = {"status": status, "readiness_score": score, "blockers": blockers, "manual_actions": manuals,
                   "verified": [area for area, item in items.items() if item["status"] == "VERIFIED"],
                   "warnings": manuals, "target_market": inspected["target_market"], "counts": inspected["counts"],
                   "commerce": inspected["commerce"], "next_action": (f"Resolve blocker: {blockers[0]}" if blockers else (f"Review: {manuals[0]}" if manuals else "Launch readiness verified"))}
        summary["domains"] = {area: item["status"] for area, item in items.items()}
        summary.update({"brand": summary["domains"]["BRAND"], "navigation": summary["domains"]["NAVIGATION"],
                        "homepage": summary["domains"]["HOMEPAGE"], "policies": items["POLICIES"]["details"].get("policies", {}),
                        "shipping": inspected["commerce"]["shipping"]["status"], "payment": inspected["commerce"]["payment"]["status"],
                        "tax": inspected["commerce"]["tax"]["status"], "domain": inspected["commerce"]["domain"]["status"],
                        "checkout": inspected["commerce"]["checkout"]["status"]})
        with connect(self.db) as con:
            con.execute("INSERT INTO store_completion_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, store_id, self.planner_version, inspected["snapshot_hash"], status, score, len(blockers), len(manuals),
                         _json(inspected["references"]), _json(summary), now, now))
            for area, item in items.items():
                con.execute("INSERT INTO store_completion_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            ("SCI_" + secrets.token_hex(8), plan_id, area, area.casefold(), area.replace("_", " ").title(),
                             "REQUIRED" if area in REQUIRED_BLOCKERS else "RECOMMENDED",
                             "AUTO_SAFE" if area in AUTO_SAFE_AREAS else ("MANUAL_REQUIRED" if item["status"] == "MANUAL_ACTION_REQUIRED" else ("INSPECTION_ONLY" if item["status"] == "BLOCKED" else "AUTO_SAFE")),
                             item["status"], self._source_component(area), inspected["references"].get(self._reference_key(area)),
                             _json(item["details"]), item.get("blocking_reason"), now))
        return self.get_plan(plan_id)

    @staticmethod
    def _reference_key(area: str) -> str:
        return {"BRAND": "brand_profile_id", "COLLECTIONS": "collection_plan_id", "NAVIGATION": "navigation_plan_id",
                "HOMEPAGE": "homepage_plan_id", "PRODUCTS": "product_sync_run_id"}.get(area, "")

    @staticmethod
    def _source_component(area: str) -> str:
        return {"BRAND": "brand_automation", "COLLECTIONS": "collection_planner", "NAVIGATION": "navigation",
                "HOMEPAGE": "homepage_automation", "PRODUCTS": "shopify_products"}.get(area, "store_completion")

    def get_plan(self, plan_id: str) -> dict:
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM store_completion_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if not row: raise KeyError(plan_id)
            items = []
            for item in con.execute("SELECT * FROM store_completion_items WHERE plan_id=? ORDER BY rowid", (plan_id,)):
                value = dict(item); value["details"] = json.loads(value.pop("details_json") or "{}"); items.append(value)
        result = dict(row); result["references"] = json.loads(result.pop("references_json")); result["summary"] = json.loads(result.pop("summary_json")); result["items"] = items
        return result

    def preview_fixes(self, plan_id: str) -> dict:
        plan = self.get_plan(plan_id)
        fixes = [{"item_id": item["id"], "area": item["area"], "action": "PREVIEW_LOCAL_SAFE_FIX"}
                 for item in plan["items"] if item["automation_mode"] == "AUTO_SAFE" and item["status"] != "VERIFIED"]
        return {"plan_id": plan_id, "status": "DRY_RUN", "fixes": fixes, "remote_writes": 0}

    def apply_safe(self, plan_id: str, selected_items: Iterable[str]) -> dict:
        plan = self.get_plan(plan_id); selected = set(selected_items)
        applied, refused = [], []
        for item in plan["items"]:
            if item["id"] not in selected: continue
            if item["automation_mode"] != "AUTO_SAFE": refused.append({"item_id": item["id"], "reason": "Manual/remote action cannot be auto-applied"})
            else: applied.append({"item_id": item["id"], "action": "LOCAL_PLAN_UPDATED"})
        return {"status": "PREVIEW_ONLY", "applied": applied, "refused": refused, "remote_writes": 0}

    def verify(self, plan_id: str) -> dict:
        plan = self.get_plan(plan_id)
        return {"plan_id": plan_id, "status": plan["status"], "ready": plan["status"] == "READY",
                "blocker_count": plan["blocking_count"], "score": plan["readiness_score"]}

    def generate_report(self, plan_id: str, *, run_id: str | None = None) -> dict:
        plan = self.get_plan(plan_id); run_id = run_id or plan_id
        root = self.export_dir / "store_completion_reports" / re.sub(r"[^A-Za-z0-9_.-]", "_", plan["store_id"]) / re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
        root.mkdir(parents=True, exist_ok=True)
        summary = plan["summary"]
        blockers = [item for item in plan["items"] if item["status"] == "BLOCKED"]
        manuals = [item for item in plan["items"] if item["status"] == "MANUAL_ACTION_REQUIRED"]
        warnings = [{"area": item["area"], "details": item["details"]} for item in manuals]
        safe_summary = {**summary, "plan_id": plan_id, "store_id": plan["store_id"], "references": plan["references"]}
        (root / "readiness_summary.json").write_text(json.dumps(safe_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "blockers.json").write_text(json.dumps(blockers, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "warnings.json").write_text(json.dumps(warnings, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "manual_actions.md").write_text("# Manual actions\n\n" + "\n".join(f"- {x['area']}: {x.get('blocking_reason') or 'Review required'}" for x in manuals) + "\n", encoding="utf-8")
        (root / "readiness_summary.md").write_text(
            f"# Store completion\n\nScore: {plan['readiness_score']} / 100\n\nStatus: {plan['status']}\n\n"
            f"Blockers: {len(blockers)}\n\nNext action: {summary['next_action']}\n", encoding="utf-8")
        return {"status": "GENERATED", "folder": str(root), "files": sorted(p.name for p in root.iterdir())}
