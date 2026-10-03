from datetime import datetime, timezone

from shopsource.db import connect, init_db
from shopsource.source_safety import SourceSafetyService, SourceMonitorService


def _seed(path, products, snapshots):
    init_db(path); safety=SourceSafetyService(path); now=datetime.now(timezone.utc).isoformat()
    with connect(path) as con:
        con.executemany("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                        ((f"S{i:09d}",f"Product {i}","{}",now,now) for i in range(products)))
        con.executemany("""INSERT INTO source_product_snapshots(product_id,asin,source_platform,source_kind,source_price,source_currency,
            availability,availability_confidence,observed_at,evidence_kind,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (((i % products)+1,f"S{i%products:09d}","AMAZON","FIXTURE",10+(i%7),"USD","IN_STOCK","HIGH",now,"FIXTURE","{}",now)
             for i in range(snapshots)))
    return safety


def test_2000_product_source_audit_batches_real(tmp_path):
    path=tmp_path/"audit.sqlite3"; _seed(path,2000,0)
    preview=SourceMonitorService(path).preview_due_checks("001",limit=2000)
    assert len(preview["items"])==2000 and preview["estimated_batches"]==20


def test_10000_latest_sellability_summary_linear(tmp_path):
    safety=_seed(tmp_path/"sellability.sqlite3",10000,10000)
    rows=safety.bulk_latest_snapshots()
    assert len(rows)==10000 and all(r["availability"]=="IN_STOCK" for r in rows)


def test_50000_snapshot_latest_lookup_indexed(tmp_path):
    safety=_seed(tmp_path/"history.sqlite3",10000,50000)
    assert len(safety.bulk_latest_snapshots())==10000
    with connect(safety.db) as con:
        plan=" ".join(str(tuple(r)) for r in con.execute("EXPLAIN QUERY PLAN SELECT * FROM source_product_snapshots WHERE product_id=? ORDER BY observed_at DESC LIMIT 1",(1,)))
    assert "idx_source_snapshots_product_observed" in plan

