from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .connectors.amazon_source_folder import AmazonSourceFolderConnector
from .connectors.spark_storage import SparkStorageConnector
from .db import connect, init_db, utc_now
from .paths import AMAZON_INBOX_DIR


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


def _import_key(source: Path, source_type: str) -> str:
    if source.is_file():
        stat = source.stat()
        inventory = [(source.name, stat.st_size, stat.st_mtime_ns)]
    else:
        inventory = []
        for file in sorted(source.rglob("*.json")):
            stat = file.stat()
            inventory.append((str(file.relative_to(source)), stat.st_size, stat.st_mtime_ns))
    raw = json.dumps(
        [source_type, str(source.resolve()), inventory], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def import_products(
    source: str | Path,
    connector,
    source_type: str,
    db=None,
    allow_reimport: bool = False,
) -> dict:
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(source)
    init_db(db)
    key = _import_key(source, source_type)
    stats = {"files_seen": 0, "rows_read": 0, "inserted": 0, "updated": 0,
             "duplicates": 0, "invalid": 0, "job_ids": set(), "batches": set(),
             "source_path": str(source.resolve())}
    fatal_error: Exception | None = None

    with connect(db) as con:
        previous = con.execute("SELECT id,status FROM import_runs WHERE import_key=?", (key,)).fetchone()
        if previous and previous["status"] == "DONE" and not allow_reimport:
            return {
                **stats,
                "job_ids": [],
                "batches": [],
                "skipped": True,
                "reason": "source already imported",
            }
        if previous:
            run_id = previous["id"]
            con.execute("DELETE FROM import_errors WHERE import_run_id=?", (run_id,))
            con.execute(
                """
                UPDATE import_runs SET source_path=?,started_at=?,finished_at=NULL,files_seen=0,
                  rows_read=0,inserted=0,updated=0,duplicates=0,invalid=0,status='RUNNING',error=NULL
                WHERE id=?
                """,
                (str(source.resolve()), utc_now(), run_id),
            )
        else:
            cur = con.execute(
                "INSERT INTO import_runs(import_key, source_path, started_at) VALUES(?,?,?)",
                (key, str(source.resolve()), utc_now()),
            )
            run_id = cur.lastrowid

        con.execute("SAVEPOINT import_payload")
        try:
            for payload, meta in connector.iter_products(source):
                stats["files_seen"] += 1
                stats["job_ids"].add(meta.get("job_id") or "")
                stats["batches"].add(meta.get("batch_id") or meta.get("job_id") or "")
                if payload.get("_invalid"):
                    stats["invalid"] += 1
                    con.execute(
                        """
                        INSERT INTO import_errors(
                          import_run_id,job_id,source_file,error_code,error_message,created_at
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (
                            run_id, meta.get("job_id"), meta.get("source_file"),
                            meta.get("error_code", "MALFORMED_JSON"),
                            str(payload["_invalid"])[:2000], utc_now(),
                        ),
                    )
                    continue
                asin = str(payload.get("asin") or "").strip().upper()
                title = str(payload.get("title") or "").strip()
                if not asin or not title:
                    stats["invalid"] += 1
                    missing = [name for name, value in (("asin", asin), ("title", title)) if not value]
                    con.execute(
                        """
                        INSERT INTO import_errors(
                          import_run_id,job_id,source_file,error_code,error_message,created_at
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (
                            run_id, meta.get("job_id"), meta.get("source_file"),
                            "MISSING_REQUIRED_FIELD", "Missing: " + ", ".join(missing), utc_now(),
                        ),
                    )
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
                    INSERT INTO product_occurrences(
                      product_id,import_run_id,job_id,source_file,collected_at,source_url,list_page,raw_json
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(product_id,job_id,source_file) DO UPDATE SET
                      raw_json=COALESCE(product_occurrences.raw_json,excluded.raw_json)
                    """,
                    (
                        product_id, run_id, meta.get("job_id"), meta.get("source_file"),
                        _normalize_epoch(meta.get("collected_at")), meta.get("source_url"),
                        meta.get("list_page"), raw,
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
            con.execute("RELEASE SAVEPOINT import_payload")
        except Exception as exc:
            con.execute("ROLLBACK TO SAVEPOINT import_payload")
            con.execute("RELEASE SAVEPOINT import_payload")
            con.execute("UPDATE import_runs SET finished_at=?, status='FAILED', error=? WHERE id=?",
                        (utc_now(), str(exc), run_id))
            fatal_error = exc

    if fatal_error is not None:
        raise fatal_error
    stats["job_ids"] = sorted(x for x in stats["job_ids"] if x)
    stats["batches"] = sorted(x for x in stats["batches"] if x)
    stats["skipped"] = False
    return stats


def import_spark(source: str | Path, db=None, allow_reimport: bool = False) -> dict:
    return import_products(
        source, SparkStorageConnector(), "SPARK_STORAGE", db, allow_reimport
    )


def import_amazon_source(
    source: str | Path | None = None,
    db=None,
    allow_reimport: bool = False,
) -> dict:
    source_path = Path(source) if source is not None else AMAZON_INBOX_DIR
    if source_path.exists() and source_path.is_dir() and not any(source_path.rglob("*.json")):
        raise ValueError(
            "소싱 상품 JSON이 없습니다. source/amazon/inbox에 상품 파일을 넣어주세요."
        )
    result = import_products(
        source_path, AmazonSourceFolderConnector(), "AMAZON_SOURCE_FOLDER", db, allow_reimport
    )
    return result
