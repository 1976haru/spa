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
    portal_package_verified INTEGER NOT NULL DEFAULT 0,
    desktop_dataset_id TEXT,
    desktop_source_path TEXT,
    desktop_destination_path TEXT,
    desktop_staged_at TEXT,
    desktop_product_count INTEGER,
    desktop_hash_verified INTEGER NOT NULL DEFAULT 0,
    desktop_hashes_json TEXT NOT NULL DEFAULT '{}',
    spark_desktop_roundtrip_verified INTEGER NOT NULL DEFAULT 0,
    spark_desktop_verified_at TEXT,
    spark_desktop_verified_product_count INTEGER,
    campaign_total INTEGER,
    campaign_exportable INTEGER,
    campaign_excluded INTEGER,
    campaign_excluded_by_status_json TEXT NOT NULL DEFAULT '{}',
    campaign_missing_decision INTEGER NOT NULL DEFAULT 0,
    campaign_missing_product INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS sourcing_campaigns (
    campaign_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    name TEXT NOT NULL,
    candidate_target INTEGER NOT NULL,
    detail_target INTEGER NOT NULL,
    search_delay_seconds INTEGER NOT NULL DEFAULT 8,
    detail_interval_seconds INTEGER NOT NULL DEFAULT 4,
    stale_page_threshold INTEGER NOT NULL DEFAULT 2,
    unique_candidates INTEGER NOT NULL DEFAULT 0,
    detail_complete INTEGER NOT NULL DEFAULT 0,
    master_imported INTEGER NOT NULL DEFAULT 0,
    classified INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    search_pages INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    batch_run_id TEXT,
    spark_package_id TEXT,
    spark_dataset_id TEXT,
    spark_total INTEGER,
    spark_included INTEGER,
    spark_excluded INTEGER,
    spark_desktop_roundtrip_verified INTEGER NOT NULL DEFAULT 0,
    verified_product_count INTEGER,
    shopify_upload_attempted_at TEXT,
    shopify_upload_result TEXT,
    shopify_uploaded_count INTEGER,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
    ,search_worker_status TEXT NOT NULL DEFAULT 'NOT_CONNECTED'
    ,current_keyword TEXT NOT NULL DEFAULT ''
    ,current_page INTEGER NOT NULL DEFAULT 0
    ,last_search_capture_at TEXT
    ,last_search_error TEXT NOT NULL DEFAULT ''
    ,campaign_type TEXT NOT NULL DEFAULT 'LIVE_2000'
    ,plan_id TEXT NOT NULL DEFAULT ''
    ,search_complete INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sourcing_campaign_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES sourcing_campaigns(campaign_id) ON DELETE CASCADE,
    keyword TEXT NOT NULL,
    position INTEGER NOT NULL,
    current_page INTEGER NOT NULL DEFAULT 0,
    pages_captured INTEGER NOT NULL DEFAULT 0,
    new_candidates INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    consecutive_zero_pages INTEGER NOT NULL DEFAULT 0,
    exhausted INTEGER NOT NULL DEFAULT 0,
    last_url TEXT NOT NULL DEFAULT '',
    next_url TEXT NOT NULL DEFAULT '',
    category_id INTEGER,
    category_quota INTEGER NOT NULL DEFAULT 0,
    max_pages INTEGER NOT NULL DEFAULT 5,
    max_unique INTEGER NOT NULL DEFAULT 300,
    keyword_score REAL NOT NULL DEFAULT 0.5,
    historical_yield REAL NOT NULL DEFAULT 0,
    exhaustion_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, keyword)
);

CREATE TABLE IF NOT EXISTS sourcing_campaign_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES sourcing_campaigns(campaign_id) ON DELETE CASCADE,
    asin TEXT NOT NULL,
    capture_run_id TEXT NOT NULL,
    first_keyword TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'NEEDS_DETAIL',
    failure_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(campaign_id, asin)
);

