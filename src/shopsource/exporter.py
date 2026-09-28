from __future__ import annotations

import csv
import json
from pathlib import Path

from .db import connect
from .paths import EXPORT_DIR, ensure_dirs

FIELDS = [
    "store_id", "asin", "title", "brand", "price", "currency", "category", "rating",
    "review_count", "url", "source_url", "fit_score", "price_status", "risk_status",
    "auto_status", "final_status", "reasons", "memo"
]


def _rows(store_id: str, statuses: list[str] | None, db=None) -> list[dict]:
    params: list[object] = [store_id]
    where = "d.store_id=?"
    if statuses:
        marks = ",".join("?" for _ in statuses)
        where += f" AND d.final_status IN ({marks})"
        params.extend(s.upper() for s in statuses)
    sql = f"""
        SELECT d.store_id,p.asin,p.title,p.brand,p.price,p.currency,p.category,p.rating,
               p.review_count,p.url,p.source_url,d.fit_score,d.price_status,d.risk_status,
               d.auto_status,d.final_status,d.reasons_json,d.memo
        FROM store_product_decisions d
        JOIN products p ON p.id=d.product_id
        WHERE {where}
        ORDER BY d.final_status,p.price,p.asin
    """
    with connect(db) as con:
        result = []
        for r in con.execute(sql, params):
            result.append({
                "store_id": r["store_id"], "asin": r["asin"], "title": r["title"],
                "brand": r["brand"], "price": r["price"], "currency": r["currency"],
                "category": r["category"], "rating": r["rating"], "review_count": r["review_count"],
                "url": r["url"], "source_url": r["source_url"], "fit_score": r["fit_score"],
                "price_status": r["price_status"], "risk_status": r["risk_status"],
                "auto_status": r["auto_status"], "final_status": r["final_status"],
                "reasons": json.loads(r["reasons_json"] or "[]"), "memo": r["memo"],
            })
    return result


def export_store(store_id: str, fmt: str = "csv", statuses: list[str] | None = None,
                 out: str | Path | None = None, db=None) -> Path:
    ensure_dirs()
    fmt = fmt.lower()
    rows = _rows(store_id, statuses, db)
    suffix = "json" if fmt == "json" else "csv"
    target = Path(out) if out else EXPORT_DIR / f"{store_id}_products.{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        target.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    elif fmt == "csv":
        with target.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            for row in rows:
                row = dict(row)
                row["reasons"] = " | ".join(row["reasons"])
                writer.writerow(row)
    else:
        raise ValueError("fmt must be csv or json")
    return target
