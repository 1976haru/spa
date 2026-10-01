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
OPEN_STATUSES = ("DRAFT", "RUNNING", "PAUSED", "PAUSED_NEEDS_USER", "CANDIDATE_TARGET_REACHED", "DETAILING", "READY_FOR_SPARK")


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
                "SELECT campaign_id FROM sourcing_campaigns WHERE store_id=? AND status IN (?,?,?,?,?,?,?) ORDER BY created_at DESC LIMIT 1",
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

    def active(self, store_id: str) -> dict | None:
        with connect(self.db) as con:
            row = con.execute(
                "SELECT campaign_id FROM sourcing_campaigns WHERE store_id=? AND status IN (?,?,?,?,?,?,?) ORDER BY created_at DESC LIMIT 1",
                (store_id, *OPEN_STATUSES),
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
            if campaign["status"] != "RUNNING":
                raise ValueError("Campaign search is not running.")
            keyword_row = con.execute("SELECT * FROM sourcing_campaign_keywords WHERE campaign_id=? AND keyword=? COLLATE NOCASE",
                                      (campaign_id, capture["keyword"])).fetchone()
            if not keyword_row:
                raise ValueError("Captured keyword is not in this campaign.")
            candidates = con.execute("SELECT asin FROM browser_capture_candidates WHERE run_id=? ORDER BY id", (capture_run_id,)).fetchall()
            added = duplicates = 0
            target = int(campaign["candidate_target"])
            for item in candidates:
                asin = item["asin"]
                con.execute("""INSERT INTO sourcing_campaign_occurrences
                    (campaign_id,asin,keyword,search_url,page_number,captured_at) VALUES(?,?,?,?,?,?)""",
                    (campaign_id, asin, capture["keyword"], capture["search_url"], keyword_row["current_page"] + 1, now))
                exists = con.execute("SELECT 1 FROM sourcing_campaign_candidates WHERE campaign_id=? AND asin=?", (campaign_id, asin)).fetchone()
                if exists:
                    duplicates += 1
                elif int(campaign["unique_candidates"]) + added < target:
                    con.execute("""INSERT INTO sourcing_campaign_candidates
                        (campaign_id,asin,capture_run_id,first_keyword,state,created_at,updated_at)
                        VALUES(?,?,?,?, 'NEEDS_DETAIL',?,?)""",
                        (campaign_id, asin, capture_run_id, capture["keyword"], now, now))
                    added += 1
            zero_pages = 0 if added else int(keyword_row["consecutive_zero_pages"]) + 1
            keyword_exhausted = bool(exhausted or not next_url or zero_pages >= int(campaign["stale_page_threshold"]))
            con.execute("""UPDATE sourcing_campaign_keywords SET current_page=current_page+1,
                pages_captured=pages_captured+1,new_candidates=new_candidates+?,duplicates=duplicates+?,
                consecutive_zero_pages=?,exhausted=?,last_url=?,updated_at=? WHERE id=?""",
                (added, duplicates, zero_pages, int(keyword_exhausted), capture["search_url"], now, keyword_row["id"]))
            total = int(campaign["unique_candidates"]) + added
            status = "CANDIDATE_TARGET_REACHED" if total >= target else "RUNNING"
            con.execute("""UPDATE sourcing_campaigns SET unique_candidates=?,duplicates=duplicates+?,
                search_pages=search_pages+1,status=?,updated_at=? WHERE campaign_id=?""",
                (total, duplicates, status, now, campaign_id))
            self._event(con, campaign_id, "SEARCH_PAGE_CAPTURED", {"keyword": capture["keyword"], "added": added, "duplicates": duplicates, "exhausted": keyword_exhausted})
        if status == "CANDIDATE_TARGET_REACHED":
            self._ensure_detail_batch(campaign_id)
        result = self.get(campaign_id)
        result["search_instruction"] = self.next_search(campaign_id, next_url if not keyword_exhausted else None)
        return result

    def next_search(self, campaign_id: str, dom_next_url: str | None = None) -> dict | None:
        campaign = self.get(campaign_id)
        if campaign["status"] != "RUNNING" or campaign["unique_candidates"] >= campaign["candidate_target"]:
            return None
        current = next((x for x in campaign["keywords"] if not x["exhausted"]), None)
        if not current:
            return None
        if dom_next_url and current["pages_captured"]:
            return {"campaign_id": campaign_id, "keyword": current["keyword"], "url": dom_next_url,
                    "delay_seconds": campaign["search_delay_seconds"]}
        from urllib.parse import quote_plus
        return {"campaign_id": campaign_id, "keyword": current["keyword"],
                "url": "https://www.amazon.com/s?k=" + quote_plus(current["keyword"]),
                "delay_seconds": campaign["search_delay_seconds"]}

    def _ensure_detail_batch(self, campaign_id: str) -> None:
        campaign = self.get(campaign_id)
        if campaign["unique_candidates"] < campaign["candidate_target"]:
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
        remaining = max(0, int(result["detail_target"]) - int(result["detail_complete"]))
        result["minimum_remaining_seconds"] = remaining * int(result["detail_interval_seconds"])
        return result

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
        campaign = self.get(campaign_id)
        with connect(self.db) as con:
            rows = con.execute("""SELECT c.asin FROM sourcing_campaign_candidates c JOIN products p ON p.asin=c.asin
                JOIN store_product_decisions d ON d.product_id=p.id AND d.store_id=?
                WHERE c.campaign_id=? ORDER BY c.id""", (campaign["store_id"], campaign_id)).fetchall()
        asins = [x["asin"] for x in rows]
        if not asins:
            raise ValueError("Campaign has no MASTER imported and classified products.")
        safe_statuses = ["PRIMARY", "RESERVE_A", "RESERVE_B", "RESERVE_C", "LOW_RESERVE", "HIGH_RESERVE", "REVIEW"]
        result = SparkCenterPackageService().create(store_id=campaign["store_id"], statuses=safe_statuses, limit=len(asins), asins=asins, out_root=out_root, db=self.db)
        with connect(self.db) as con:
            con.execute("UPDATE sourcing_campaigns SET spark_package_id=?,updated_at=? WHERE campaign_id=?", (result.package_id, utc_now(), campaign_id))
        return result.to_dict()

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
                  "extension_required_version": "0.1.5", "extension_health": "CHECK_IN_UI",
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
            keywords = [dict(row) for row in con.execute("SELECT keyword,pages_captured,new_candidates,duplicates,exhausted FROM sourcing_campaign_keywords WHERE campaign_id=? ORDER BY position", (campaign_id,)).fetchall()]
            package = con.execute("SELECT output_path FROM export_runs WHERE package_id=?", (campaign.get("spark_package_id"),)).fetchone() if campaign.get("spark_package_id") else None
        if commit == "unknown":
            try:
                commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True,
                                        capture_output=True, check=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        summary = {key: campaign.get(key) for key in ("campaign_id", "name", "store_id", "started_at", "finished_at", "unique_candidates", "duplicates", "detail_complete", "master_imported", "classified", "failed", "spark_package_id", "spark_dataset_id", "spark_total", "spark_included", "spark_excluded", "shopify_upload_result", "shopify_uploaded_count")}
        summary.update({"classification_counts": classes, "package_path": package["output_path"] if package else None,
                        "commit": commit, "app_version": "0.1.0"})
        (folder / "assignment_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [f"# {campaign['name']}", "", f"- Campaign: {campaign_id}", f"- Store: {campaign['store_id']}",
                 f"- Unique candidates: {campaign['unique_candidates']}", f"- Duplicates: {campaign['duplicates']}",
                 f"- Detail complete: {campaign['detail_complete']}", f"- MASTER: {campaign['master_imported']}",
                 f"- Classified: {campaign['classified']}", f"- Failures: {campaign['failed']}",
                 f"- Spark total / included / excluded: {campaign['spark_total']} / {campaign['spark_included']} / {campaign['spark_excluded']}",
                 f"- Shopify result / uploaded: {campaign['shopify_upload_result']} / {campaign['shopify_uploaded_count']}",
                 f"- Package: {campaign['spark_package_id'] or 'not created'}", f"- Commit/app: {commit} / 0.1.0", ""]
        (folder / "assignment_summary.md").write_text("\n".join(lines), encoding="utf-8")
        with (folder / "sourcing_counts.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["keyword", "pages_captured", "new_candidates", "duplicates", "exhausted"])
            writer.writeheader(); writer.writerows(keywords)
        return {"folder": str(folder), "summary": summary}

    @staticmethod
    def _event(con, campaign_id: str, event_type: str, payload: dict | None = None) -> None:
        con.execute("INSERT INTO sourcing_campaign_events(campaign_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                    (campaign_id, event_type, json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")), utc_now()))
