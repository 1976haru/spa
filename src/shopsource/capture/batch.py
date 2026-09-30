"""Persistent, user-started browser detail queue (no page traversal)."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from ..db import connect, get_store, init_db, utc_now
from .validation import ASIN_RE, _amazon_url

class BatchSourcingService:
    def __init__(self, db=None):
        self.db = db
        init_db(db)

    def create(self, store_id: str, keyword: str, target_candidates: int = 50,
               auto_import_master: bool = True, target_mode: str = "CANDIDATES") -> dict:
        keyword = str(keyword or "").strip()[:300]
        if not keyword:
            raise ValueError("A search keyword is required.")
        if isinstance(target_candidates, bool) or int(target_candidates) < 1 or int(target_candidates) > 10000:
            raise ValueError("Target must be between 1 and 10,000.")
        target_mode = str(target_mode).upper()
        if target_mode not in {"CANDIDATES", "PRIMARY"}:
            raise ValueError("Invalid target mode.")
        get_store(store_id, self.db)
        run_id = "BB_" + secrets.token_hex(10)
        now = utc_now()
        with connect(self.db) as con:
            con.execute("""INSERT INTO browser_batch_runs
                (run_id,store_id,keyword,target_candidates,target_mode,status,auto_import_master,created_at)
                VALUES(?,?,?,?,?,'PAUSED_NEEDS_USER',?,?)""",
                (run_id, str(store_id), keyword, int(target_candidates), target_mode,
                 int(bool(auto_import_master)), now))
            self._event(con, run_id, "RUN_CREATED", {"target": int(target_candidates), "mode": target_mode})
        return self.get(run_id)

    def add_search_capture(self, batch_run_id: str, capture_run_id: str) -> dict:
        now = utc_now()
        with connect(self.db) as con:
            run = con.execute("SELECT * FROM browser_batch_runs WHERE run_id=?", (batch_run_id,)).fetchone()
            capture = con.execute("SELECT * FROM browser_capture_runs WHERE run_id=?", (capture_run_id,)).fetchone()
            if not run or not capture or run["store_id"] != capture["store_id"]:
                raise ValueError("Batch or capture run was not found for this store.")
            if run["status"] in {"CANCELLED", "DONE"}:
                raise ValueError("This batch is already closed.")
            profile = get_store(run["store_id"], self.db)
            excludes = [str(x).casefold() for x in profile.get("exclude_keywords", [])]
            risks = profile.get("risk_rules", [])
            found = con.execute("SELECT * FROM browser_capture_candidates WHERE run_id=? ORDER BY id", (capture_run_id,)).fetchall()
            seen = added = rejected = 0
            already = con.execute("SELECT COUNT(*) FROM browser_batch_items WHERE batch_run_id=?", (batch_run_id,)).fetchone()[0]
            available = max(0, int(run["target_candidates"]) - already)
            for candidate in found:
                seen += 1
                if con.execute("SELECT 1 FROM browser_batch_items WHERE batch_run_id=? AND asin=?", (batch_run_id, candidate["asin"])).fetchone():
                    continue
                if added >= available:
                    continue
                payload = json.loads(candidate["search_payload_json"])
                title = str(payload.get("title") or "")
                text = title.casefold()
                master = con.execute("SELECT 1 FROM products WHERE asin=?", (candidate["asin"],)).fetchone()
                if master:
                    state, priority = "DUPLICATE", 0
                elif not _amazon_url(payload.get("url")):
                    state, priority = "FAILED", 100
                elif title and any(term and term in text for term in excludes):
                    state, priority = "PREFILTER_REJECTED", 0
                else:
                    state = "DETAIL_PENDING"
                    # Price/risk are signals, never deletion criteria.
                    flagged = any(any(str(term).casefold() in text for term in rule.get("terms", [])) for rule in risks)
                    priority = 20 if flagged else 10 if payload.get("price") is not None else 30
                con.execute("""INSERT INTO browser_batch_items
                    (batch_run_id,asin,capture_run_id,priority,state,last_error,completeness_score,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (batch_run_id, candidate["asin"], capture_run_id, priority, state,
                     "Missing valid Amazon product URL" if state == "FAILED" else "",
                     candidate["completeness_score"], now, now))
                added += 1
                if state == "PREFILTER_REJECTED": rejected += 1
            waiting_for_page = run["status"] == "PAUSED_NEEDS_USER" and "insufficient" in str(run["error"]).lower()
            next_status = "RUNNING" if waiting_for_page or run["status"] in {"PENDING", "RUNNING"} else run["status"]
            con.execute("""UPDATE browser_batch_runs SET status=?,started_at=COALESCE(started_at,?),
                total_seen=total_seen+?,deduped=deduped+?,prefiltered=prefiltered+?,checkpoint_json=? WHERE run_id=?""",
                (next_status, now, seen, max(0, seen - added), rejected,
                 json.dumps({"last_capture_run_id": capture_run_id}, separators=(",", ":")), batch_run_id))
            if waiting_for_page:
                con.execute("UPDATE browser_batch_runs SET error='' WHERE run_id=?", (batch_run_id,))
            self._event(con, batch_run_id, "SEARCH_CAPTURE", {"capture_run_id": capture_run_id, "added": added, "duplicates": max(0, seen-added)})
        self._refresh(batch_run_id)
        result = self.get(batch_run_id)
        result["last_capture_added"] = added
        result["last_capture_duplicates"] = max(0, seen - added)
        return result

    def attach_latest(self, store_id: str, keyword: str, capture_run_id: str) -> dict | None:
        with connect(self.db) as con:
            row = con.execute("""SELECT run_id FROM browser_batch_runs WHERE store_id=? AND keyword=?
                AND status IN ('PENDING','RUNNING','PAUSED_NEEDS_USER') ORDER BY created_at DESC LIMIT 1""",
                (str(store_id), str(keyword or "").strip()[:300])).fetchone()
        return self.add_search_capture(row["run_id"], capture_run_id) if row else None

    def queue_existing_candidates(self, batch_run_id: str, asins: list[str] | None = None) -> dict:
        with connect(self.db) as con:
            run = con.execute("SELECT * FROM browser_batch_runs WHERE run_id=?", (batch_run_id,)).fetchone()
            if not run: raise KeyError(batch_run_id)
            params: list = [run["store_id"]]
            sql = """SELECT c.run_id,c.asin,c.completeness_score,c.search_payload_json FROM browser_capture_candidates c
                JOIN browser_capture_runs r ON r.run_id=c.run_id WHERE r.store_id=? AND c.capture_status='NEEDS_DETAIL'"""
            if asins:
                sql += " AND c.asin IN (" + ",".join("?" for _ in asins) + ")"
                params.extend([str(a).upper() for a in asins])
            sql += " ORDER BY c.completeness_score DESC,c.updated_at DESC LIMIT ?"
            params.append(int(run["target_candidates"]))
            rows = con.execute(sql, params).fetchall()
            now = utc_now()
            for row in rows:
                payload = json.loads(row["search_payload_json"])
                state = "DETAIL_PENDING" if _amazon_url(payload.get("url")) else "FAILED"
                con.execute("""INSERT OR IGNORE INTO browser_batch_items
                    (batch_run_id,asin,capture_run_id,priority,state,last_error,completeness_score,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (batch_run_id,row["asin"],row["run_id"],10,state,
                    "Missing valid Amazon product URL" if state == "FAILED" else "",row["completeness_score"],now,now))
            self._event(con,batch_run_id,"ITEM_QUEUED",{"count":len(rows),"source":"existing_candidates"})
        self._refresh(batch_run_id)
        return self.get(batch_run_id)

    def next_item(self, run_id: str, interval_seconds: int = 4) -> dict | None:
        """Reserve one item; callers must open it only after explicit batch start."""
        with connect(self.db) as con:
            run = con.execute("SELECT * FROM browser_batch_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or run["status"] != "RUNNING":
                return None
            cp = json.loads(run["checkpoint_json"] or "{}")
            now = utc_now()
            if cp.get("next_open_after") and cp["next_open_after"] > now:
                return None
            # Single active item at a time; no tab fan-out.
            opened = con.execute("SELECT * FROM browser_batch_items WHERE batch_run_id=? AND state='DETAIL_OPENED' ORDER BY id LIMIT 1", (run_id,)).fetchone()
            if opened:
                stale_before = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(timespec="seconds")
                if opened["updated_at"] >= stale_before:
                    return None
                if int(opened["retry_count"]) < 2:
                    con.execute("UPDATE browser_batch_items SET state='DETAIL_PENDING',retry_count=retry_count+1,last_error='Detail capture timed out during resume',updated_at=? WHERE id=?", (now, opened["id"]))
                    self._event(con,run_id,"RETRY",{"asin":opened["asin"],"reason":"stale_open_item"})
                else:
                    con.execute("UPDATE browser_batch_items SET state='FAILED',last_error='Detail capture timed out during resume',updated_at=? WHERE id=?", (now, opened["id"]))
                    self._event(con,run_id,"FAIL",{"asin":opened["asin"],"reason":"stale_open_item"})
            row = con.execute("""SELECT i.*,c.search_payload_json FROM browser_batch_items i
                JOIN browser_capture_candidates c ON c.run_id=i.capture_run_id AND c.asin=i.asin
                WHERE i.batch_run_id=? AND i.state='DETAIL_PENDING' ORDER BY i.priority,i.id LIMIT 1""", (run_id,)).fetchone()
            if not row:
                return None
            payload = json.loads(row["search_payload_json"])
            url = payload.get("url")
            if not _amazon_url(url):
                con.execute("UPDATE browser_batch_items SET state='FAILED',last_error='Invalid Amazon product URL',updated_at=? WHERE id=?", (now, row["id"]))
                self._event(con, run_id, "FAIL", {"asin": row["asin"], "reason": "invalid_url"})
                return None
            cp["next_open_after"] = now  # next slot is set on completion below
            con.execute("UPDATE browser_batch_items SET state='DETAIL_OPENED',updated_at=? WHERE id=?", (now, row["id"]))
            con.execute("UPDATE browser_batch_runs SET checkpoint_json=? WHERE run_id=?", (json.dumps(cp), run_id))
            self._event(con, run_id, "DETAIL_OPENED", {"asin": row["asin"]})
            return {"asin": row["asin"], "url": url, "batch_run_id": run_id}

    def record_detail(self, run_id: str, asin: str, status: str, error: str = "", interval_seconds: int = 4) -> dict:
        asin = str(asin or "").upper()
        if not ASIN_RE.fullmatch(asin):
            raise ValueError("Invalid ASIN.")
        with connect(self.db) as con:
            item = con.execute("SELECT * FROM browser_batch_items WHERE batch_run_id=? AND asin=?", (run_id, asin)).fetchone()
            run = con.execute("SELECT * FROM browser_batch_runs WHERE run_id=?", (run_id,)).fetchone()
            if not item or not run:
                raise ValueError("Batch item was not found.")
            now = utc_now()
            if status == "CAPTCHA":
                con.execute("UPDATE browser_batch_items SET state='DETAIL_PENDING',last_error='User action required: CAPTCHA/robot check',updated_at=? WHERE id=?", (now, item["id"]))
                con.execute("UPDATE browser_batch_runs SET status='PAUSED_NEEDS_USER',paused_at=?,error='Amazon confirmation page detected' WHERE run_id=?", (now, run_id))
                self._event(con, run_id, "CAPTCHA_PAUSE", {"asin": asin})
            elif status == "DETAIL_COMPLETE":
                con.execute("UPDATE browser_batch_items SET state='DETAIL_COMPLETE',last_error='',updated_at=? WHERE id=?", (now, item["id"]))
                cp = json.loads(run["checkpoint_json"] or "{}")
                from datetime import datetime, timedelta, timezone
                cp["next_open_after"] = (datetime.now(timezone.utc) + timedelta(seconds=max(3, min(30, int(interval_seconds))))).isoformat(timespec="seconds")
                con.execute("UPDATE browser_batch_runs SET checkpoint_json=?,error='' WHERE run_id=?", (json.dumps(cp), run_id))
                self._event(con, run_id, "DETAIL_COMPLETE", {"asin": asin})
            else:
                retries = int(item["retry_count"])
                if retries < 2:
                    con.execute("UPDATE browser_batch_items SET state='DETAIL_PENDING',retry_count=retry_count+1,last_error=?,updated_at=? WHERE id=?", (str(error)[:250], now, item["id"]))
                    self._event(con, run_id, "RETRY", {"asin": asin, "retry": retries + 1})
                else:
                    con.execute("UPDATE browser_batch_items SET state='FAILED',last_error=?,updated_at=? WHERE id=?", (str(error)[:250], now, item["id"]))
                    self._event(con, run_id, "FAIL", {"asin": asin})
        self._refresh(run_id)
        result = self.get(run_id)
        if status == "DETAIL_COMPLETE" and result.get("auto_import_master"):
            self._import_ready(run_id)
            result = self.get(run_id)
        return result

    def action(self, run_id: str, action: str) -> dict:
        action = str(action).upper()
        now = utc_now()
        with connect(self.db) as con:
            run = con.execute("SELECT * FROM browser_batch_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError(run_id)
            if action == "PAUSE":
                if run["status"] in {"DONE", "CANCELLED"}: raise ValueError("Closed batch cannot be paused.")
                con.execute("UPDATE browser_batch_runs SET status='PAUSED',paused_at=? WHERE run_id=?", (now, run_id))
                self._event(con, run_id, "USER_PAUSE")
            elif action == "RESUME":
                if run["status"] not in {"PAUSED", "PAUSED_NEEDS_USER", "PENDING"}: raise ValueError("Batch is not paused.")
                con.execute("UPDATE browser_batch_runs SET status='RUNNING',started_at=COALESCE(started_at,?),paused_at=NULL,error='' WHERE run_id=?", (now, run_id))
                self._event(con, run_id, "RESUME")
            elif action == "CANCEL":
                con.execute("UPDATE browser_batch_runs SET status='CANCELLED',finished_at=? WHERE run_id=?", (now, run_id))
                self._event(con, run_id, "CANCEL")
            elif action == "RETRY":
                con.execute("UPDATE browser_batch_items SET state='DETAIL_PENDING',retry_count=0,last_error='',updated_at=? WHERE batch_run_id=? AND state='FAILED'", (now, run_id))
                con.execute("UPDATE browser_batch_runs SET status='RUNNING',finished_at=NULL,error='' WHERE run_id=?", (run_id,))
                self._event(con, run_id, "RETRY", {"scope": "failed"})
            else:
                raise ValueError("Unsupported batch action.")
        self._refresh(run_id)
        return self.get(run_id)

    def pause_for_captcha(self, run_id: str) -> dict:
        with connect(self.db) as con:
            row = con.execute("SELECT run_id FROM browser_batch_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row: raise KeyError(run_id)
            con.execute("UPDATE browser_batch_runs SET status='PAUSED_NEEDS_USER',paused_at=?,error='Amazon confirmation page detected' WHERE run_id=?", (utc_now(),run_id))
            self._event(con,run_id,"CAPTCHA_PAUSE")
        return self.get(run_id)

    def pause_latest_for_keyword(self, store_id: str, keyword: str) -> dict | None:
        with connect(self.db) as con:
            row = con.execute("SELECT run_id FROM browser_batch_runs WHERE store_id=? AND keyword=? AND status IN ('PENDING','RUNNING') ORDER BY created_at DESC LIMIT 1",(str(store_id),str(keyword or "").strip()[:300])).fetchone()
        return self.pause_for_captcha(row["run_id"]) if row else None

    def get(self, run_id: str) -> dict:
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM browser_batch_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row: raise KeyError(run_id)
            result = dict(row)
            result["auto_import_master"] = bool(result["auto_import_master"])
            result["checkpoint"] = json.loads(result.pop("checkpoint_json") or "{}")
            result["items"] = [dict(x) for x in con.execute("SELECT * FROM browser_batch_items WHERE batch_run_id=? ORDER BY updated_at DESC,id DESC LIMIT 20", (run_id,)).fetchall()]
            result["events"] = [dict(x) for x in con.execute("SELECT * FROM browser_batch_events WHERE batch_run_id=? ORDER BY id DESC LIMIT 50", (run_id,)).fetchall()]
        return result

    def active(self, store_id: str | None = None) -> list[dict]:
        with connect(self.db) as con:
            if store_id:
                rows = con.execute("SELECT run_id FROM browser_batch_runs WHERE store_id=? AND status IN ('PENDING','RUNNING','PAUSED','PAUSED_NEEDS_USER') ORDER BY created_at DESC", (store_id,)).fetchall()
            else:
                rows = con.execute("SELECT run_id FROM browser_batch_runs WHERE status IN ('PENDING','RUNNING','PAUSED','PAUSED_NEEDS_USER') ORDER BY created_at DESC").fetchall()
        return [self.get(row["run_id"]) for row in rows]

    def _import_ready(self, run_id: str) -> None:
        from .service import CaptureService
        with connect(self.db) as con:
            run = con.execute("SELECT store_id FROM browser_batch_runs WHERE run_id=?", (run_id,)).fetchone()
            rows = con.execute("SELECT asin FROM browser_batch_items WHERE batch_run_id=? AND state='DETAIL_COMPLETE' ORDER BY id LIMIT 20", (run_id,)).fetchall()
        if not run or not rows: return
        try:
            result = CaptureService(self.db).import_candidates(run["store_id"], [x["asin"] for x in rows])
        except ValueError as exc:
            self._set_item_error(run_id, rows[0]["asin"], str(exc))
            return
        with connect(self.db) as con:
            con.execute("UPDATE browser_batch_items SET state='MASTER_IMPORTED',updated_at=? WHERE batch_run_id=? AND state='DETAIL_COMPLETE' AND asin IN (SELECT asin FROM products)", (utc_now(), run_id))
            self._event(con, run_id, "MASTER_IMPORTED", {"count": len(rows), "inserted": result.get("inserted", 0), "updated": result.get("updated", 0)})
        self._refresh(run_id)

    def _set_item_error(self, run_id: str, asin: str, error: str) -> None:
        with connect(self.db) as con:
            con.execute("UPDATE browser_batch_items SET last_error=? WHERE batch_run_id=? AND asin=?", (error[:250], run_id, asin))

    @staticmethod
    def _event(con, run_id: str, event_type: str, payload: dict | None = None):
        con.execute("INSERT INTO browser_batch_events(batch_run_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                    (run_id, event_type, json.dumps(payload or {}, separators=(",", ":")), utc_now()))

    def _refresh(self, run_id: str) -> None:
        with connect(self.db) as con:
            run = con.execute("SELECT * FROM browser_batch_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run: return
            states = {row["state"]: row["n"] for row in con.execute("SELECT state,COUNT(*) n FROM browser_batch_items WHERE batch_run_id=? GROUP BY state", (run_id,))}
            imported = states.get("MASTER_IMPORTED", 0)
            primary = reserve = review = restricted = 0
            if imported:
                row = con.execute("""SELECT SUM(CASE WHEN d.final_status='PRIMARY' THEN 1 ELSE 0 END) primary_n,
                    SUM(CASE WHEN d.final_status LIKE 'RESERVE_%' OR d.final_status IN ('LOW_RESERVE','HIGH_RESERVE') THEN 1 ELSE 0 END) reserve_n,
                    SUM(CASE WHEN d.final_status='REVIEW' THEN 1 ELSE 0 END) review_n,
                    SUM(CASE WHEN d.final_status='RESTRICTED' THEN 1 ELSE 0 END) restricted_n
                    FROM browser_batch_items i JOIN products p ON p.asin=i.asin
                    LEFT JOIN store_product_decisions d ON d.product_id=p.id AND d.store_id=?
                    WHERE i.batch_run_id=? AND i.state='MASTER_IMPORTED'""", (run["store_id"], run_id)).fetchone()
                primary, reserve = int(row["primary_n"] or 0), int(row["reserve_n"] or 0)
                review, restricted = int(row["review_n"] or 0), int(row["restricted_n"] or 0)
            pending = states.get("DETAIL_PENDING", 0) + states.get("DETAIL_OPENED", 0)
            complete = states.get("DETAIL_COMPLETE", 0) + imported
            failed = states.get("FAILED", 0)
            item_count = sum(states.values())
            checkpoint = json.loads(run["checkpoint_json"] or "{}")
            has_work = item_count > 0 or bool(checkpoint.get("last_capture_run_id"))
            goal_met = run["target_mode"] == "PRIMARY" and primary >= run["target_candidates"]
            terminal = goal_met or (has_work and pending == 0 and complete + failed + states.get("PREFILTER_REJECTED", 0) + states.get("DUPLICATE", 0) >= run["target_candidates"])
            if has_work and not terminal and pending == 0 and not con.execute("SELECT 1 FROM browser_batch_items WHERE batch_run_id=? AND state='DETAIL_PENDING'", (run_id,)).fetchone() and run["status"] == "RUNNING":
                terminal = True
            processed = complete + failed + states.get("PREFILTER_REJECTED", 0) + states.get("DUPLICATE", 0)
            shortage = has_work and pending == 0 and not goal_met and (
                (run["target_mode"] == "PRIMARY" and primary < run["target_candidates"]) or
                (run["target_mode"] == "CANDIDATES" and processed < run["target_candidates"]))
            status = "PAUSED_NEEDS_USER" if shortage and run["status"] == "RUNNING" else "DONE" if terminal and run["status"] == "RUNNING" else run["status"]
            finished = utc_now() if status == "DONE" else run["finished_at"]
            error = "Current captured candidates were insufficient; capture another Amazon search page." if shortage else ("" if status == "DONE" else run["error"])
            con.execute("""UPDATE browser_batch_runs SET detail_pending=?,detail_complete=?,master_imported=?,
                primary_count=?,reserve_count=?,review_count=?,restricted_count=?,failed_count=?,status=?,finished_at=?,error=? WHERE run_id=?""",
                (pending, complete, imported, primary, reserve, review, restricted, failed, status, finished, error, run_id))
            if status == "DONE" and run["status"] != "DONE": self._event(con, run_id, "DONE")
        
