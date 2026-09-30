from __future__ import annotations

import json
from pathlib import Path

from ..classifier import ALLOWED_STATUSES
from ..connectors.spark_center_package import list_packages, mark_package, SparkCenterPackageService
from ..db import connect, get_store, init_db, upsert_store, utc_now
from ..paths import EXPORT_DIR
from ..stats import master_summary, store_summary

PRODUCT_SORTS = {
    "asin": "p.asin", "title": "p.title", "price": "p.price",
    "source": "p.source_kind", "status": "d.final_status",
    "fit_score": "d.fit_score", "first_seen": "p.first_seen_at", "last_seen": "p.last_seen_at",
}


def list_stores(db=None) -> list[dict]:
    init_db(db)
    with connect(db) as con:
        rows = con.execute("SELECT store_id,store_name,category,profile_json FROM stores WHERE enabled=1 ORDER BY store_id").fetchall()
    result = []
    for row in rows:
        profile = json.loads(row["profile_json"])
        primary = [band for band in profile.get("price_bands", []) if band.get("status") == "PRIMARY"]
        reserve = [band for band in profile.get("price_bands", []) if "RESERVE" in str(band.get("status", ""))]
        result.append({
            "store_id": row["store_id"], "store_name": row["store_name"], "category": row["category"],
            "profile": profile, "primary_bands": primary, "reserve_bands": reserve,
        })
    return result


def dashboard_data(store_id: str, db=None) -> dict:
    init_db(db)
    master = master_summary(db)
    summary = store_summary(store_id, db)
    with connect(db) as con:
        sourcing = con.execute("SELECT * FROM sourcing_runs WHERE store_id=? ORDER BY id DESC LIMIT 1", (store_id,)).fetchone()
        package = con.execute("SELECT * FROM export_runs WHERE target='SPARK_CENTER_MANUAL' AND store_id=? ORDER BY id DESC LIMIT 1", (store_id,)).fetchone()
        errors = con.execute("SELECT error_code,error_message,created_at FROM import_errors ORDER BY id DESC LIMIT 5").fetchall()
    return {"master": master, "store": summary, "sourcing": dict(sourcing) if sourcing else None,
            "package": dict(package) if package else None, "errors": [dict(row) for row in errors]}


def product_page(*, store_id: str, page: int = 0, page_size: int = 100,
                 search: str = "", status: str = "ALL", source: str = "ALL",
                 sort: str = "asin", descending: bool = False, db=None) -> dict:
    init_db(db)
    if page < 0 or page_size not in {25, 50, 100, 200}:
        raise ValueError("Invalid page or page size")
    order = PRODUCT_SORTS.get(sort)
    if not order:
        raise ValueError("Unsupported product sort field")
    # The JOIN is already scoped to the selected store. Decision-less MASTER
    # products must remain visible when the status filter is ALL.
    clauses, params = ["1=1"], []
    if search.strip():
        term = f"%{search.strip()}%"
        clauses.append("(p.asin LIKE ? OR p.title LIKE ? OR p.brand LIKE ?)")
        params.extend((term, term, term))
    if status != "ALL":
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"Unsupported status: {status}")
        clauses.append("d.final_status=?")
        params.append(status)
    if source != "ALL":
        clauses.append("p.source_kind=?")
        params.append(source)
    where = " AND ".join(clauses)
    with connect(db) as con:
        total = con.execute(f"SELECT COUNT(*) FROM products p LEFT JOIN store_product_decisions d ON d.product_id=p.id AND d.store_id=? WHERE {where}",
                            [store_id, *params]).fetchone()[0]
        rows = con.execute(f"""SELECT p.id,p.asin,p.title,p.brand,p.price,p.source_kind,p.images_json,
            p.first_seen_at,p.last_seen_at,p.category,p.rating,p.review_count,d.fit_score,d.price_status,
            d.risk_status,d.final_status,d.manual_override,d.reasons_json
            FROM products p LEFT JOIN store_product_decisions d ON d.product_id=p.id AND d.store_id=?
            WHERE {where} ORDER BY {order} {'DESC' if descending else 'ASC'} LIMIT ? OFFSET ?""",
            [store_id, *params, page_size, page * page_size]).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["images"] = json.loads(item.pop("images_json") or "[]")
        except json.JSONDecodeError:
            item["images"] = []
        try:
            item["reasons"] = json.loads(item.pop("reasons_json") or "[]")
        except json.JSONDecodeError:
            item["reasons"] = []
        item["manual_override"] = bool(item.get("manual_override"))
        result.append(item)
    return {"rows": result, "total": total, "page": page, "page_size": page_size}


