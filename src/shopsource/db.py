from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .paths import DEFAULT_DB, ensure_dirs

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asin TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'amazon',
    source_kind TEXT NOT NULL DEFAULT 'SPARK_STORAGE',
    url TEXT,
    title TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    price REAL,
    currency TEXT NOT NULL DEFAULT 'USD',
    category TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    overview_json TEXT NOT NULL DEFAULT '[]',
    about_json TEXT NOT NULL DEFAULT '[]',
    images_json TEXT NOT NULL DEFAULT '[]',
    options_json TEXT NOT NULL DEFAULT '{}',
    rating REAL,
    review_count INTEGER,
    source_url TEXT,
    list_page INTEGER,
    raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_key TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    files_seen INTEGER NOT NULL DEFAULT 0,
    rows_read INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    invalid INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    error TEXT
);

CREATE TABLE IF NOT EXISTS product_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    import_run_id INTEGER REFERENCES import_runs(id) ON DELETE SET NULL,
    job_id TEXT,
    source_file TEXT,
    collected_at TEXT,
    source_url TEXT,
    list_page INTEGER,
    raw_json TEXT,
    source_kind TEXT NOT NULL DEFAULT 'SPARK_STORAGE',
    UNIQUE(product_id, job_id, source_file)
);

CREATE TABLE IF NOT EXISTS import_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_run_id INTEGER NOT NULL REFERENCES import_runs(id) ON DELETE CASCADE,
    job_id TEXT,
    source_file TEXT,
    error_code TEXT NOT NULL,
    error_message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL UNIQUE,
    store_name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    profile_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS store_product_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    fit_score REAL NOT NULL DEFAULT 0,
    price_status TEXT NOT NULL,
    risk_status TEXT NOT NULL,
    auto_status TEXT NOT NULL,
    final_status TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    manual_override INTEGER NOT NULL DEFAULT 0,
    memo TEXT NOT NULL DEFAULT '',
    classified_at TEXT NOT NULL,
    UNIQUE(store_id, product_id)
);

CREATE TABLE IF NOT EXISTS export_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    package_id TEXT,
    target TEXT NOT NULL DEFAULT 'SPARK_DESKTOP',
    store_id TEXT NOT NULL,
    store_name TEXT NOT NULL DEFAULT '',
    statuses_json TEXT NOT NULL,
    output_path TEXT NOT NULL,
    requested_limit INTEGER,
    product_count INTEGER NOT NULL,
    asin_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    package_status TEXT NOT NULL DEFAULT 'CREATED',
    uploaded_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    portal_package_verified INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sourcing_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    store_id TEXT NOT NULL,
    marketplace TEXT NOT NULL DEFAULT 'US',
    recipe_snapshot_json TEXT NOT NULL,
    target_candidates INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT,
    finder_requests INTEGER NOT NULL DEFAULT 0,
    product_requests INTEGER NOT NULL DEFAULT 0,
    tokens_consumed INTEGER NOT NULL DEFAULT 0,
    tokens_left INTEGER,
    discovered_asins INTEGER NOT NULL DEFAULT 0,
    hydrated_products INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    invalid INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS sourcing_run_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES sourcing_runs(run_id) ON DELETE CASCADE,
    asin TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    keyword TEXT NOT NULL DEFAULT '',
    finder_page INTEGER NOT NULL DEFAULT 0,
    discovered_at TEXT NOT NULL,
    discovery_rank INTEGER NOT NULL,
    query_hash TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'DISCOVERED',
    reason_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(run_id, asin, recipe_id, finder_page)
);

