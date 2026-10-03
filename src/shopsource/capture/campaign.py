"""Persistent, explicitly-started browser sourcing campaigns.

The service stores every search occurrence while using ASIN as campaign identity.
It never performs browser automation itself; the companion extension only starts
navigation after an operator action in the local UI.
"""
from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import secrets
import subprocess
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import datetime, timezone
from pathlib import Path

from ..connectors.spark_center_package import SparkCenterPackageService
from ..db import connect, db_path, get_store, init_db, utc_now
from ..paths import EXPORT_DIR, PROJECT_ROOT
from .batch import BatchSourcingService

LIVE_2000_NAME = "LIVE 과제 2,000개"
DEFAULT_KEYWORDS = (
    "trunk organizer", "car trunk storage", "car seat organizer",
    "backseat organizer", "center console organizer", "car console storage",
    "seat gap organizer", "seat gap filler organizer", "visor organizer",
    "glove box organizer", "car storage organizer", "car interior storage",
    "cargo organizer", "SUV organizer", "car trash organizer",
    "headrest organizer", "car cup holder organizer", "trunk storage box",
    "foldable trunk organizer", "car document organizer",
)
OPEN_STATUSES = ("DRAFT", "RUNNING", "RUNNING_SEARCH", "SEARCH_COMPLETE", "PAUSED", "PAUSED_NEEDS_USER", "CANDIDATE_TARGET_REACHED", "DETAILING", "READY_FOR_SPARK")


def _keywords(profile: dict) -> list[str]:
    dynamic = []
    for value in profile.get("sourcing", {}).get("recipes", []):
        keyword = value.get("keyword", "") if isinstance(value, dict) else str(value)
        keyword = keyword.strip()
        if keyword and keyword.casefold() not in {x.casefold() for x in dynamic}:
            dynamic.append(keyword)
    for keyword in DEFAULT_KEYWORDS:
        if keyword.casefold() not in {x.casefold() for x in dynamic}:
            dynamic.append(keyword)
    return dynamic


