"""Reusable synthetic scale helpers for Phase 4.1 qualification.

No helper performs network access or writes outside the caller-provided DB and
report directories.
"""
from __future__ import annotations

import re
import sqlite3
import time
import tracemalloc
from pathlib import Path

from .db import connect, init_db

PROFILES = {
    "small": (100, 5), "medium": (2_000, 8),
    "large": (10_000, 15), "massive": (50_000, 30),
}
UI_PREVIEW_CAP = 100


def seed_catalog(db: str | Path, *, products: int, stores: int = 1, products_per_store: int | None = None) -> dict:
    """Batch-seed a shared MASTER and isolated Store Decisions."""
    init_db(db)
    per_store = products if products_per_store is None else products_per_store
    master_count = max(products, per_store)
    product_rows = [(f"S{n:09d}", f"Synthetic organizer {n} collection-{n % 30}", "Fixture", 29.99,
                     "storage", "[]", "{}", "now", "now") for n in range(master_count)]
    with connect(db) as con:
        con.executemany("""INSERT INTO products(asin,title,brand,price,category,tags_json,raw_json,first_seen_at,last_seen_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""", product_rows)
        ids = [row[0] for row in con.execute("SELECT id FROM products ORDER BY id LIMIT ?", (per_store,))]
        now = "now"
        for store_no in range(stores):
            store_id = f"stress-{store_no:03d}"
            con.execute("INSERT INTO stores(store_id,store_name,category,profile_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (store_id, f"Stress Store {store_no}", "storage", "{}", now, now))
            con.executemany("""INSERT INTO store_product_decisions(store_id,product_id,price_status,risk_status,auto_status,final_status,classified_at)
                               VALUES(?,?,'IN_RANGE','SAFE','PRIMARY','PRIMARY',?)""",
                            ((store_id, product_id, now) for product_id in ids))
    return {"products": master_count, "stores": stores, "decisions": stores * per_store}


def seed_collections(db: str | Path, store_id: str, count: int) -> str:
    plan_id = f"stress-plan-{store_id}"
    with connect(db) as con:
        con.execute("""INSERT INTO store_collection_plans(plan_id,store_id,version,planner_version,status,created_at,updated_at)
                       VALUES(?,?,1,'stress-4.1','READY','now','now')""", (plan_id, store_id))
        con.executemany("""INSERT INTO store_collection_definitions(plan_id,collection_key,title,handle,priority,enabled,created_at,updated_at)
                           VALUES(?,?,?,?,?,1,'now','now')""",
                        ((plan_id, f"c{n}", f"Collection {n}", f"collection-{n}", n) for n in range(count)))
    return plan_id


def _tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^\w]+", str(value).casefold(), flags=re.UNICODE) if token}


def match_collection_rules(products, rules, *, strategy="MIXED", sample_cap=10) -> dict:
    """Deterministic, bounded-memory O(products × rules) fixture matcher."""
    compiled = []
    for position, rule in enumerate(rules):
        title_terms = tuple(sorted({str(x).casefold() for x in rule.get("title_terms", []) if str(x)}))
        title_patterns = tuple(re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.UNICODE) for term in title_terms)
        tag_terms = frozenset(str(x).casefold() for x in rule.get("tags", []) if str(x))
        compiled.append((str(rule["key"]), position, title_patterns, tag_terms))
    counts = {key: 0 for key, *_ in compiled}; samples = {key: [] for key, *_ in compiled}
    unmatched = overlaps = total = 0
    for product in products:
        total += 1; title = str(product.get("title", "")).casefold(); tags = {str(x).casefold() for x in product.get("tags", [])}
        matched = []
        for key, _position, title_patterns, tag_terms in compiled:
            title_hit = bool(title_patterns) and any(pattern.search(title) for pattern in title_patterns)
            tag_hit = bool(tag_terms & tags)
            hit = tag_hit if strategy == "TAG_PREFERRED" and tag_terms else title_hit
            if strategy == "MIXED": hit = title_hit or tag_hit
            if hit:
                counts[key] += 1; matched.append(key)
                if len(samples[key]) < sample_cap: samples[key].append(product.get("id"))
        if not matched: unmatched += 1
        if len(matched) > 1: overlaps += 1
    broad = [key for key, count in counts.items() if total and count / total > 0.8]
    return {"total": total, "counts": counts, "unmatched": unmatched, "overlaps": overlaps,
            "broad_warnings": broad, "samples": samples, "strategy": strategy}


def measure(callable_, *args, **kwargs):
    tracemalloc.start(); started = time.perf_counter()
    result = callable_(*args, **kwargs)
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    return result, {"elapsed_seconds": round(elapsed, 4), "peak_memory_bytes": peak}


def sqlite_size(db: str | Path) -> int:
    return Path(db).stat().st_size if Path(db).exists() else 0


def integrity(db: str | Path) -> dict:
    with sqlite3.connect(db) as con:
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
        indexes = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'").fetchone()[0]
    return {"integrity": result, "indexes": indexes}