CREATE TABLE IF NOT EXISTS keyword_validation_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    candidate_yield INTEGER NOT NULL DEFAULT 0,
    price_fit REAL NOT NULL DEFAULT 0,
    quality_fit REAL NOT NULL DEFAULT 0,
    risk_rate REAL NOT NULL DEFAULT 0,
    master_duplicate_rate REAL NOT NULL DEFAULT 0,
    tokens_consumed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS browser_capture_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    store_id TEXT NOT NULL,
    keyword TEXT NOT NULL DEFAULT '',
    search_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'SEARCH_CAPTURED',
    captured_at TEXT NOT NULL,
    candidates INTEGER NOT NULL DEFAULT 0,
    detailed INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS browser_capture_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES browser_capture_runs(run_id) ON DELETE CASCADE,
    asin TEXT NOT NULL,
    search_payload_json TEXT NOT NULL,
    detail_payload_json TEXT,
    completeness_score INTEGER NOT NULL DEFAULT 0,
    capture_status TEXT NOT NULL DEFAULT 'NEEDS_DETAIL',
    selected INTEGER NOT NULL DEFAULT 0,
    seen_count INTEGER NOT NULL DEFAULT 1,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    search_urls_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, asin)
);

CREATE TABLE IF NOT EXISTS browser_capture_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT,
    action TEXT NOT NULL,
    error_message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS browser_batch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    store_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    target_candidates INTEGER NOT NULL,
    target_mode TEXT NOT NULL DEFAULT 'CANDIDATES',
    status TEXT NOT NULL DEFAULT 'PENDING',
    auto_import_master INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    started_at TEXT,
    paused_at TEXT,
    finished_at TEXT,
    total_seen INTEGER NOT NULL DEFAULT 0,
    deduped INTEGER NOT NULL DEFAULT 0,
    prefiltered INTEGER NOT NULL DEFAULT 0,
    detail_pending INTEGER NOT NULL DEFAULT 0,
    detail_complete INTEGER NOT NULL DEFAULT 0,
    master_imported INTEGER NOT NULL DEFAULT 0,
    primary_count INTEGER NOT NULL DEFAULT 0,
    reserve_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    restricted_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS browser_batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_run_id TEXT NOT NULL REFERENCES browser_batch_runs(run_id) ON DELETE CASCADE,
    asin TEXT NOT NULL,
    capture_run_id TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    state TEXT NOT NULL DEFAULT 'DISCOVERED',
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    completeness_score INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_run_id, asin)
);

