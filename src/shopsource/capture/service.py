from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..classifier import classify_store
from ..db import connect, get_store, init_db, utc_now
from ..importer import import_products
from ..connectors.amazon_source_folder import AmazonSourceFolderConnector
from ..sourcing.mapping import browser_capture_to_spark_payload
from .models import completeness_score
from .validation import _amazon_url, canonical_product_url, sensitive_paths, validate_product


class CaptureService:
    def __init__(self, db=None):
        self.db = db
        init_db(db)

    def create_pairing_code(self) -> str:
        code = secrets.token_urlsafe(18)
        digest = hashlib.sha256(code.encode()).hexdigest()
        with connect(self.db) as con:
            con.execute("INSERT INTO app_settings(setting_key,setting_value,updated_at) VALUES('capture_pairing_hash',?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=excluded.updated_at", (digest, utc_now()))
        return code

    def authenticate(self, code: str | None) -> bool:
        if not code:
            return False
        digest = hashlib.sha256(code.encode()).hexdigest()
        with connect(self.db) as con:
            row = con.execute("SELECT setting_value FROM app_settings WHERE setting_key='capture_pairing_hash'").fetchone()
        return bool(row and secrets.compare_digest(row["setting_value"], digest))

    def capture_search(self, body: dict) -> dict:
        if sensitive_paths(body):
            raise ValueError("민감정보로 보이는 필드가 있어 캡처를 거부했습니다.")
        store_id = str(body.get("store_id") or "").strip()
        get_store(store_id, self.db)
        products = body.get("products")
        if not isinstance(products, list) or not products:
            raise ValueError("Amazon 검색결과를 찾지 못했습니다.")
        if len(products) > 100:
            raise ValueError("한 번에 100개를 초과하는 후보는 캡처할 수 없습니다.")
        run_id = "BC_" + secrets.token_hex(10)
        now = utc_now()
        keyword = str(body.get("keyword") or "")[:300]
        search_url = str(body.get("search_url") or "")[:2000]
        if not _amazon_url(search_url):
            raise ValueError("Amazon 검색 주소가 올바르지 않습니다.")
        accepted = 0
        with connect(self.db) as con:
            con.execute("INSERT INTO browser_capture_runs(run_id,store_id,keyword,search_url,status,captured_at,candidates) VALUES(?,?,?,?,?,?,0)",
                        (run_id, store_id, keyword, search_url, "SEARCH_CAPTURED", now))
            for index, item in enumerate(products):
                product = validate_product(item, allow_missing_title=True)
                # Search-card hrefs (especially sponsored placements) may be redirects or absent.
                # The ASIN is the identity; always persist a direct product URL for detail opening.
                product["url"] = canonical_product_url(product["asin"])
                product.setdefault("_sourceUrl", search_url)
                product.setdefault("_listPage", body.get("page_number"))
                product.setdefault("_collectedAt", body.get("captured_at") or now)
                product.setdefault("url", None); product.setdefault("price", None)
                product.setdefault("images", []); product.setdefault("rating", None); product.setdefault("reviewCount", None)
                raw = json.dumps(product, ensure_ascii=False, separators=(",", ":"))
                previous = con.execute("SELECT id,seen_count,keywords_json,search_urls_json FROM browser_capture_candidates WHERE run_id=? AND asin=?", (run_id, product["asin"])).fetchone()
                if previous:
                    con.execute("UPDATE browser_capture_candidates SET search_payload_json=?,seen_count=seen_count+1,updated_at=? WHERE id=?", (raw, now, previous["id"]))
                else:
                    con.execute("INSERT INTO browser_capture_candidates(run_id,asin,search_payload_json,completeness_score,capture_status,keywords_json,search_urls_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                (run_id, product["asin"], raw, completeness_score(product), "NEEDS_DETAIL", json.dumps([keyword], ensure_ascii=False), json.dumps([search_url], ensure_ascii=False), now, now))
                    accepted += 1
            con.execute("UPDATE browser_capture_runs SET candidates=? WHERE run_id=?", (accepted, run_id))
        return {"run_id": run_id, "candidates": accepted, "status": "SEARCH_CAPTURED"}

    def capture_detail(self, body: dict) -> dict:
        if sensitive_paths(body):
            raise ValueError("민감정보로 보이는 필드가 있어 캡처를 거부했습니다.")
        store_id = str(body.get("store_id") or "").strip()
        get_store(store_id, self.db)
        payload = validate_product(body.get("product"), detail=True)
        payload.setdefault("_collectedAt", utc_now())
        batch_run_id = str(body.get("batch_run_id") or "").strip()
        if batch_run_id:
            with connect(self.db) as con:
                queued = con.execute("""SELECT i.state,r.store_id FROM browser_batch_items i
                    JOIN browser_batch_runs r ON r.run_id=i.batch_run_id
                    WHERE i.batch_run_id=? AND i.asin=?""", (batch_run_id, payload["asin"])).fetchone()
            if not queued or queued["store_id"] != store_id or queued["state"] != "DETAIL_OPENED":
                with connect(self.db) as con:
                    opened = con.execute("SELECT asin FROM browser_batch_items WHERE batch_run_id=? AND state='DETAIL_OPENED' ORDER BY id LIMIT 1", (batch_run_id,)).fetchone()
                    if opened:
                        now = utc_now()
                        reason = "Opened product ASIN did not match queued ASIN"
                        con.execute("UPDATE browser_batch_items SET state='FAILED',last_error=?,updated_at=? WHERE batch_run_id=? AND asin=?",
                                    (reason, now, batch_run_id, opened["asin"]))
                        from .batch import BatchSourcingService
                        BatchSourcingService._event(con, batch_run_id, "FAIL", {"asin": opened["asin"], "reason": "asin_mismatch"})
                if opened:
                    from .batch import BatchSourcingService
                    BatchSourcingService(self.db)._refresh(batch_run_id)
                raise ValueError("Opened product ASIN did not match queued ASIN")
        with connect(self.db) as con:
            preferred = con.execute("SELECT capture_run_id FROM browser_batch_items WHERE batch_run_id=? AND asin=?", (batch_run_id, payload["asin"])).fetchone() if batch_run_id else None
            if preferred:
                row = con.execute("SELECT c.id,c.search_payload_json,c.run_id FROM browser_capture_candidates c WHERE c.run_id=? AND c.asin=?", (preferred["capture_run_id"], payload["asin"])).fetchone()
            else:
                row = con.execute("SELECT c.id,c.search_payload_json,c.run_id FROM browser_capture_candidates c JOIN browser_capture_runs r ON r.run_id=c.run_id WHERE r.store_id=? AND c.asin=? ORDER BY c.updated_at DESC LIMIT 1", (store_id, payload["asin"])).fetchone()
            if not row:
                # A user may open a detail page directly; preserve it as a small capture run.
                run_id = "BC_" + secrets.token_hex(10)
                con.execute("INSERT INTO browser_capture_runs(run_id,store_id,keyword,search_url,status,captured_at,candidates) VALUES(?,?,?,?,?,?,1)",
                            (run_id, store_id, "", payload.get("url", ""), "DETAIL_CAPTURED", utc_now()))
                search_raw = json.dumps({"asin": payload["asin"], "title": payload["title"], "url": payload.get("url"), "images": []}, ensure_ascii=False)
                cur = con.execute("INSERT INTO browser_capture_candidates(run_id,asin,search_payload_json,created_at,updated_at) VALUES(?,?,?,?,?)", (run_id, payload["asin"], search_raw, utc_now(), utc_now()))
                candidate_id = cur.lastrowid
            else:
                candidate_id = row["id"]; run_id = row["run_id"]
            score = completeness_score(payload)
            con.execute("UPDATE browser_capture_candidates SET detail_payload_json=?,completeness_score=?,capture_status='DETAIL_COMPLETE',updated_at=? WHERE id=?",
                        (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), score, utc_now(), candidate_id))
            con.execute("UPDATE browser_capture_runs SET detailed=(SELECT COUNT(*) FROM browser_capture_candidates WHERE run_id=? AND capture_status='DETAIL_COMPLETE'),status='DETAIL_CAPTURED' WHERE run_id=?", (run_id, run_id))
        result = {"run_id": run_id, "asin": payload["asin"], "completeness_score": score, "status": "DETAIL_COMPLETE"}
        if batch_run_id:
            from .batch import BatchSourcingService
            result["batch"] = BatchSourcingService(self.db).record_detail(batch_run_id, payload["asin"], "DETAIL_COMPLETE")
        return result

    def list_candidates(self, store_id: str, limit: int = 100) -> list[dict]:
        with connect(self.db) as con:
            rows = con.execute("SELECT c.*,r.keyword,r.search_url,r.captured_at FROM browser_capture_candidates c JOIN browser_capture_runs r ON r.run_id=c.run_id WHERE r.store_id=? ORDER BY c.updated_at DESC LIMIT ?", (store_id, min(max(limit, 1), 500))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["search_payload"] = json.loads(item.pop("search_payload_json"))
            item["detail_payload"] = json.loads(item.pop("detail_payload_json")) if item.get("detail_payload_json") else None
            result.append(item)
        return result

    def import_candidates(self, store_id: str, asins: list[str]) -> dict:
        if not asins or len(set(asins)) > 100:
            raise ValueError("MASTER로 보낼 상품을 1~100개 선택하세요.")
        placeholders = ",".join("?" for _ in asins)
        with connect(self.db) as con:
            rows = con.execute(f"""SELECT c.id,c.run_id,c.asin,c.detail_payload_json
                FROM browser_capture_candidates c JOIN browser_capture_runs r ON r.run_id=c.run_id
                WHERE r.store_id=? AND c.asin IN ({placeholders}) AND c.capture_status='DETAIL_COMPLETE'
                  AND c.id=(SELECT c2.id FROM browser_capture_candidates c2
                    JOIN browser_capture_runs r2 ON r2.run_id=c2.run_id
                    WHERE r2.store_id=r.store_id AND c2.asin=c.asin AND c2.capture_status='DETAIL_COMPLETE'
                    ORDER BY c2.updated_at DESC,c2.id DESC LIMIT 1)
                ORDER BY c.asin""", (store_id, *[a.upper() for a in asins])).fetchall()
        if not rows:
            raise ValueError("상세정보가 완료된 선택 상품이 없습니다.")
        # Feed validated canonical records through the existing transactional importer.
        with tempfile.TemporaryDirectory(prefix="shopsource_capture_") as temp:
            root = Path(temp)
            for index, row in enumerate(rows, 1):
                (root / f"{row['run_id']}_{index:09}.json").write_text(row["detail_payload_json"], encoding="utf-8")
            result = import_products(root, AmazonSourceFolderConnector(), "BROWSER_CAPTURE", self.db, allow_reimport=True)
        with connect(self.db) as con:
            con.execute("UPDATE products SET source='amazon_browser',source_kind='BROWSER_CAPTURE' WHERE asin IN (" + placeholders + ")", [a.upper() for a in asins])
            con.execute("UPDATE product_occurrences SET source_kind='BROWSER_CAPTURE' WHERE product_id IN (SELECT id FROM products WHERE asin IN (" + placeholders + "))", [a.upper() for a in asins])
            con.execute(f"UPDATE browser_capture_candidates SET capture_status='MASTER_IMPORTED',updated_at=? WHERE asin IN ({placeholders}) AND run_id IN (SELECT run_id FROM browser_capture_runs WHERE store_id=?)", (utc_now(), *[a.upper() for a in asins], store_id))
            con.execute(f"""
                UPDATE browser_capture_runs SET
                  completed=(SELECT COUNT(*) FROM browser_capture_candidates c WHERE c.run_id=browser_capture_runs.run_id AND c.capture_status='MASTER_IMPORTED'),
                  status=CASE WHEN EXISTS(SELECT 1 FROM browser_capture_candidates c WHERE c.run_id=browser_capture_runs.run_id AND c.capture_status!='MASTER_IMPORTED') THEN 'PARTIAL' ELSE 'MASTER_IMPORTED' END
                WHERE store_id=? AND run_id IN (SELECT DISTINCT run_id FROM browser_capture_candidates WHERE asin IN ({placeholders}))
            """, (store_id, *[a.upper() for a in asins]))
        classified = classify_store(store_id, self.db)
        return {**result, "classified": classified}

    def log_error(self, action: str, message: str, store_id: str | None = None) -> None:
        # Store only a bounded human-readable error, never the submitted payload.
        safe_message = str(message).replace("\n", " ")[:500]
        with connect(self.db) as con:
            con.execute("INSERT INTO browser_capture_errors(store_id,action,error_message,created_at) VALUES(?,?,?,?)",
                        (store_id, action[:40], safe_message, utc_now()))
