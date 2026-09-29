from __future__ import annotations

import json
from pathlib import Path

from .core.rules import classify
from .db import connect, get_store, init_db, utc_now

ALLOWED_STATUSES = {
    "PRIMARY", "RESERVE_A", "RESERVE_B", "RESERVE_C", "LOW_RESERVE",
    "HIGH_RESERVE", "REVIEW", "RESTRICTED", "ARCHIVED",
}


def classify_store(store_id: str, db: str | Path | None = None) -> dict:
    init_db(db)
    profile = get_store(store_id, db)
    counts: dict[str, int] = {}
    processed = 0
    with connect(db) as con:
        products = con.execute("SELECT * FROM products WHERE archived=0 ORDER BY id")
        for row in products:
            result = classify(row, profile)
            old = con.execute(
                "SELECT final_status, manual_override, memo FROM store_product_decisions WHERE store_id=? AND product_id=?",
                (store_id, row["id"]),
            ).fetchone()
            manual_override = int(old["manual_override"]) if old else 0
            final_status = old["final_status"] if old and manual_override else result.auto_status
            memo = old["memo"] if old else ""
            con.execute(
                """
                INSERT INTO store_product_decisions(
                  store_id,product_id,fit_score,price_status,risk_status,auto_status,final_status,
                  reasons_json,manual_override,memo,classified_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(store_id,product_id) DO UPDATE SET
                  fit_score=excluded.fit_score,
                  price_status=excluded.price_status,
                  risk_status=excluded.risk_status,
                  auto_status=excluded.auto_status,
                  final_status=CASE WHEN store_product_decisions.manual_override=1
                              THEN store_product_decisions.final_status ELSE excluded.final_status END,
                  reasons_json=excluded.reasons_json,
                  classified_at=excluded.classified_at
                """,
                (
                    store_id, row["id"], result.fit_score, result.price_status, result.risk_status,
                    result.auto_status, final_status, json.dumps(result.reasons, ensure_ascii=False),
                    manual_override, memo, utc_now(),
                ),
            )
            counts[final_status] = counts.get(final_status, 0) + 1
            processed += 1
    return {"store_id": store_id, "processed": processed, "counts": counts}


def manual_override(store_id: str, asin: str, status: str, memo: str = "", db=None) -> None:
    status = status.upper()
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Unsupported decision status: {status}")
    with connect(db) as con:
        product = con.execute("SELECT id FROM products WHERE asin=?", (asin,)).fetchone()
        if not product:
            raise KeyError(f"ASIN not found: {asin}")
        row = con.execute(
            "SELECT id FROM store_product_decisions WHERE store_id=? AND product_id=?",
            (store_id, product["id"]),
        ).fetchone()
        if not row:
            raise KeyError("Product has not been classified for this store yet")
        con.execute(
            "UPDATE store_product_decisions SET final_status=?, manual_override=1, memo=?, classified_at=? WHERE id=?",
            (status, memo, utc_now(), row["id"]),
        )


def clear_manual_override(store_id: str, asin: str, db=None) -> None:
    """Remove an override; the next classification restores the automatic result."""
    with connect(db) as con:
        row = con.execute(
            """
            SELECT d.id FROM store_product_decisions d
            JOIN products p ON p.id=d.product_id
            WHERE d.store_id=? AND p.asin=?
            """,
            (store_id, asin),
        ).fetchone()
        if not row:
            raise KeyError("Product has not been classified for this store yet")
        con.execute(
            "UPDATE store_product_decisions SET manual_override=0, memo='', classified_at=? WHERE id=?",
            (utc_now(), row["id"]),
        )