class CampaignService:
    def __init__(self, db=None):
        self.db = db
        init_db(db)

    def create_live_2000(self, store_id: str, target: int = 2000,
                         search_delay_seconds: int = 8, detail_interval_seconds: int = 4) -> dict:
        target = int(target)
        delay = int(search_delay_seconds)
        interval = int(detail_interval_seconds)
        if not 1 <= target <= 10000:
            raise ValueError("Target must be between 1 and 10,000.")
        if not 6 <= delay <= 30:
            raise ValueError("Live search delay must be between 6 and 30 seconds.")
        if interval < 4:
            raise ValueError("Live detail interval cannot be below 4 seconds.")
        profile = get_store(store_id, self.db)
        with connect(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT campaign_id FROM sourcing_campaigns WHERE store_id=? AND campaign_type='LIVE_2000' AND status IN (?,?,?,?,?,?,?,?,?) ORDER BY created_at DESC LIMIT 1",
                (store_id, *OPEN_STATUSES),
            ).fetchone()
            if existing:
                return self.get(existing["campaign_id"])
            campaign_id = "LC_" + secrets.token_hex(10)
            now = utc_now()
            con.execute("""INSERT INTO sourcing_campaigns
                (campaign_id,store_id,name,candidate_target,detail_target,search_delay_seconds,
                 detail_interval_seconds,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'DRAFT',?,?)""",
                (campaign_id, store_id, LIVE_2000_NAME, target, target, delay, interval, now, now))
            for position, keyword in enumerate(_keywords(profile)):
                con.execute("""INSERT INTO sourcing_campaign_keywords
                    (campaign_id,keyword,position,updated_at) VALUES(?,?,?,?)""",
                    (campaign_id, keyword, position, now))
            self._event(con, campaign_id, "CAMPAIGN_CREATED", {"preset": "LIVE_2000", "target": target})
        return self.get(campaign_id)

    def create_auto_store(self, plan_id: str) -> dict:
        """Create (or load) an executor campaign from a persisted Phase 3.0 plan."""
        from ..sourcing.planner import CategoryPlanner
        plan = CategoryPlanner(self.db).get_plan(plan_id)
        with connect(self.db) as con:
            existing = con.execute("SELECT campaign_id FROM sourcing_campaigns WHERE plan_id=? AND campaign_type='AUTO_STORE' ORDER BY created_at DESC LIMIT 1", (plan_id,)).fetchone()
            if existing:
                return self.get(existing["campaign_id"])
            campaign_id, now = "AC_" + secrets.token_hex(10), utc_now()
            settings = plan["settings"]
            con.execute("""INSERT INTO sourcing_campaigns
                (campaign_id,store_id,name,candidate_target,detail_target,search_delay_seconds,
                 detail_interval_seconds,stale_page_threshold,status,campaign_type,plan_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,'DRAFT','AUTO_STORE',?,?,?)""",
                (campaign_id, plan["store_id"], plan["name"], plan["total_candidate_target"],
                 plan["detail_target"], 8, 4, settings["stale_pages"], plan_id, now, now))
            used_categories, seen_keywords = [], set()
            for category in plan["categories"]:
                if not category["enabled"]:
                    continue
                active = []
                for keyword in category["keywords"]:
                    normalized = " ".join(keyword["keyword"].casefold().split())
                    if keyword["active_by_default"] and normalized not in seen_keywords:
                        active.append(keyword)
                        seen_keywords.add(normalized)
                if active:
                    used_categories.append({**category, "executor_keywords": active})
            if not used_categories:
                raise ValueError("Plan has no enabled active unique keywords.")
            total_quota = int(plan["total_candidate_target"])
            weights = [max(1, int(category["quota"])) for category in used_categories]
            shares = [total_quota * weight / sum(weights) for weight in weights]
            allocated = [int(value) for value in shares]
            for index in sorted(range(len(shares)), key=lambda i: (-(shares[i] - allocated[i]), i))[:total_quota-sum(allocated)]:
                allocated[index] += 1
            position = 0
            for category, category_quota in zip(used_categories, allocated):
                for keyword in category["executor_keywords"]:
                    con.execute("""INSERT INTO sourcing_campaign_keywords
                        (campaign_id,keyword,position,category_id,category_quota,max_pages,max_unique,keyword_score,historical_yield,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (campaign_id, keyword["keyword"], position, category["id"], category_quota,
                         settings["max_pages_per_keyword"], settings["max_unique_candidates_per_keyword"],
                         keyword["score"], keyword.get("historical_yield") or 0, now))
                    position += 1
            if not position:
                raise ValueError("Plan has no enabled active keywords.")
            con.execute("UPDATE store_sourcing_plans SET status='READY',updated_at=? WHERE plan_id=?", (now, plan_id))
            self._event(con, campaign_id, "AUTO_STORE_CREATED", {"plan_id": plan_id, "keyword_count": position})
        return self.get(campaign_id)

    def active(self, store_id: str, campaign_type: str = "LIVE_2000") -> dict | None:
        with connect(self.db) as con:
            row = con.execute(
                "SELECT campaign_id FROM sourcing_campaigns WHERE store_id=? AND campaign_type=? AND status IN (?,?,?,?,?,?,?,?,?) ORDER BY created_at DESC LIMIT 1",
                (store_id, campaign_type, *OPEN_STATUSES),
            ).fetchone()
        return self.get(row["campaign_id"]) if row else None

    def action(self, campaign_id: str, action: str) -> dict:
        action = str(action).upper()
        now = utc_now()
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM sourcing_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
            if not row:
                raise KeyError(campaign_id)
            if action == "START" or action == "RESUME":
                if row["status"] in {"DONE", "CANCELLED"}:
                    raise ValueError("Closed campaign cannot resume.")
                if row["campaign_type"] == "AUTO_STORE":
                    status = "DETAILING" if row["search_complete"] else "RUNNING_SEARCH"
                else:
                    status = "DETAILING" if row["unique_candidates"] >= row["candidate_target"] else "RUNNING"
                con.execute("UPDATE sourcing_campaigns SET status=?,started_at=COALESCE(started_at,?),updated_at=? WHERE campaign_id=?",
                            (status, now, now, campaign_id))
                self._event(con, campaign_id, action)
            elif action == "PAUSE":
                con.execute("UPDATE sourcing_campaigns SET status='PAUSED',updated_at=? WHERE campaign_id=?", (now, campaign_id))
                self._event(con, campaign_id, "USER_PAUSE")
            elif action == "CANCEL":
                con.execute("UPDATE sourcing_campaigns SET status='CANCELLED',finished_at=?,updated_at=? WHERE campaign_id=?", (now, now, campaign_id))
                self._event(con, campaign_id, "CANCEL")
            elif action == "CAPTCHA":
                con.execute("UPDATE sourcing_campaigns SET status='PAUSED_NEEDS_USER',updated_at=? WHERE campaign_id=?", (now, campaign_id))
                self._event(con, campaign_id, "CAPTCHA_PAUSE")
            elif action == "RETRY":
                con.execute("UPDATE sourcing_campaign_candidates SET state='NEEDS_DETAIL',failure_code='',updated_at=? WHERE campaign_id=? AND state='FAILED'", (now, campaign_id))
                con.execute("UPDATE sourcing_campaigns SET status='DETAILING',updated_at=? WHERE campaign_id=?", (now, campaign_id))
                self._event(con, campaign_id, "RETRY_FAILED")
            else:
                raise ValueError("Unsupported campaign action.")
        if action in {"START", "RESUME", "RETRY"}:
            self._ensure_detail_batch(campaign_id)
        return self.get(campaign_id)

    def record_search_capture(self, campaign_id: str, capture_run_id: str,
                              next_url: str | None, exhausted: bool = False) -> dict:
        now = utc_now()
        with connect(self.db) as con:
            campaign = con.execute("SELECT * FROM sourcing_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
            capture = con.execute("SELECT * FROM browser_capture_runs WHERE run_id=?", (capture_run_id,)).fetchone()
            if not campaign or not capture or campaign["store_id"] != capture["store_id"]:
                raise ValueError("Campaign or capture does not belong to this store.")
            running_status = "RUNNING_SEARCH" if campaign["campaign_type"] == "AUTO_STORE" else "RUNNING"
            if campaign["status"] != running_status:
                raise ValueError("Campaign search is not running.")
            keyword_row = con.execute("SELECT * FROM sourcing_campaign_keywords WHERE campaign_id=? AND keyword=? COLLATE NOCASE",
                                      (campaign_id, capture["keyword"])).fetchone()
            if not keyword_row:
                raise ValueError("Captured keyword is not in this campaign.")
            first_candidate = con.execute("SELECT search_payload_json FROM browser_capture_candidates WHERE run_id=? ORDER BY id LIMIT 1", (capture_run_id,)).fetchone()
            first_payload = json.loads(first_candidate["search_payload_json"]) if first_candidate else {}
            page_number = int(first_payload.get("_listPage") or keyword_row["current_page"] + 1)
            normalized_url = _normalized_search_url(capture["search_url"])
            seen_page = con.execute("SELECT id FROM sourcing_campaign_pages WHERE campaign_id=? AND (keyword=? AND page_number=? OR normalized_url=?)",
                                    (campaign_id, capture["keyword"], page_number, normalized_url)).fetchone()
            if seen_page:
                con.execute("UPDATE sourcing_campaigns SET search_worker_status='CONNECTED',last_search_capture_at=?,last_search_error='',current_keyword=?,current_page=? WHERE campaign_id=?",
                            (now, capture["keyword"], page_number, campaign_id))
                self._event(con, campaign_id, "SEARCH_CAPTURE_ACK_DUPLICATE", {"keyword": capture["keyword"], "page": page_number})
                duplicate_result = True
            else:
                duplicate_result = False
                con.execute("INSERT INTO sourcing_campaign_pages(campaign_id,keyword,page_number,normalized_url,capture_run_id,captured_at) VALUES(?,?,?,?,?,?)",
                            (campaign_id, capture["keyword"], page_number, normalized_url, capture_run_id, now))
            candidates = con.execute("SELECT asin FROM browser_capture_candidates WHERE run_id=? ORDER BY id", (capture_run_id,)).fetchall()
            added = duplicates = 0
            target = int(campaign["candidate_target"])
            for item in ([] if duplicate_result else candidates):
                asin = item["asin"]
                con.execute("""INSERT INTO sourcing_campaign_occurrences
                    (campaign_id,asin,keyword,search_url,page_number,captured_at) VALUES(?,?,?,?,?,?)""",
                    (campaign_id, asin, capture["keyword"], capture["search_url"], keyword_row["current_page"] + 1, now))
                exists = con.execute("SELECT 1 FROM sourcing_campaign_candidates WHERE campaign_id=? AND asin=?", (campaign_id, asin)).fetchone()
                if exists:
                    duplicates += 1
                else:
                    under_target = int(campaign["unique_candidates"]) + added < target
                    under_keyword_cap = int(keyword_row["new_candidates"]) + added < int(keyword_row["max_unique"])
                    under_category_quota = True
                    if campaign["campaign_type"] == "AUTO_STORE" and keyword_row["category_id"] is not None:
                        category_count = int(con.execute("""SELECT COUNT(*) FROM sourcing_campaign_candidates c
                            JOIN sourcing_campaign_keywords k ON k.campaign_id=c.campaign_id AND k.keyword=c.first_keyword COLLATE NOCASE
                            WHERE c.campaign_id=? AND k.category_id=?""",
                            (campaign_id, keyword_row["category_id"])).fetchone()[0])
                        under_category_quota = category_count + added < int(keyword_row["category_quota"])
                    if under_target and under_keyword_cap and under_category_quota:
                        con.execute("""INSERT INTO sourcing_campaign_candidates
                            (campaign_id,asin,capture_run_id,first_keyword,state,created_at,updated_at)
                            VALUES(?,?,?,?, 'NEEDS_DETAIL',?,?)""",
                            (campaign_id, asin, capture_run_id, capture["keyword"], now, now))
                        added += 1
            zero_pages = int(keyword_row["consecutive_zero_pages"]) if duplicate_result else (0 if added else int(keyword_row["consecutive_zero_pages"]) + 1)
            reason = ""
            if not duplicate_result:
                if exhausted: reason = "NO_NEXT_PAGE"
                elif not next_url: reason = "NO_NEXT_PAGE"
                elif zero_pages >= int(campaign["stale_page_threshold"]): reason = "STALE_PAGES"
                elif int(keyword_row["pages_captured"]) + 1 >= int(keyword_row["max_pages"]): reason = "MAX_PAGES"
                elif int(keyword_row["new_candidates"]) + added >= int(keyword_row["max_unique"]): reason = "MAX_UNIQUE"
                elif campaign["campaign_type"] == "AUTO_STORE" and keyword_row["category_id"] is not None:
                    category_count = int(con.execute("""SELECT COUNT(*) FROM sourcing_campaign_candidates c
                        JOIN sourcing_campaign_keywords k ON k.campaign_id=c.campaign_id AND k.keyword=c.first_keyword COLLATE NOCASE
                        WHERE c.campaign_id=? AND k.category_id=?""",
                        (campaign_id, keyword_row["category_id"])).fetchone()[0])
                    if category_count >= int(keyword_row["category_quota"]): reason = "CATEGORY_QUOTA"
            keyword_exhausted = bool(keyword_row["exhausted"] or reason)
            page_increment = int(not duplicate_result)
            con.execute("""UPDATE sourcing_campaign_keywords SET current_page=current_page+?,
                pages_captured=pages_captured+?,new_candidates=new_candidates+?,duplicates=duplicates+?,
                consecutive_zero_pages=?,exhausted=?,last_url=?,next_url=?,exhaustion_reason=CASE WHEN ?<>'' THEN ? ELSE exhaustion_reason END,updated_at=? WHERE id=?""",
                (page_increment, page_increment, added, duplicates, zero_pages, int(keyword_exhausted), capture["search_url"], next_url or "", reason, reason, now, keyword_row["id"]))
            if campaign["campaign_type"] == "AUTO_STORE" and not duplicate_result and keyword_row["category_id"] is not None:
                con.execute("""UPDATE store_sourcing_keywords SET
                    historical_yield=(COALESCE(historical_yield,0)*pages_used+?)/(pages_used+1),
                    duplicate_rate=(COALESCE(duplicate_rate,0)*pages_used+?)/(pages_used+1),
                    pages_used=pages_used+1,last_run_at=?,updated_at=?
                    WHERE category_id=? AND keyword=? COLLATE NOCASE""",
                    (added, duplicates / max(1, added + duplicates), now, now, keyword_row["category_id"], capture["keyword"]))
            if campaign["campaign_type"] == "AUTO_STORE" and keyword_exhausted and keyword_row["category_id"] is not None:
                category_count = int(con.execute("""SELECT COUNT(*) FROM sourcing_campaign_candidates c JOIN sourcing_campaign_keywords k
                    ON k.campaign_id=c.campaign_id AND k.keyword=c.first_keyword COLLATE NOCASE
                    WHERE c.campaign_id=? AND k.category_id=?""", (campaign_id, keyword_row["category_id"])).fetchone()[0])
                if category_count >= int(keyword_row["category_quota"]):
                    con.execute("UPDATE sourcing_campaign_keywords SET exhausted=1,exhaustion_reason='CATEGORY_QUOTA' WHERE campaign_id=? AND category_id=? AND exhausted=0",
                                (campaign_id, keyword_row["category_id"]))
                self._reallocate_exhausted_category(con, {**dict(campaign), "unique_candidates": int(campaign["unique_candidates"]) + added}, keyword_row["category_id"])
            total = int(campaign["unique_candidates"]) + added
            if total >= target:
                status = "CANDIDATE_TARGET_REACHED" if campaign["campaign_type"] != "AUTO_STORE" else "SEARCH_COMPLETE"
            elif campaign["campaign_type"] == "AUTO_STORE" and not con.execute("SELECT 1 FROM sourcing_campaign_keywords WHERE campaign_id=? AND exhausted=0 LIMIT 1", (campaign_id,)).fetchone():
                status = "SEARCH_COMPLETE"
            else: status = running_status
            complete_search = int(status == "SEARCH_COMPLETE")
            con.execute("""UPDATE sourcing_campaigns SET unique_candidates=?,duplicates=duplicates+?,search_complete=MAX(search_complete,?),
                search_pages=search_pages+?,status=?,search_worker_status='CONNECTED',current_keyword=?,current_page=?,
                last_search_capture_at=?,last_search_error='',updated_at=? WHERE campaign_id=?""",
                (total, duplicates, complete_search, int(not duplicate_result), status, capture["keyword"], page_number, now, now, campaign_id))
            if not duplicate_result:
                self._event(con, campaign_id, "SEARCH_PAGE_CAPTURED", {"keyword": capture["keyword"], "page": page_number, "added": added, "duplicates": duplicates, "exhausted": keyword_exhausted})
        if status in {"CANDIDATE_TARGET_REACHED", "SEARCH_COMPLETE"}:
            self._ensure_detail_batch(campaign_id)
        result = self.get(campaign_id)
        result["duplicate_capture"] = duplicate_result
        result["search_instruction"] = self.next_search(campaign_id, next_url if not keyword_exhausted else None)
        return result

    def next_search(self, campaign_id: str, dom_next_url: str | None = None) -> dict | None:
        campaign = self.get(campaign_id)
        running_status = "RUNNING_SEARCH" if campaign["campaign_type"] == "AUTO_STORE" else "RUNNING"
        if campaign["status"] != running_status or campaign["unique_candidates"] >= campaign["candidate_target"]:
            return None
        current = self._next_keyword(campaign)
        if not current:
            return None
        if dom_next_url:
            active_keyword = next((item for item in campaign["keywords"]
                                   if item["keyword"].casefold() == campaign.get("current_keyword", "").casefold()
                                   and not item["exhausted"]), current)
            return {"campaign_id": campaign_id, "keyword": active_keyword["keyword"], "url": dom_next_url,
                    "delay_seconds": campaign["search_delay_seconds"]}
        if current.get("next_url"):
            return {"campaign_id": campaign_id, "keyword": current["keyword"], "url": current["next_url"],
                    "delay_seconds": campaign["search_delay_seconds"]}
        from urllib.parse import quote_plus
        return {"campaign_id": campaign_id, "keyword": current["keyword"],
                "url": "https://www.amazon.com/s?k=" + quote_plus(current["keyword"]),
                "delay_seconds": campaign["search_delay_seconds"]}

    @staticmethod
    def _next_keyword(campaign: dict) -> dict | None:
        available = [k for k in campaign["keywords"] if not k["exhausted"]]
        if not available: return None
        if campaign.get("campaign_type") != "AUTO_STORE": return available[0]
        category_stats = {}
        for row in available:
            category_stats.setdefault(row["category_id"], {"quota": row["category_quota"], "new": 0, "weight": 0.0})
        for row in campaign["keywords"]:
            if row["category_id"] in category_stats:
                stat = category_stats[row["category_id"]]
                stat["new"] += int(row["new_candidates"])
                stat["weight"] += max(.01, float(row["keyword_score"] or 0))
        categories = [(cid, stat) for cid, stat in category_stats.items() if stat["new"] < stat["quota"]]
        if not categories: categories = list(category_stats.items())
        category_id, _ = max(categories, key=lambda pair: ((pair[1]["quota"]-pair[1]["new"])/max(1,pair[1]["quota"]), pair[1]["weight"]))
        return min((k for k in available if k["category_id"] == category_id), key=lambda k: (k["pages_captured"], k["position"]))

    def _reallocate_exhausted_category(self, con, campaign: dict, exhausted_category_id: int) -> None:
        remaining = max(0, int(campaign["candidate_target"]) - int(campaign["unique_candidates"]))
        if remaining <= 0:
            return
        still_active = con.execute("SELECT 1 FROM sourcing_campaign_keywords WHERE campaign_id=? AND category_id=? AND exhausted=0 LIMIT 1",
                                   (campaign["campaign_id"], exhausted_category_id)).fetchone()
        if still_active:
            return
        previous = con.execute("SELECT MAX(category_quota) quota FROM sourcing_campaign_keywords WHERE campaign_id=? AND category_id=?",
                               (campaign["campaign_id"], exhausted_category_id)).fetchone()["quota"] or 0
        collected = con.execute("""SELECT COUNT(*) FROM sourcing_campaign_candidates c JOIN sourcing_campaign_keywords k
            ON k.campaign_id=c.campaign_id AND k.keyword=c.first_keyword COLLATE NOCASE
            WHERE c.campaign_id=? AND k.category_id=?""",
            (campaign["campaign_id"], exhausted_category_id)).fetchone()[0]
        shortfall = max(0, int(previous) - int(collected))
        if not shortfall:
            return
        con.execute("UPDATE sourcing_campaign_keywords SET category_quota=? WHERE campaign_id=? AND category_id=?",
                    (int(collected), campaign["campaign_id"], exhausted_category_id))
        rows = con.execute("""SELECT category_id,MAX(category_quota) quota,
            SUM(MAX(0,max_unique-new_candidates)) capacity,AVG(keyword_score) relevance,
            AVG(historical_yield) historical_yield
            FROM sourcing_campaign_keywords WHERE campaign_id=? AND exhausted=0 AND category_id<>?
            GROUP BY category_id""", (campaign["campaign_id"], exhausted_category_id)).fetchall()
        if not rows:
            return
        weights = []
        for row in rows:
            capacity = max(1, int(row["capacity"] or 0))
            weight = capacity * max(.1, float(row["relevance"] or .1)) * (1 + min(1, float(row["historical_yield"] or 0) / 100))
            weights.append(weight)
        total_weight = sum(weights)
        additions = [int(shortfall * weight / total_weight) for weight in weights]
        for index in sorted(range(len(weights)), key=lambda i: (-(shortfall * weights[i] / total_weight - additions[i]), i))[:shortfall-sum(additions)]:
            additions[index] += 1
        for row, addition in zip(rows, additions):
            new_quota = int(row["quota"]) + addition
            con.execute("UPDATE sourcing_campaign_keywords SET category_quota=? WHERE campaign_id=? AND category_id=?",
                        (new_quota, campaign["campaign_id"], row["category_id"]))
        self._event(con, campaign["campaign_id"], "CATEGORY_QUOTA_REALLOCATED",
                    {"from_category_id": exhausted_category_id, "shortfall": shortfall,
                     "allocations": {str(row["category_id"]): value for row, value in zip(rows, additions)}})

    def _ensure_detail_batch(self, campaign_id: str) -> None:
        campaign = self.get(campaign_id)
        if campaign["unique_candidates"] < campaign["candidate_target"] and not campaign["search_complete"]:
            return
        batch_id = campaign.get("batch_run_id")
        batch = BatchSourcingService(self.db)
        if not batch_id:
            created = batch.create(campaign["store_id"], f"campaign:{campaign_id}", campaign["detail_target"], True)
            batch_id = created["run_id"]
            with connect(self.db) as con:
                con.execute("UPDATE sourcing_campaigns SET batch_run_id=?,status='DETAILING',updated_at=? WHERE campaign_id=?", (batch_id, utc_now(), campaign_id))
        with connect(self.db) as con:
            rows = con.execute("SELECT asin FROM sourcing_campaign_candidates WHERE campaign_id=? AND state='NEEDS_DETAIL' ORDER BY id LIMIT ?",
                               (campaign_id, campaign["detail_target"])).fetchall()
        batch.queue_existing_candidates(batch_id, [row["asin"] for row in rows])
        try:
            if batch.get(batch_id)["status"] != "RUNNING":
                batch.action(batch_id, "RESUME")
        except ValueError:
            pass

    def get(self, campaign_id: str) -> dict:
        self._sync(campaign_id)
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM sourcing_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
            if not row:
                raise KeyError(campaign_id)
            result = dict(row)
            result["keywords"] = [dict(x) for x in con.execute("SELECT * FROM sourcing_campaign_keywords WHERE campaign_id=? ORDER BY position", (campaign_id,)).fetchall()]
            result["events"] = [dict(x) for x in con.execute("SELECT * FROM sourcing_campaign_events WHERE campaign_id=? ORDER BY id DESC LIMIT 100", (campaign_id,)).fetchall()]
            batch = con.execute("SELECT status,detail_complete,failed_count FROM browser_batch_runs WHERE run_id=?",
                                (result.get("batch_run_id"),)).fetchone() if result.get("batch_run_id") else None
            current_detail = con.execute("SELECT asin,state,last_error FROM browser_batch_items WHERE batch_run_id=? AND state='DETAIL_OPENED' ORDER BY priority,id LIMIT 1",
                                         (result.get("batch_run_id"),)).fetchone() if result.get("batch_run_id") else None
            last_detail_success = con.execute("SELECT updated_at FROM browser_batch_items WHERE batch_run_id=? AND state IN ('DETAIL_COMPLETE','MASTER_IMPORTED') ORDER BY updated_at DESC,id DESC LIMIT 1",
                                              (result.get("batch_run_id"),)).fetchone() if result.get("batch_run_id") else None
            package_row = con.execute("""SELECT campaign_total,campaign_exportable,campaign_excluded,
                campaign_excluded_by_status_json,campaign_missing_decision,campaign_missing_product
                FROM export_runs WHERE package_id=?""", (result.get("spark_package_id"),)).fetchone() if result.get("spark_package_id") else None
        result["search_stage_complete"] = (
            (int(result["unique_candidates"]) >= int(result["candidate_target"]) or result["search_complete"])
            and result["status"] in {"CANDIDATE_TARGET_REACHED", "SEARCH_COMPLETE", "DETAILING", "READY_FOR_SPARK", "DONE"}
        )
        result["detail_worker_status"] = batch["status"] if batch else ("NOT_STARTED" if result["search_stage_complete"] else "WAITING")
        result["detail_current_asin"] = current_detail["asin"] if current_detail else ""
        result["detail_last_error"] = current_detail["last_error"] if current_detail else ""
        result["detail_last_success_at"] = last_detail_success["updated_at"] if last_detail_success else None
        result["detail_failure_count"] = int(batch["failed_count"]) if batch else int(result.get("failed", 0))
        result["campaign_package_summary"] = ({
            "campaign_total": package_row["campaign_total"],
            "campaign_exportable": package_row["campaign_exportable"],
            "campaign_excluded": package_row["campaign_excluded"],
            "campaign_excluded_by_status": json.loads(package_row["campaign_excluded_by_status_json"] or "{}"),
            "campaign_missing_decision": package_row["campaign_missing_decision"],
            "campaign_missing_product": package_row["campaign_missing_product"],
        } if package_row and package_row["campaign_total"] is not None else None)
        remaining = max(0, int(result["detail_target"]) - int(result["detail_complete"]))
        result["minimum_remaining_seconds"] = remaining * int(result["detail_interval_seconds"])
        return result

    def record_extension_event(self, campaign_id: str, event_name: str, payload: dict | None = None) -> dict:
        allowed = {"SEARCH_WORKER_CREATED", "SEARCH_WORKER_REUSED", "SEARCH_CAPTURE_TRIGGERED",
                   "SEARCH_CAPTURE_ACK", "SEARCH_CAPTURE_ERROR", "SEARCH_RESULTS_NOT_READY", "SEARCH_CAPTCHA",
                   "SEARCH_WORKER_MISSING"}
        event_name = str(event_name or "").upper()
        if event_name not in allowed:
            raise ValueError("Unsupported campaign search event.")
        data = payload if isinstance(payload, dict) else {}
        keyword = str(data.get("keyword") or "")[:300]
        try: page = max(0, int(data.get("page") or 0))
        except (TypeError, ValueError): page = 0
        error = str(data.get("error") or event_name).replace("\n", " ")[:500]
        now = utc_now()
        worker_status = "CONNECTED"
        if event_name in {"SEARCH_CAPTURE_ERROR", "SEARCH_RESULTS_NOT_READY", "SEARCH_WORKER_MISSING"}: worker_status = "ERROR"
        if event_name == "SEARCH_CAPTCHA": worker_status = "CAPTCHA"
        with connect(self.db) as con:
            if not con.execute("SELECT 1 FROM sourcing_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone():
                raise KeyError(campaign_id)
            con.execute("""UPDATE sourcing_campaigns SET search_worker_status=?,
                current_keyword=CASE WHEN ?='' THEN current_keyword ELSE ? END,
                current_page=CASE WHEN ?>0 THEN ? ELSE current_page END,
                last_search_capture_at=CASE WHEN ?='SEARCH_CAPTURE_ACK' THEN ? ELSE last_search_capture_at END,
                last_search_error=CASE WHEN ? IN ('SEARCH_CAPTURE_ERROR','SEARCH_RESULTS_NOT_READY','SEARCH_WORKER_MISSING','SEARCH_CAPTCHA') THEN ? ELSE '' END,
                updated_at=? WHERE campaign_id=?""",
                (worker_status, keyword, keyword, page, page, event_name, now, event_name, error, now, campaign_id))
            self._event(con, campaign_id, event_name, {"keyword": keyword, "page": page,
                                                       "error": error if worker_status in {"ERROR", "CAPTCHA"} else ""})
            if event_name == "SEARCH_CAPTCHA":
                con.execute("UPDATE sourcing_campaigns SET status='PAUSED_NEEDS_USER' WHERE campaign_id=?", (campaign_id,))
        return self.get(campaign_id)

    def _sync(self, campaign_id: str) -> None:
        with connect(self.db) as con:
            campaign = con.execute("SELECT * FROM sourcing_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
            if not campaign or not campaign["batch_run_id"]:
                return
            batch = con.execute("SELECT * FROM browser_batch_runs WHERE run_id=?", (campaign["batch_run_id"],)).fetchone()
            if not batch:
                return
            con.execute("""UPDATE sourcing_campaign_candidates SET state=(SELECT i.state FROM browser_batch_items i
                WHERE i.batch_run_id=? AND i.asin=sourcing_campaign_candidates.asin),updated_at=?
                WHERE campaign_id=? AND EXISTS(SELECT 1 FROM browser_batch_items i WHERE i.batch_run_id=? AND i.asin=sourcing_campaign_candidates.asin)""",
                (campaign["batch_run_id"], utc_now(), campaign_id, campaign["batch_run_id"]))
            classified = con.execute("""SELECT COUNT(*) FROM sourcing_campaign_candidates c JOIN products p ON p.asin=c.asin
                JOIN store_product_decisions d ON d.product_id=p.id AND d.store_id=? WHERE c.campaign_id=?""", (campaign["store_id"], campaign_id)).fetchone()[0]
            status = campaign["status"]
            if batch["status"] in {"PAUSED", "PAUSED_NEEDS_USER"}: status = batch["status"]
            elif int(batch["master_imported"]) >= int(campaign["detail_target"]): status = "READY_FOR_SPARK"
            con.execute("""UPDATE sourcing_campaigns SET detail_complete=?,master_imported=?,classified=?,failed=?,status=?,updated_at=? WHERE campaign_id=?""",
                (batch["detail_complete"] + batch["master_imported"], batch["master_imported"], classified, batch["failed_count"], status, utc_now(), campaign_id))

    def create_package(self, campaign_id: str, out_root=None) -> dict:
        # Package generation must not trigger _sync(), which reconciles detail
        # batch state back into campaign candidate rows as a side effect.
        with connect(self.db) as con:
            campaign_row = con.execute("SELECT * FROM sourcing_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
        if campaign_row is None:
            raise KeyError(campaign_id)
        campaign = dict(campaign_row)
        safe_statuses = ["PRIMARY", "RESERVE_A", "RESERVE_B", "RESERVE_C", "LOW_RESERVE", "HIGH_RESERVE", "REVIEW"]
        with connect(self.db) as con:
            rows = con.execute("""SELECT c.asin,p.id AS product_id,d.final_status
                FROM sourcing_campaign_candidates c
                LEFT JOIN products p ON p.asin=c.asin
                LEFT JOIN store_product_decisions d ON d.product_id=p.id AND d.store_id=?
                WHERE c.campaign_id=? ORDER BY c.id""", (campaign["store_id"], campaign_id)).fetchall()
        exportable: list[str] = []
        excluded_by_status: dict[str, int] = {}
        missing_product = missing_decision = 0
        for row in rows:
            if row["product_id"] is None:
                missing_product += 1
            elif row["final_status"] is None:
                missing_decision += 1
            elif row["final_status"] in safe_statuses:
                exportable.append(row["asin"])
            else:
                status = str(row["final_status"])
                excluded_by_status[status] = excluded_by_status.get(status, 0) + 1
        campaign_total = len(rows)
        excluded = campaign_total - len(exportable)
        summary = {
            "campaign_total": campaign_total,
            "campaign_exportable": len(exportable),
            "campaign_excluded": excluded,
            "campaign_excluded_by_status": dict(sorted(excluded_by_status.items())),
            "campaign_missing_decision": missing_decision,
            "campaign_missing_product": missing_product,
        }
        if not exportable:
            raise ValueError(
                "Campaign has no safe exportable products. "
                f"Campaign total {campaign_total}; excluded {excluded}; "
                f"missing product {missing_product}; missing decision {missing_decision}; "
                f"excluded by status {summary['campaign_excluded_by_status']}."
            )
        result = SparkCenterPackageService().create(
            store_id=campaign["store_id"], statuses=safe_statuses, limit=len(exportable),
            asins=exportable, out_root=out_root, db=self.db, campaign_summary=summary,
        )
        with connect(self.db) as con:
            con.execute("UPDATE sourcing_campaigns SET spark_package_id=?,updated_at=? WHERE campaign_id=?", (result.package_id, utc_now(), campaign_id))
        return {**result.to_dict(), **summary}

    def record_outcome(self, campaign_id: str, *, spark_dataset_id: str | None = None,
                       spark_total: int | None = None, spark_included: int | None = None,
                       spark_excluded: int | None = None, verified_product_count: int | None = None,
                       shopify_upload_result: str | None = None,
                       shopify_uploaded_count: int | None = None, notes: str = "") -> dict:
        counts = [spark_total, spark_included, spark_excluded, verified_product_count, shopify_uploaded_count]
        if any(value is not None and int(value) < 0 for value in counts):
            raise ValueError("Counts cannot be negative.")
        if spark_total is not None and spark_included is not None and spark_excluded is not None and int(spark_included) + int(spark_excluded) != int(spark_total):
            raise ValueError("Spark included + excluded must equal Spark total.")
        attempted = utc_now() if shopify_upload_result is not None else None
        with connect(self.db) as con:
            con.execute("""UPDATE sourcing_campaigns SET spark_dataset_id=COALESCE(?,spark_dataset_id),
                spark_total=COALESCE(?,spark_total),spark_included=COALESCE(?,spark_included),spark_excluded=COALESCE(?,spark_excluded),
                spark_desktop_roundtrip_verified=CASE WHEN ? IS NULL THEN spark_desktop_roundtrip_verified ELSE 1 END,
                verified_product_count=COALESCE(?,verified_product_count),shopify_upload_attempted_at=COALESCE(?,shopify_upload_attempted_at),
                shopify_upload_result=COALESCE(?,shopify_upload_result),shopify_uploaded_count=COALESCE(?,shopify_uploaded_count),notes=?,updated_at=?
                WHERE campaign_id=?""", (spark_dataset_id, spark_total, spark_included, spark_excluded,
                verified_product_count, verified_product_count, attempted, shopify_upload_result,
                shopify_uploaded_count, str(notes)[:2000], utc_now(), campaign_id))
        return self.get(campaign_id)

    def preflight(self, campaign_id: str) -> dict:
        campaign = self.get(campaign_id)
        source = db_path(self.db)
        backup_dir = PROJECT_ROOT / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = backup_dir / f"pre_live_2000_{stamp}.sqlite3"
        with sqlite3.connect(source) as src, sqlite3.connect(backup) as dst:
            src.backup(dst)
        free = shutil.disk_usage(source.parent).free
        result = {"database_backup": str(backup), "database_backup_created": backup.is_file(),
                  "disk_free_bytes": free, "store_profile_loaded": bool(get_store(campaign["store_id"], self.db)),
                  "active_campaign_same_id": self.active(campaign["store_id"])["campaign_id"] == campaign_id,
                  "extension_required_version": "0.1.7", "extension_health": "CHECK_IN_UI",
                  "spark_staging_connector": "AVAILABLE"}
        with connect(self.db) as con:
            self._event(con, campaign_id, "PREFLIGHT", result)
        return result

    def report(self, campaign_id: str, commit: str = "unknown") -> dict:
        campaign = self.get(campaign_id)
        folder = EXPORT_DIR / "assignment_reports" / campaign_id
        folder.mkdir(parents=True, exist_ok=True)
        with connect(self.db) as con:
            classes = {row["final_status"]: row["count"] for row in con.execute("""SELECT d.final_status,COUNT(*) count
                FROM sourcing_campaign_candidates c JOIN products p ON p.asin=c.asin
                JOIN store_product_decisions d ON d.product_id=p.id AND d.store_id=?
                WHERE c.campaign_id=? GROUP BY d.final_status""", (campaign["store_id"], campaign_id)).fetchall()}
            keywords = [dict(row) for row in con.execute("""SELECT k.keyword,k.pages_captured,k.new_candidates,k.duplicates,k.exhausted,
                k.exhaustion_reason,k.category_quota,c.category_name
                FROM sourcing_campaign_keywords k LEFT JOIN store_sourcing_categories c ON c.id=k.category_id
                WHERE k.campaign_id=? ORDER BY k.position""", (campaign_id,)).fetchall()]
            package = con.execute("""SELECT output_path,campaign_total,campaign_exportable,campaign_excluded,
                campaign_excluded_by_status_json,campaign_missing_decision,campaign_missing_product
                FROM export_runs WHERE package_id=?""", (campaign.get("spark_package_id"),)).fetchone() if campaign.get("spark_package_id") else None
        if commit == "unknown":
            try:
                commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True,
                                        capture_output=True, check=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        summary = {key: campaign.get(key) for key in ("campaign_id", "name", "store_id", "started_at", "finished_at", "unique_candidates", "duplicates", "detail_complete", "master_imported", "classified", "failed", "spark_package_id", "spark_dataset_id", "spark_total", "spark_included", "spark_excluded", "shopify_upload_result", "shopify_uploaded_count")}
        if campaign.get("campaign_type") == "AUTO_STORE":
            summary["category_quotas"] = {row["category_name"]: row["category_quota"] for row in keywords if row.get("category_name")}
            summary["category_results"] = {
                name: {"quota": quota, "actual": sum(int(row["new_candidates"]) for row in keywords if row.get("category_name") == name)}
                for name, quota in summary["category_quotas"].items()
            }
        try:
            started = datetime.fromisoformat(str(campaign.get("started_at") or "").replace("Z", "+00:00"))
            ended_value = campaign.get("finished_at") or campaign.get("updated_at")
            ended = datetime.fromisoformat(str(ended_value or "").replace("Z", "+00:00"))
            summary["elapsed_seconds"] = max(0, int((ended - started).total_seconds()))
        except (TypeError, ValueError):
            summary["elapsed_seconds"] = None
        summary.update({"classification_counts": classes, "package_path": package["output_path"] if package else None,
                        "commit": commit, "app_version": "0.1.0"})
        if package and package["campaign_total"] is not None:
            summary["campaign_package_summary"] = {
                "campaign_total": package["campaign_total"],
                "campaign_exportable": package["campaign_exportable"],
                "campaign_excluded": package["campaign_excluded"],
                "campaign_excluded_by_status": json.loads(package["campaign_excluded_by_status_json"] or "{}"),
                "campaign_missing_decision": package["campaign_missing_decision"],
                "campaign_missing_product": package["campaign_missing_product"],
            }
        (folder / "assignment_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [f"# {campaign['name']}", "", f"- Campaign: {campaign_id}", f"- Store: {campaign['store_id']}",
                 f"- Unique candidates: {campaign['unique_candidates']}", f"- Duplicates: {campaign['duplicates']}",
                 f"- Detail complete: {campaign['detail_complete']}", f"- MASTER: {campaign['master_imported']}",
                 f"- Classified: {campaign['classified']}", f"- Failures: {campaign['failed']}",
                 f"- Campaign products / Spark exportable / excluded: {summary.get('campaign_package_summary', {}).get('campaign_total', 'not packaged')} / {summary.get('campaign_package_summary', {}).get('campaign_exportable', 'not packaged')} / {summary.get('campaign_package_summary', {}).get('campaign_excluded', 'not packaged')}",
                 f"- Spark total / included / excluded: {campaign['spark_total']} / {campaign['spark_included']} / {campaign['spark_excluded']}",
                 f"- Shopify result / uploaded: {campaign['shopify_upload_result']} / {campaign['shopify_uploaded_count']}",
                 f"- Package: {campaign['spark_package_id'] or 'not created'}", f"- Commit/app: {commit} / 0.1.0", ""]
        if campaign.get("campaign_type") == "AUTO_STORE":
            lines.insert(-1, f"- Category quota / actual: {summary.get('category_results', {})}")
        lines.insert(-1, f"- Elapsed seconds: {summary.get('elapsed_seconds')}")
        for status, count in summary.get("campaign_package_summary", {}).get("campaign_excluded_by_status", {}).items():
            lines.insert(-1, f"- Campaign excluded {status}: {count}")
        if summary.get("campaign_package_summary"):
            lines.insert(-1, f"- Campaign missing decision / product: {summary['campaign_package_summary']['campaign_missing_decision']} / {summary['campaign_package_summary']['campaign_missing_product']}")
        (folder / "assignment_summary.md").write_text("\n".join(lines), encoding="utf-8")
        with (folder / "sourcing_counts.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["category_name", "category_quota", "keyword", "pages_captured", "new_candidates", "duplicates", "exhausted", "exhaustion_reason"])
            writer.writeheader(); writer.writerows(keywords)
        return {"folder": str(folder), "summary": summary}

    @staticmethod
    def _event(con, campaign_id: str, event_type: str, payload: dict | None = None) -> None:
        con.execute("INSERT INTO sourcing_campaign_events(campaign_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                    (campaign_id, event_type, json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")), utc_now()))


def _normalized_search_url(value: str) -> str:
    parts = urlsplit(value)
    excluded = {"ref", "qid", "sr", "sprefix", "crid", "dib"}
    query = urlencode(sorted((key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
                             if key.casefold() not in excluded))
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path, query, ""))