CREATE TABLE IF NOT EXISTS sourcing_campaign_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES sourcing_campaigns(campaign_id) ON DELETE CASCADE,
    asin TEXT NOT NULL,
    keyword TEXT NOT NULL DEFAULT '',
    search_url TEXT NOT NULL DEFAULT '',
    page_number INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sourcing_campaign_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES sourcing_campaigns(campaign_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sourcing_campaign_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES sourcing_campaigns(campaign_id) ON DELETE CASCADE,
    keyword TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    normalized_url TEXT NOT NULL,
    capture_run_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    UNIQUE(campaign_id, keyword, page_number),
    UNIQUE(campaign_id, normalized_url)
);

CREATE TABLE IF NOT EXISTS store_sourcing_plans (
    plan_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    total_candidate_target INTEGER NOT NULL,
    detail_target INTEGER NOT NULL,
    detail_ratio REAL NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    planner_version TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(store_id, version)
);
CREATE TABLE IF NOT EXISTS store_sourcing_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES store_sourcing_plans(plan_id) ON DELETE CASCADE,
    category_key TEXT NOT NULL,
    category_name TEXT NOT NULL,
    weight REAL NOT NULL,
    quota INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(plan_id, category_key)
);
CREATE TABLE IF NOT EXISTS store_sourcing_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES store_sourcing_categories(id) ON DELETE CASCADE,
    keyword TEXT NOT NULL,
    source TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    historical_yield REAL,
    duplicate_rate REAL,
    price_fit REAL,
    quality_fit REAL,
    risk_rate REAL,
    pages_used INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(category_id, keyword COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS store_collection_plans (
    plan_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    sourcing_plan_id TEXT,
    version INTEGER NOT NULL,
    planner_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    master_product_count INTEGER NOT NULL DEFAULT 0,
    included_product_count INTEGER NOT NULL DEFAULT 0,
    unmatched_product_count INTEGER NOT NULL DEFAULT 0,
    unmatched_percentage REAL NOT NULL DEFAULT 0,
    settings_json TEXT NOT NULL DEFAULT '{}',
    diff_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(store_id, version)
);
CREATE TABLE IF NOT EXISTS store_collection_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES store_collection_plans(plan_id) ON DELETE CASCADE,
    collection_key TEXT NOT NULL,
    source_category_id INTEGER,
    title TEXT NOT NULL,
    description_html TEXT NOT NULL DEFAULT '',
    handle TEXT NOT NULL,
    priority INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    match_mode TEXT NOT NULL DEFAULT 'ANY',
    rule_strategy TEXT NOT NULL DEFAULT 'TITLE_FALLBACK',
    estimated_product_count INTEGER NOT NULL DEFAULT 0,
    title_rule_specificity_estimate REAL NOT NULL DEFAULT 0,
    sample_products_json TEXT NOT NULL DEFAULT '[]',
    store_status_breakdown_json TEXT NOT NULL DEFAULT '{}',
    warning_json TEXT NOT NULL DEFAULT '[]',
    image_prompt TEXT NOT NULL DEFAULT '',
    image_alt_text TEXT NOT NULL DEFAULT '',
    shopify_collection_id TEXT,
    shopify_sync_status TEXT NOT NULL DEFAULT 'NOT_SYNCED',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(plan_id, collection_key)
);
CREATE TABLE IF NOT EXISTS store_collection_conditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_definition_id INTEGER NOT NULL REFERENCES store_collection_definitions(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    relation TEXT NOT NULL,
    value TEXT NOT NULL,
    group_operator TEXT NOT NULL DEFAULT 'OR',
    priority INTEGER NOT NULL DEFAULT 0
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
CREATE INDEX IF NOT EXISTS idx_collection_plans_store_version ON store_collection_plans(store_id,version DESC);
CREATE INDEX IF NOT EXISTS idx_collection_definitions_plan_priority ON store_collection_definitions(plan_id,priority,id);
CREATE INDEX IF NOT EXISTS idx_collection_conditions_definition_priority ON store_collection_conditions(collection_definition_id,priority,id);
CREATE INDEX IF NOT EXISTS idx_capture_runs_store_time ON browser_capture_runs(store_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_capture_candidates_run_status ON browser_capture_candidates(run_id, capture_status);
CREATE INDEX IF NOT EXISTS idx_capture_candidates_asin ON browser_capture_candidates(asin);
CREATE INDEX IF NOT EXISTS idx_capture_errors_created ON browser_capture_errors(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_batch_runs_store_status ON browser_batch_runs(store_id,status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_batch_items_queue ON browser_batch_items(batch_run_id,state,priority,id);
CREATE INDEX IF NOT EXISTS idx_batch_events_run ON browser_batch_events(batch_run_id,created_at);
CREATE INDEX IF NOT EXISTS idx_campaigns_store_status ON sourcing_campaigns(store_id,status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_campaign_candidates_state ON sourcing_campaign_candidates(campaign_id,state,id);
CREATE INDEX IF NOT EXISTS idx_campaign_events_recent ON sourcing_campaign_events(campaign_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_campaign_pages_lookup ON sourcing_campaign_pages(campaign_id,keyword,page_number);
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
    "desktop_dataset_id": "TEXT",
    "desktop_source_path": "TEXT",
    "desktop_destination_path": "TEXT",
    "desktop_staged_at": "TEXT",
    "desktop_product_count": "INTEGER",
    "desktop_hash_verified": "INTEGER NOT NULL DEFAULT 0",
    "desktop_hashes_json": "TEXT NOT NULL DEFAULT '{}'",
    "spark_desktop_roundtrip_verified": "INTEGER NOT NULL DEFAULT 0",
    "spark_desktop_verified_at": "TEXT",
    "spark_desktop_verified_product_count": "INTEGER",
    "campaign_total": "INTEGER",
    "campaign_exportable": "INTEGER",
    "campaign_excluded": "INTEGER",
    "campaign_excluded_by_status_json": "TEXT NOT NULL DEFAULT '{}'",
    "campaign_missing_decision": "INTEGER NOT NULL DEFAULT 0",
    "campaign_missing_product": "INTEGER NOT NULL DEFAULT 0",
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

CAMPAIGN_ADDITIVE_COLUMNS = {
    "search_worker_status": "TEXT NOT NULL DEFAULT 'NOT_CONNECTED'",
    "current_keyword": "TEXT NOT NULL DEFAULT ''",
    "current_page": "INTEGER NOT NULL DEFAULT 0",
    "last_search_capture_at": "TEXT",
    "last_search_error": "TEXT NOT NULL DEFAULT ''",
    "campaign_type": "TEXT NOT NULL DEFAULT 'LIVE_2000'",
    "plan_id": "TEXT NOT NULL DEFAULT ''",
    "search_complete": "INTEGER NOT NULL DEFAULT 0",
}

CAMPAIGN_KEYWORD_ADDITIVE_COLUMNS = {
    "next_url": "TEXT NOT NULL DEFAULT ''", "category_id": "INTEGER",
    "category_quota": "INTEGER NOT NULL DEFAULT 0", "max_pages": "INTEGER NOT NULL DEFAULT 5",
    "max_unique": "INTEGER NOT NULL DEFAULT 300", "keyword_score": "REAL NOT NULL DEFAULT 0.5",
    "historical_yield": "REAL NOT NULL DEFAULT 0", "exhaustion_reason": "TEXT NOT NULL DEFAULT ''",
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
        campaign_columns = {row["name"] for row in con.execute("PRAGMA table_info(sourcing_campaigns)")}
        for name, declaration in CAMPAIGN_ADDITIVE_COLUMNS.items():
            if name not in campaign_columns:
                con.execute(f"ALTER TABLE sourcing_campaigns ADD COLUMN {name} {declaration}")
        keyword_columns = {row["name"] for row in con.execute("PRAGMA table_info(sourcing_campaign_keywords)")}
        for name, declaration in CAMPAIGN_KEYWORD_ADDITIVE_COLUMNS.items():
            if name not in keyword_columns:
                con.execute(f"ALTER TABLE sourcing_campaign_keywords ADD COLUMN {name} {declaration}")
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