def product_detail(product_id: int, store_id: str, db=None) -> dict:
    with connect(db) as con:
        product = con.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not product:
            raise KeyError(f"Product not found: {product_id}")
        decision = con.execute("SELECT * FROM store_product_decisions WHERE store_id=? AND product_id=?", (store_id, product_id)).fetchone()
        occurrences = con.execute("""SELECT job_id,source_file,collected_at,source_url,list_page,source_kind
            FROM product_occurrences WHERE product_id=? ORDER BY collected_at DESC,id DESC LIMIT 30""", (product_id,)).fetchall()
    result = dict(product)
    for field, source in (("images", "images_json"), ("options", "options_json"),
                          ("tags", "tags_json"), ("overview", "overview_json"), ("aboutThis", "about_json")):
        try:
            result[field] = json.loads(result.get(source) or ("{}" if field == "options" else "[]"))
        except json.JSONDecodeError:
            result[field] = []
    result["decision"] = dict(decision) if decision else None
    if decision:
        result["decision"]["reasons"] = json.loads(decision["reasons_json"] or "[]")
    result["occurrences"] = [dict(row) for row in occurrences]
    return result


def bulk_override(store_id: str, product_ids: list[int], status: str, note: str = "", db=None) -> int:
    status = status.upper()
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Unsupported decision status: {status}")
    if not product_ids:
        return 0
    now = utc_now()
    changed = 0
    with connect(db) as con:
        for start in range(0, len(product_ids), 400):
            batch = product_ids[start:start + 400]
            marks = ",".join("?" for _ in batch)
            changed += con.execute(
                f"""UPDATE store_product_decisions SET final_status=?,manual_override=1,memo=?,classified_at=?
                WHERE store_id=? AND product_id IN ({marks})""",
                [status, note, now, store_id, *batch],
            ).rowcount
    return changed


def clear_bulk_override(store_id: str, product_ids: list[int], db=None) -> int:
    if not product_ids:
        return 0
    changed = 0
    with connect(db) as con:
        for start in range(0, len(product_ids), 400):
            batch = product_ids[start:start + 400]
            marks = ",".join("?" for _ in batch)
            changed += con.execute(
                f"UPDATE store_product_decisions SET manual_override=0,memo='',final_status=auto_status,classified_at=? WHERE store_id=? AND product_id IN ({marks})",
                [utc_now(), store_id, *batch],
            ).rowcount
    return changed


def list_sourcing_runs(store_id: str | None = None, limit: int = 100, db=None) -> list[dict]:
    init_db(db)
    params = []
    where = ""
    if store_id:
        where = "WHERE store_id=?"
        params.append(store_id)
    params.append(limit)
    with connect(db) as con:
        return [dict(row) for row in con.execute(
            f"SELECT run_id,provider,store_id,status,target_candidates,started_at,finished_at,discovered_asins,hydrated_products,inserted,updated,tokens_consumed,tokens_left,error FROM sourcing_runs {where} ORDER BY id DESC LIMIT ?",
            params,
        )]


def list_recent_errors(limit: int = 100, db=None) -> list[dict]:
    with connect(db) as con:
        return [dict(row) for row in con.execute("""SELECT id,import_run_id,job_id,source_file,error_code,error_message,created_at
            FROM import_errors ORDER BY id DESC LIMIT ?""", (limit,))]


def get_app_setting(key: str, default=None, db=None):
    init_db(db)
    with connect(db) as con:
        row = con.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)).fetchone()
    return row[0] if row else default


def set_app_settings(values: dict[str, object], db=None) -> None:
    init_db(db)
    now = utc_now()
    with connect(db) as con:
        con.executemany("""INSERT INTO app_settings(setting_key,setting_value,updated_at) VALUES(?,?,?)
            ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=excluded.updated_at""",
            [(key, str(value), now) for key, value in values.items()])


def create_store_profile(profile: dict, db=None) -> Path:
    from ..paths import STORE_DIR
    import re
    init_db(db)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(profile.get("store_id", ""))):
        raise ValueError("Store ID may contain only letters, numbers, hyphen, and underscore")
    with connect(db) as con:
        if con.execute("SELECT 1 FROM stores WHERE store_id=?", (profile["store_id"],)).fetchone():
            raise ValueError(f"Store ID already exists: {profile['store_id']}")
    upsert_store(profile, db)
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", profile["store_name"]).strip("_") or "store"
    path = STORE_DIR / f"{profile['store_id']}_{slug}.json"
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def create_spark_package(store_id: str, statuses: list[str], limit: int, asins: list[str] | None = None, db=None):
    return SparkCenterPackageService().create(store_id=store_id, statuses=statuses, limit=limit, asins=asins, db=db)


def open_package(path: str | Path) -> None:
    import os
    os.startfile(str(path))
