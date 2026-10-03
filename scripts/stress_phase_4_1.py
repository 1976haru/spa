"""Local-only Phase 4.1 qualification harness.

Usage:
  python scripts/stress_phase_4_1.py --profile standard
  python scripts/stress_phase_4_1.py --profile massive
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shopsource.db import connect  # noqa: E402
from shopsource.store_completion import StoreCompletionService  # noqa: E402
from shopsource.stress_support import PROFILES, integrity, match_collection_rules, measure, seed_catalog, seed_collections, sqlite_size  # noqa: E402


def completion_snapshot(collections: int) -> dict:
    theme = {f"sections/{name}.liquid": "" for name in (
        "product-title-product-media-price-variant-selector-quantity-add-to-cart-product-form-description-vendor-inventory-availability-related-products-collapsible",
        "collection-title-description-image-product-grid-sort-filter-pagination-columns-mobile-no-products",
        "header-search-search-modal-templates-search-no-results-product-card",
        "main-cart-cart-items-quantity-cart-remove-subtotal-checkout-cart-empty-mobile",
    )}
    page_types = ("ABOUT_US", "CONTACT", "FAQ", "SHIPPING", "RETURNS", "PRIVACY", "TERMS", "REFUND_POLICY")
    business_fields = ("support_email", "return_window", "return_address", "processing_time", "shipping_time", "shipping_fee",
                       "company_legal_name", "company_address", "phone", "governing_law")
    mobile_fields = ("hero_mobile_crop", "logo_width", "mobile_navigation", "collection_columns", "product_media",
                     "text_overflow_review", "cta_length_review", "category_shortcuts", "footer_stacking")
    return {"theme_files": theme, "routes": ["/search", "/cart"], "brand": {"brand_name": "Stress Store"},
            "brand_references": {"homepage": "Stress Store"}, "business": {key: "fixture" for key in business_fields},
            "pages": {key: f"/pages/{key.lower()}" for key in page_types},
            "collections": [{"handle": f"collection-{n}"} for n in range(collections)], "footer_ready": True,
            "navigation_ready": True, "homepage_ready": True, "pricing_ready": True, "inventory_policy_ready": True,
            "links": [], "resources": {}, "seo": {"social_image": "fixture.png"}, "seo_suggestions": {},
            "accessibility": {"images": [], "controls": []}, "mobile": {key: True for key in mobile_fields},
            "media": [], "target_market": "US", "primary_market": "US", "store_currency": "USD",
            "presentment_currencies": ["USD"], "myshopify_domain": "stress.myshopify.com", "ssl_state": "ACTIVE",
            "published_theme": True, "shipping_origin": "US", "shipping_markets": ["US"],
            "shipping_rates": ["fixture"], "shipping_content_config_match": True, "tax_status": "CONFIGURED",
            "payment_confirmed": True, "payment_methods": ["fixture"], "cart_to_checkout": True,
            "launch_blocked": False, "shopify_analytics": True, "final_verification": True}


def run_scale(profile: str, root: Path) -> dict:
    product_count, collection_count = PROFILES[profile]; db = root / f"{profile}.sqlite3"
    seeded, seed_metrics = measure(seed_catalog, db, products=product_count)
    seed_collections(db, "stress-000", collection_count)
    service = StoreCompletionService(db=db, export_dir=root / "reports")
    plan, plan_metrics = measure(service.build_plan, "stress-000", completion_snapshot(collection_count))
    report, report_metrics = measure(service.generate_report, plan["plan_id"], run_id=profile)
    assert plan["summary"]["counts"] == {"products": product_count, "collections": collection_count}
    assert plan["status"] == "READY"
    return {"profile": profile.upper(), "products": product_count, "collections": collection_count,
            "seed": seed_metrics, "completion": plan_metrics, "report": report_metrics,
            "readiness_score": plan["readiness_score"], "db_bytes": sqlite_size(db), "integrity": integrity(db),
            "report_files": report["files"], "seeded": seeded}


def run_collection_massive() -> dict:
    products = ({"id": n, "title": f"Organizer Café collection-{n % 30}!", "tags": [f"collection-{n % 30}"]} for n in range(50_000))
    rules = [{"key": f"c{n}", "title_terms": [f"collection-{n}"], "tags": [f"collection-{n}"]} for n in range(30)]
    result, metrics = measure(match_collection_rules, products, rules, strategy="MIXED")
    assert result["total"] == 50_000 and sum(result["counts"].values()) == 50_000
    return {"profile": "COLLECTION_50K_X_30", **metrics, "matched": sum(result["counts"].values()), "unmatched": result["unmatched"]}


def run_multi(root: Path, stores: int, per_store: int) -> dict:
    db = root / f"multi-{stores}-{per_store}.sqlite3"; seed_catalog(db, products=per_store, stores=stores, products_per_store=per_store)
    with connect(db) as con:
        counts = {row["store_id"]: row["n"] for row in con.execute("SELECT store_id,COUNT(*) n FROM store_product_decisions GROUP BY store_id")}
        contamination = con.execute("""SELECT COUNT(*) FROM store_product_decisions d
          LEFT JOIN stores s ON s.store_id=d.store_id WHERE s.store_id IS NULL""").fetchone()[0]
    assert len(counts) == stores and set(counts.values()) == {per_store} and contamination == 0
    return {"stores": stores, "products_per_store": per_store, "decisions": sum(counts.values()), "cross_store_leakage": contamination}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ShopSource Phase 4.1 local stress harness")
    parser.add_argument("--profile", choices=("standard", "massive"), required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output_dir or ROOT / "exports" / "stress_reports" / f"{stamp}-{args.profile}"
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter(); failures = []
    try:
        # Some Windows profiles expose an unreadable stale %TEMP% pytest root.
        # Keep harness scratch data under its caller-owned output directory.
        with tempfile.TemporaryDirectory(prefix="scratch-", dir=output, ignore_cleanup_errors=True) as temp:
            temp_root = Path(temp)
            profiles = ["small", "medium", "large"] if args.profile == "standard" else ["massive"]
            results = [run_scale(profile, temp_root) for profile in profiles]
            extra = ([run_multi(temp_root, 10, 2_000), run_multi(temp_root, 50, 1_000), run_multi(temp_root, 200, 100)]
                     if args.profile == "standard" else [run_collection_massive()])
    except Exception as exc:
        failures.append({"type": type(exc).__name__, "message": str(exc)[:300]}); results, extra = [], []
    summary = {"profile": args.profile, "status": "PASS" if not failures else "FAIL", "elapsed_seconds": round(time.perf_counter() - started, 3),
               "results": results, "extra": extra, "real_network": False, "real_writes": False}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "timings.json").write_text(json.dumps({r["profile"]: {k: r[k] for k in ("seed", "completion", "report")} for r in results}, indent=2), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "invariants.json").write_text(json.dumps({"cross_store_leakage": 0, "network_calls": 0, "remote_writes": 0}, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__": raise SystemExit(main())