CREATE TABLE IF NOT EXISTS browser_batch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_run_id TEXT NOT NULL REFERENCES browser_batch_runs(run_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_price ON products(price);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_decisions_store_status ON store_product_decisions(store_id, final_status);
CREATE INDEX IF NOT EXISTS idx_decisions_product ON store_product_decisions(product_id);
CREATE INDEX IF NOT EXISTS idx_occurrence_job ON product_occurrences(job_id);
CREATE INDEX IF NOT EXISTS idx_occurrence_product ON product_occurrences(product_id);
CREATE INDEX IF NOT EXISTS idx_occurrence_import_run ON product_occurrences(import_run_id);
CREATE INDEX IF NOT EXISTS idx_import_errors_run ON import_errors(import_run_id);
CREATE INDEX IF NOT EXISTS idx_export_runs_store_created ON export_runs(store_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sourcing_runs_store_created ON sourcing_runs(store_id, started_at);
CREATE INDEX IF NOT EXISTS idx_sourcing_runs_status ON sourcing_runs(status);
CREATE INDEX IF NOT EXISTS idx_sourcing_candidates_run_asin ON sourcing_run_candidates(run_id, asin);
CREATE INDEX IF NOT EXISTS idx_keyword_validation_store_keyword ON keyword_validation_results(store_id, keyword, checked_at);
CREATE INDEX IF NOT EXISTS idx_capture_runs_store_time ON browser_capture_runs(store_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_capture_candidates_run_status ON browser_capture_candidates(run_id, capture_status);
CREATE INDEX IF NOT EXISTS idx_capture_candidates_asin ON browser_capture_candidates(asin);
CREATE INDEX IF NOT EXISTS idx_capture_errors_created ON browser_capture_errors(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_batch_runs_store_status ON browser_batch_runs(store_id,status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_batch_items_queue ON browser_batch_items(batch_run_id,state,priority,id);
CREATE INDEX IF NOT EXISTS idx_batch_events_run ON browser_batch_events(batch_run_id,created_at);
"""

EXPORT_RUN_ADDITIVE_COLUMNS = {
    "package_id": "TEXT",
    "target": "TEXT NOT NULL DEFAULT 'SPARK_DESKTOP'",
    "store_name": "TEXT NOT NULL DEFAULT ''",
    "requested_limit": "INTEGER",
    "package_status": "TEXT NOT NULL DEFAULT 'CREATED'",
    "uploaded_at": "TEXT",
    "note": "TEXT NOT NULL DEFAULT ''",
    "portal_package_verified": "INTEGER NOT NULL DEFAULT 0",
}

PRODUCT_ADDITIVE_COLUMNS = {
    "source_kind": "TEXT NOT NULL DEFAULT 'SPARK_STORAGE'",
}

OCCURRENCE_ADDITIVE_COLUMNS = {
    "raw_json": "TEXT",
    "source_kind": "TEXT NOT NULL DEFAULT 'SPARK_STORAGE'",
}

BATCH_RUN_ADDITIVE_COLUMNS = {
    "reserve_count": "INTEGER NOT NULL DEFAULT 0",
    "restricted_count": "INTEGER NOT NULL DEFAULT 0",
    "precompleted_count": "INTEGER NOT NULL DEFAULT 0",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path(path: str | Path | None = None) -> Path:
    ensure_dirs()
    result = Path(path) if path else DEFAULT_DB
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


@contextmanager
def connect(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    p = db_path(path)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db(path: str | Path | None = None) -> Path:
    p = db_path(path)
    with connect(p) as con:
        con.executescript(SCHEMA)
        product_columns = {row["name"] for row in con.execute("PRAGMA table_info(products)")}
        for name, declaration in PRODUCT_ADDITIVE_COLUMNS.items():
            if name not in product_columns:
                con.execute(f"ALTER TABLE products ADD COLUMN {name} {declaration}")
        occurrence_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(product_occurrences)")
        }
        for name, declaration in OCCURRENCE_ADDITIVE_COLUMNS.items():
            if name not in occurrence_columns:
                con.execute(f"ALTER TABLE product_occurrences ADD COLUMN {name} {declaration}")
        batch_columns = {row["name"] for row in con.execute("PRAGMA table_info(browser_batch_runs)")}
        for name, declaration in BATCH_RUN_ADDITIVE_COLUMNS.items():
            if name not in batch_columns:
                con.execute(f"ALTER TABLE browser_batch_runs ADD COLUMN {name} {declaration}")
        export_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(export_runs)")
        }
        for name, declaration in EXPORT_RUN_ADDITIVE_COLUMNS.items():
            if name not in export_columns:
                con.execute(f"ALTER TABLE export_runs ADD COLUMN {name} {declaration}")
        con.execute("UPDATE export_runs SET package_id=job_id WHERE package_id IS NULL")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_export_runs_package_id "
            "ON export_runs(package_id) WHERE package_id IS NOT NULL"
        )
    return p


def upsert_store(profile: dict, path: str | Path | None = None) -> None:
    required = {"store_id", "store_name", "category"}
    missing = required - set(profile)
    if missing:
        raise ValueError(f"Missing store profile keys: {sorted(missing)}")
    now = utc_now()
    with connect(path) as con:
        con.execute(
            """
            INSERT INTO stores(store_id, store_name, category, profile_json, created_at, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(store_id) DO UPDATE SET
                store_name=excluded.store_name,
                category=excluded.category,
                profile_json=excluded.profile_json,
                enabled=1,
                updated_at=excluded.updated_at
            """,
            (
                profile["store_id"], profile["store_name"], profile.get("category", ""),
                json.dumps(profile, ensure_ascii=False), now, now,
            ),
        )


def get_store(store_id: str, path: str | Path | None = None) -> dict:
    with connect(path) as con:
        row = con.execute("SELECT profile_json FROM stores WHERE store_id=?", (store_id,)).fetchone()
    if not row:
        raise KeyError(f"Store profile not found: {store_id}")
    return json.loads(row["profile_json"])
