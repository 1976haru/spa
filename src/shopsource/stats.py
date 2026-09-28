from __future__ import annotations

from .db import connect


def master_summary(db=None) -> dict:
    with connect(db) as con:
        products = con.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
        occurrences = con.execute("SELECT COUNT(*) c FROM product_occurrences").fetchone()["c"]
        jobs = con.execute("SELECT COUNT(DISTINCT job_id) c FROM product_occurrences WHERE job_id IS NOT NULL").fetchone()["c"]
        prices = con.execute(
            "SELECT MIN(price) mn, MAX(price) mx, AVG(price) av FROM products WHERE price IS NOT NULL"
        ).fetchone()
    return {
        "unique_products": products,
        "occurrences": occurrences,
        "duplicates_or_repeats": max(0, occurrences - products),
        "spark_jobs": jobs,
        "min_price": prices["mn"], "max_price": prices["mx"], "avg_price": prices["av"],
    }


def store_summary(store_id: str, db=None) -> dict:
    with connect(db) as con:
        rows = con.execute(
            "SELECT final_status, COUNT(*) c FROM store_product_decisions WHERE store_id=? GROUP BY final_status ORDER BY c DESC",
            (store_id,),
        ).fetchall()
        total = sum(r["c"] for r in rows)
    return {"store_id": store_id, "total": total, "counts": {r["final_status"]: r["c"] for r in rows}}
