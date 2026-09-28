from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .connectors.spark_storage import SparkStorageConnector
from .db import connect, init_db, utc_now


def _normalize_epoch(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        v = float(value)
        # Spark sample may contain epoch milliseconds or seconds.
        if v > 10_000_000_000:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return str(value)


def _import_key(source: Path) -> str:
    stat = source.stat()
    raw = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def import_spark(source: str | Path, db=None, allow_reimport: bool = False) -> dict:
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(source)
    init_db(db)
    key = _import_key(source)
    stats = {"files_seen": 0, "rows_read": 0, "inserted": 0, "updated": 0,
             "duplicates": 0, "invalid": 0, "job_ids": set()}

    connector = SparkStorageConnector()
    with connect(db) as con:
        previous = con.execute("SELECT status FROM import_runs WHERE import_key=?", (key,)).fetchone()
        if previous and previous["status"] == "DONE" and not allow_reimport:
            return {**stats, "job_ids": [], "skipped": True, "reason": "source already imported"}
        if previous:
            con.execute("DELETE FROM import_runs WHERE import_key=?", (key,))
        cur = con.execute(
            "INSERT INTO import_runs(import_key, source_path, started_at) VALUES(?,?,?)",
            (key, str(source.resolve()), utc_now()),
        )
        run_id = cur.lastrowid

        try:
            for payload, meta in connector.iter_products(source):
                stats["files_seen"] += 1
                stats["job_ids"].add(meta.get("job_id") or "")
                if payload.get("_invalid"):
                    stats["invalid"] += 1
                    continue
                asin = str(payload.get("asin") or "").strip()
                title = str(payload.get("title") or "").strip()
                if not asin or not title:
                    stats["invalid"] += 1
                    continue
                stats["rows_read"] += 1
                now = utc_now()
                existing = con.execute("SELECT id FROM products WHERE asin=?", (asin,)).fetchone()
                raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                values = (
                    payload.get("url"), title, payload.get("brand") or "", payload.get("price"),
                    "USD", payload.get("category") or "",
                    json.dumps(payload.get("tags") or [], ensure_ascii=False),
                    json.dumps(payload.get("overview") or [], ensure_ascii=False),
                    json.dumps(payload.get("aboutThis") or [], ensure_ascii=False),
                    json.dumps(payload.get("images") or [], ensure_ascii=False),
                    json.dumps(payload.get("options") or {}, ensure_ascii=False),
                    payload.get("rating"), payload.get("reviewCount"), payload.get("_sourceUrl"),
                    payload.get("_listPage"), raw, now,
                )
                if existing:
                    product_id = existing["id"]
                    stats["duplicates"] += 1
                    con.execute(
                        """
                        UPDATE products SET url=?, title=?, brand=?, price=?, currency=?, category=?,
                        tags_json=?, overview_json=?, about_json=?, images_json=?, options_json=?, rating=?,
                        review_count=?, source_url=?, list_page=?, raw_json=?, last_seen_at=? WHERE id=?
                        """,
                        values + (product_id,),
                    )
                    stats["updated"] += 1
                else:
                    cur2 = con.execute(
                        """
                        INSERT INTO products(
                          asin,url,title,brand,price,currency,category,tags_json,overview_json,about_json,
                          images_json,options_json,rating,review_count,source_url,list_page,raw_json,
                          first_seen_at,last_seen_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (asin,) + values[:-1] + (now, now),
                    )
                    product_id = cur2.lastrowid
                    stats["inserted"] += 1

                con.execute(
                    """
                    INSERT OR IGNORE INTO product_occurrences(
                      product_id,import_run_id,job_id,source_file,collected_at,source_url,list_page
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        product_id, run_id, meta.get("job_id"), meta.get("source_file"),
                        _normalize_epoch(meta.get("collected_at")), meta.get("source_url"), meta.get("list_page")
                    ),
                )

            con.execute(
                """
                UPDATE import_runs SET finished_at=?, files_seen=?, rows_read=?, inserted=?, updated=?,
                duplicates=?, invalid=?, status='DONE' WHERE id=?
                """,
                (utc_now(), stats["files_seen"], stats["rows_read"], stats["inserted"], stats["updated"],
                 stats["duplicates"], stats["invalid"], run_id),
            )
        except Exception as exc:
            con.execute("UPDATE import_runs SET finished_at=?, status='FAILED', error=? WHERE id=?",
                        (utc_now(), str(exc), run_id))
            raise

    stats["job_ids"] = sorted(x for x in stats["job_ids"] if x)
    stats["skipped"] = False
    return stats
