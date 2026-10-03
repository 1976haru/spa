"""Durable single-store launch orchestration and sanitized final reports."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .db import connect, init_db
from .paths import EXPORT_DIR

STAGES = ("PLAN", "BRAND_PLAN", "BRAND_ASSET_PREVIEW", "BRAND_ASSET_GENERATION", "BRAND_ASSET_APPROVAL",
          "SOURCING", "SOURCE_VALIDATION", "PRODUCT_SYNC_PREVIEW", "PRODUCT_SYNC", "PRODUCT_VERIFY",
          "COLLECTION_PLAN", "COLLECTION_IMAGE", "COLLECTION_SYNC_PREVIEW", "COLLECTION_SYNC", "COLLECTION_VERIFY",
          "BRAND_APPLY_PREVIEW", "BRAND_APPLY", "HOMEPAGE_PLAN", "FINAL_VERIFY", "COMPLETE")
STAGE_STATES = {"PENDING", "RUNNING", "COMPLETE", "COMPLETE_WITH_WARNINGS", "PAUSED", "FAILED", "MANUAL_ACTION_REQUIRED", "SKIPPED"}


def _now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(value): return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _sanitize(value):
    if isinstance(value, dict): return {key: _sanitize(item) for key, item in value.items()}
    if isinstance(value, list): return [_sanitize(item) for item in value]
    if isinstance(value, str): return re.sub(r"shpat_[A-Za-z0-9]+|(?:access|api)[_-]?token\s*[:=]\s*[^\s,;]+", "[REDACTED]", value, flags=re.I)[:500]
    return value


class StoreBuildOrchestrator:
    def __init__(self, *, db=None, export_dir: str | Path | None = None, handlers: dict[str, Callable] | None = None):
        self.db, self.export_dir, self.handlers = db, Path(export_dir) if export_dir else EXPORT_DIR, handlers or {}
        init_db(db)
        with connect(db) as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS store_build_runs(
              run_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,status TEXT NOT NULL,mode TEXT NOT NULL,
              provider TEXT NOT NULL,stage TEXT NOT NULL,options_json TEXT NOT NULL,checkpoint_json TEXT NOT NULL DEFAULT '{}',
              counts_json TEXT NOT NULL DEFAULT '{}',stage_data_json TEXT NOT NULL DEFAULT '{}',input_hash TEXT NOT NULL,
              last_error TEXT NOT NULL DEFAULT '',retry_count INTEGER NOT NULL DEFAULT 0,started_at TEXT,finished_at TEXT,
              created_at TEXT NOT NULL,updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS store_build_stage_runs(
              run_id TEXT NOT NULL,stage TEXT NOT NULL,status TEXT NOT NULL,counts_json TEXT NOT NULL DEFAULT '{}',
              output_hash TEXT NOT NULL DEFAULT '',last_error TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,
              PRIMARY KEY(run_id,stage)
            );
            """)

    def _input_hash(self, store_id, options):
        with connect(self.db) as con:
            cursor = con.execute("SELECT p.id,p.asin,p.source,p.source_kind,p.title,p.brand,p.category,p.price,p.raw_json,p.last_seen_at,p.archived,d.final_status,d.manual_override FROM products p LEFT JOIN store_product_decisions d ON d.product_id=p.id AND d.store_id=? ORDER BY p.id", (store_id,))
            digest = hashlib.sha256()
            for row in cursor:
                digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        return _hash({"catalog_digest": digest.hexdigest(), "options": options})

    def preview(self, store_id: str, *, options: dict | None = None, provider="DIRECT_SHOPIFY", mode="PREVIEW") -> dict:
        options = {"auto_sourcing": True, "product_sync": True, "collection_design": True,
                   "collection_images": False, "paid_image_opt_in": False, "collection_sync": True,
                   "publish_collections": False, "homepage_plan": True, "source_target": 2000,
                   "publish_status": "DRAFT", "media_mode": "MANUAL_MEDIA", "brand_automation": False,
                   "brand_image_opt_in": False, "brand_image_model": None, "brand_apply": False, **(options or {})}
        provider = str(provider).upper(); mode = str(mode).upper()
        if provider not in {"DIRECT_SHOPIFY", "SPARK_FALLBACK"}: raise ValueError("Unknown product route")
        if mode not in {"PREVIEW", "LIVE"}: raise ValueError("mode must be PREVIEW or LIVE")
        if options["collection_images"] and options["paid_image_opt_in"] is not True:
            options["collection_images"] = False
        run_id, now = "SBR_" + secrets.token_hex(10), _now()
        stages = {stage: "PENDING" for stage in STAGES}
        with connect(self.db) as con:
            con.execute("INSERT INTO store_build_runs(run_id,store_id,status,mode,provider,stage,options_json,counts_json,input_hash,created_at,updated_at) VALUES(?,?,'PENDING',?,?,?,?,?,?,?,?)",
                        (run_id, store_id, mode, provider, "PLAN", json.dumps(options), "{}", self._input_hash(store_id, options), now, now))
            for stage in STAGES:
                con.execute("INSERT INTO store_build_stage_runs(run_id,stage,status,updated_at) VALUES(?,?,?,?)", (run_id, stage, stages[stage], now))
        return {"run_id": run_id, "store_id": store_id, "mode": mode, "provider": provider,
                "status": "PENDING", "stages": stages, "options": options,
                "input_hash": self.get(run_id)["input_hash"], "write_performed": False}

    def get(self, run_id: str) -> dict:
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM store_build_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row: raise KeyError(run_id)
            result = dict(row)
            result["options"] = json.loads(result.pop("options_json"))
            for key in ("checkpoint_json", "counts_json", "stage_data_json"):
                result[key.removesuffix("_json")] = json.loads(result.pop(key) or "{}")
            result["stages"] = {item["stage"]: item["status"] for item in con.execute("SELECT stage,status FROM store_build_stage_runs WHERE run_id=? ORDER BY rowid", (run_id,))}
            return result

    def latest(self, store_id: str) -> dict | None:
        with connect(self.db) as con:
            row = con.execute("SELECT run_id FROM store_build_runs WHERE store_id=? ORDER BY updated_at DESC LIMIT 1", (store_id,)).fetchone()
        return self.get(row["run_id"]) if row else None

    def start(self, run_id: str, *, live_confirmed=False) -> dict:
        run = self.get(run_id)
        if run["mode"] != "LIVE" or not live_confirmed:
            return {**run, "status": run["status"], "write_performed": False,
                    "message": "Preview only. Select LIVE and explicitly confirm before starting."}
        if self._input_hash(run["store_id"], run["options"]) != run["input_hash"]:
            raise RuntimeError("Preview is stale because source catalog or Store Decisions changed")
        with connect(self.db) as con:
            active = con.execute("SELECT run_id FROM store_build_runs WHERE store_id=? AND status IN ('RUNNING','PAUSED','MANUAL_ACTION_REQUIRED') AND run_id<>? LIMIT 1", (run["store_id"], run_id)).fetchone()
            if active: raise RuntimeError(f"Another store build run is active: {active['run_id']}")
            con.execute("UPDATE store_build_runs SET status='RUNNING',started_at=COALESCE(started_at,?),updated_at=? WHERE run_id=?", (_now(), _now(), run_id))
        return self._execute(run_id)

    def resume(self, run_id: str, *, manual_confirmation: str | None = None) -> dict:
        run = self.get(run_id)
        if run["status"] not in {"RUNNING", "PAUSED", "MANUAL_ACTION_REQUIRED", "FAILED"}:
            raise RuntimeError("Run is not paused or awaiting a manual action")
        stage = run["stage"]
        if run["status"] == "MANUAL_ACTION_REQUIRED":
            expected = self._required_confirmation(run)
            if manual_confirmation != expected:
                raise RuntimeError("Confirm the required manual action explicitly before resuming")
            if expected == "amazon_challenge_resolved":
                from .capture.campaign import CampaignService
                details = run["stage_data"].get(run["stage"], {})
                campaign = CampaignService(self.db).action(details["campaign_id"], "START")
                output = {**details, "manual_gate": "SOURCE_WORKER", "status": "MANUAL_ACTION_REQUIRED",
                          "campaign_id": campaign["campaign_id"],
                          "instructions": "Challenge resolution was acknowledged. Continue the existing browser worker and resume after campaign completion."}
                self._persist_stage_data(run_id, stage, output)
                self._set_stage(run_id, stage, "MANUAL_ACTION_REQUIRED", output=output)
                with connect(self.db) as con: con.execute("UPDATE store_build_runs SET status='MANUAL_ACTION_REQUIRED',updated_at=? WHERE run_id=?", (_now(), run_id))
                self._write_report(run_id)
                return self.get(run_id)
            if expected == "source_complete":
                from .capture.campaign import CampaignService
                campaign_id = run["stage_data"].get(run["stage"], {}).get("campaign_id")
                campaign = CampaignService(self.db).get(campaign_id) if campaign_id else None
                if not campaign or campaign.get("status") not in {"READY_FOR_SPARK", "DONE", "SEARCH_COMPLETE", "CANDIDATE_TARGET_REACHED"}:
                    raise RuntimeError("The Phase 3.1 campaign is not complete yet; continue sourcing before resuming")
            if expected == "brand_assets_approved":
                from .brand_automation import list_brand_assets
                approved={(asset["asset_type"],asset["approval_status"]) for asset in list_brand_assets(run["store_id"],db=self.db)}
                if not all((kind,"APPROVED") in approved for kind in ("LOGO_MARK","LOGO_HORIZONTAL","FAVICON_32")):
                    raise RuntimeError("Approve a LOGO_MARK, LOGO_HORIZONTAL, and FAVICON_32 before resuming the brand stage.")
            if expected == "brand_theme_manual_applied":
                from .brand_automation import BrandThemeService
                preview_id=run["stage_data"].get("BRAND_APPLY_PREVIEW",{}).get("preview_id")
                check=BrandThemeService(db=self.db).verify_manual_apply(preview_id) if preview_id else {"status":"NOT_FOUND"}
                if check.get("status")!="VERIFIED":raise RuntimeError("Shopify theme settings do not yet match the approved logo/favicon preview.")
            self._set_stage(run_id, stage, "COMPLETE_WITH_WARNINGS", counts={"manual_confirmation": expected})
            run = self.get(run_id)
        elif run["status"] == "FAILED":
            self._set_stage(run_id, stage, "PENDING")
        with connect(self.db) as con:
            con.execute("UPDATE store_build_runs SET status='RUNNING',last_error='',updated_at=? WHERE run_id=?", (_now(), run_id))
        return self._execute(run_id)

    def pause(self, run_id: str) -> bool:
        with connect(self.db) as con:
            row = con.execute("SELECT status FROM store_build_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["status"] != "RUNNING": return False
            con.execute("UPDATE store_build_runs SET status='PAUSED',updated_at=? WHERE run_id=?", (_now(), run_id))
            stage = con.execute("SELECT stage FROM store_build_runs WHERE run_id=?", (run_id,)).fetchone()["stage"]
            con.execute("UPDATE store_build_stage_runs SET status='PAUSED',updated_at=? WHERE run_id=? AND stage=?", (_now(), run_id, stage))
        run = self.get(run_id)
        if stage == "PRODUCT_SYNC":
            product_run = run["stage_data"].get("product_sync_preview", {}).get("run_id")
            if product_run:
                from .shopify_products import DirectShopifyProductPublisher
                DirectShopifyProductPublisher(db=self.db).pause(product_run)
        return True

    def retry_failed(self, run_id: str, *, live_confirmed=False) -> dict:
        if not live_confirmed: raise RuntimeError("Explicit confirmation required to retry failed stages")
        run = self.get(run_id)
        child_id = (run["stage_data"].get("PRODUCT_SYNC", {}).get("product_sync_run_id")
                    or run["stage_data"].get("product_sync_preview", {}).get("run_id"))
        if child_id and run["provider"] == "DIRECT_SHOPIFY":
            from .shopify_products import DirectShopifyProductPublisher
            with connect(self.db) as con:
                failed_count = con.execute("SELECT COUNT(*) FROM shopify_product_sync_items WHERE run_id=? AND status='FAILED'", (child_id,)).fetchone()[0]
            if failed_count:
                result = DirectShopifyProductPublisher(db=self.db).retry_failed(child_id, confirmed=True)
                self._persist_stage_data(run_id, "PRODUCT_SYNC", {"counts": result.get("counts", {}), "product_sync_run_id": child_id,
                                                                      "stage_status": result.get("status")})
                state = "COMPLETE_WITH_WARNINGS" if result.get("status") == "COMPLETE_WITH_WARNINGS" else "COMPLETE"
                self._set_stage(run_id, "PRODUCT_SYNC", state, counts=result.get("counts", {}), output=result)
                final = self._default_final_status(run_id)
                with connect(self.db) as con:
                    con.execute("UPDATE store_build_runs SET status=?,retry_count=retry_count+1,updated_at=? WHERE run_id=?",
                                (final, _now(), run_id))
                self._write_report(run_id)
                return self.get(run_id)
        image_failures = run["stage_data"].get("COLLECTION_IMAGE", {}).get("failures", [])
        if image_failures and run["options"].get("paid_image_opt_in") and run["stage_data"].get("collection_plan_id"):
            from .collection_images import OpenAIImagesProvider, generate_collection_image
            from .collection_planner import CollectionPlanner
            plan = CollectionPlanner(self.db).get_plan(run["stage_data"]["collection_plan_id"])
            by_key = {row["collection_key"]: row for row in plan["collections"]}
            remaining = []
            for failure in image_failures:
                definition = by_key.get(failure.get("collection_key"))
                if not definition:
                    remaining.append(failure); continue
                try: generate_collection_image(run["store_id"], definition, provider=OpenAIImagesProvider(), enabled=True)
                except Exception as exc: remaining.append({"collection_key": definition["collection_key"], "error": str(exc)[:300]})
            result = {"counts": {"ready": len(plan["collections"]) - len(remaining), "failed": len(remaining)}, "failures": remaining}
            self._persist_stage_data(run_id, "COLLECTION_IMAGE", result)
            state = "COMPLETE_WITH_WARNINGS" if remaining else "COMPLETE"
            self._set_stage(run_id, "COLLECTION_IMAGE", state, counts=result["counts"], output=result)
            final = self._default_final_status(run_id)
            with connect(self.db) as con:
                con.execute("UPDATE store_build_runs SET status=?,retry_count=retry_count+1,updated_at=? WHERE run_id=?", (final, _now(), run_id))
            self._write_report(run_id)
            return self.get(run_id)
        collection_failures = run["stage_data"].get("COLLECTION_SYNC", {}).get("counts", {}).get("FAILED", 0)
        if collection_failures and run["stage_data"].get("collection_plan_id"):
            from .collection_planner import CollectionPlanner
            from .shopify_collections import ShopifyCollectionPublisher
            publisher = ShopifyCollectionPublisher(db=self.db)
            plan = CollectionPlanner(self.db).get_plan(run["stage_data"]["collection_plan_id"])
            preview = publisher.dry_run(plan, publish_online_store=bool(run["options"].get("publish_collections")))
            result = publisher.sync(plan, confirmed=True, publish_online_store=bool(run["options"].get("publish_collections")),
                                    retry_failed_only=True, expected_preview=preview)
            self._persist_stage_data(run_id, "COLLECTION_SYNC", {"counts": result.get("summary", {}), "retry_run_id": result.get("run_id")})
            state = "COMPLETE_WITH_WARNINGS" if result.get("summary", {}).get("FAILED") else "COMPLETE"
            self._set_stage(run_id, "COLLECTION_SYNC", state, counts=result.get("summary", {}), output=result)
            final = self._default_final_status(run_id)
            with connect(self.db) as con:
                con.execute("UPDATE store_build_runs SET status=?,retry_count=retry_count+1,updated_at=? WHERE run_id=?",
                            (final, _now(), run_id))
            self._write_report(run_id)
            return self.get(run_id)
        failed = next((stage for stage, state in run["stages"].items() if state == "FAILED"), None)
        if not failed: return run
        with connect(self.db) as con:
            con.execute("UPDATE store_build_runs SET stage=?,status='RUNNING',retry_count=retry_count+1,last_error='',updated_at=? WHERE run_id=?", (failed, _now(), run_id))
        self._set_stage(run_id, failed, "PENDING")
        return self._execute(run_id)

    def _execute(self, run_id):
        run = self.get(run_id)
        start_index = STAGES.index(run["stage"])
        for stage in STAGES[start_index:]:
            run = self.get(run_id)
            if run["status"] != "RUNNING": return run
            if run["stages"].get(stage) in {"COMPLETE", "COMPLETE_WITH_WARNINGS", "SKIPPED"}: continue
            self._set_stage(run_id, stage, "RUNNING")
            with connect(self.db) as con: con.execute("UPDATE store_build_runs SET stage=?,updated_at=? WHERE run_id=?", (stage, _now(), run_id))
            try:
                output = self.handlers[stage](run) if stage in self.handlers else self._default_stage(stage, run)
                output = output or {}
                if output.get("status") == "PAUSED":
                    self._persist_stage_data(run_id, stage, output, context_data=run.get("stage_data"))
                    self._set_stage(run_id, stage, "PAUSED", counts=output.get("counts", {}), output=output)
                    with connect(self.db) as con: con.execute("UPDATE store_build_runs SET status='PAUSED',updated_at=? WHERE run_id=?", (_now(), run_id))
                    return self.get(run_id)
                if output.get("status") == "MANUAL_ACTION_REQUIRED":
                    self._persist_stage_data(run_id, stage, output, context_data=run.get("stage_data"))
                    self._set_stage(run_id, stage, "MANUAL_ACTION_REQUIRED", counts=output.get("counts", {}), output=output)
                    with connect(self.db) as con: con.execute("UPDATE store_build_runs SET status='MANUAL_ACTION_REQUIRED',updated_at=? WHERE run_id=?", (_now(), run_id))
                    self._write_report(run_id)
                    return self.get(run_id)
                state = output.get("stage_status", "COMPLETE")
                if state not in {"COMPLETE", "COMPLETE_WITH_WARNINGS", "SKIPPED"}: state = "COMPLETE"
                self._persist_stage_data(run_id, stage, output, context_data=run.get("stage_data"))
                self._set_stage(run_id, stage, state, counts=output.get("counts", {}), output=output)
            except Exception as exc:
                safe_error = _sanitize(str(exc))
                self._set_stage(run_id, stage, "FAILED", error=safe_error)
                with connect(self.db) as con: con.execute("UPDATE store_build_runs SET status='FAILED',last_error=?,updated_at=? WHERE run_id=?", (safe_error, _now(), run_id))
                self._write_report(run_id)
                return self.get(run_id)
        final = self._default_final_status(run_id)
        with connect(self.db) as con:
            con.execute("UPDATE store_build_runs SET status=?,finished_at=?,updated_at=? WHERE run_id=?", (final, _now(), _now(), run_id))
        self._write_report(run_id)
        return self.get(run_id)

    def _default_stage(self, stage, run):
        store_id, options, data = run["store_id"], run["options"], run["stage_data"]
        if stage == "PLAN": return {"counts": {"requested_target": int(options.get("source_target", 2000))}}
        if stage == "BRAND_PLAN":
            from .brand_automation import brand_profile_from_store
            profile=brand_profile_from_store(store_id,db=self.db)
            return {"counts":{"brand_profile_version":profile["version"]},"brand_name":profile["profile"]["brand_name"]}
        if stage == "BRAND_ASSET_PREVIEW":
            if not options.get("brand_automation"):return {"stage_status":"SKIPPED"}
            return {"counts":{"image_calls":1 if options.get("brand_image_opt_in") else 0},
                    "provider":"OPENAI_IMAGES" if options.get("brand_image_opt_in") else "MANUAL",
                    "model":options.get("brand_image_model") or "gpt-image-1","opt_in":bool(options.get("brand_image_opt_in"))}
        if stage == "BRAND_ASSET_GENERATION":
            if not options.get("brand_automation"):return {"stage_status":"SKIPPED"}
            if not options.get("brand_image_opt_in"):
                return {"status":"MANUAL_ACTION_REQUIRED","manual_gate":"BRAND_GENERATION",
                        "instructions":"브랜드 화면에서 로고 mark를 직접 선택하거나 이미지 자동 생성 사용을 명시적으로 켜세요."}
            from .brand_automation import generate_logo_mark
            from .collection_images import OpenAIImagesProvider
            mark=generate_logo_mark(store_id,provider=OpenAIImagesProvider(model=options.get("brand_image_model")),enabled=True,db=self.db)
            return {"status":"MANUAL_ACTION_REQUIRED","manual_gate":"BRAND_APPROVAL","asset_id":mark["asset_id"],
                    "instructions":"생성 자산을 검토하고 가로 로고와 파비콘을 만든 뒤 필요한 자산을 모두 승인하세요."}
        if stage == "BRAND_ASSET_APPROVAL":
            if not options.get("brand_automation"):return {"stage_status":"SKIPPED"}
            from .brand_automation import list_brand_assets
            approved={(asset["asset_type"],asset["approval_status"]) for asset in list_brand_assets(store_id,db=self.db)}
            if all((kind,"APPROVED") in approved for kind in ("LOGO_MARK","LOGO_HORIZONTAL","FAVICON_32")):
                return {"counts":{"approved_assets":3}}
            return {"status":"MANUAL_ACTION_REQUIRED","manual_gate":"BRAND_APPROVAL","instructions":"LOGO_MARK, LOGO_HORIZONTAL, FAVICON_32를 검토하고 승인하세요."}
        if stage == "BRAND_APPLY_PREVIEW":
            if not options.get("brand_apply"):return {"stage_status":"SKIPPED"}
            from .brand_automation import list_brand_assets,BrandThemeService
            assets=list_brand_assets(store_id,db=self.db)
            logo=next((x for x in reversed(assets) if x["asset_type"]=="LOGO_HORIZONTAL" and x["approval_status"]=="APPROVED"),None)
            favicon=next((x for x in reversed(assets) if x["asset_type"]=="FAVICON_32" and x["approval_status"]=="APPROVED"),None)
            if not logo or not favicon:return {"status":"MANUAL_ACTION_REQUIRED","manual_gate":"BRAND_APPROVAL","instructions":"승인된 로고와 파비콘이 필요합니다."}
            preview=BrandThemeService(db=self.db).preview_apply(store_id,logo_asset_id=logo["asset_id"],favicon_asset_id=favicon["asset_id"])
            data["brand_apply_preview"]=preview
            return {"counts":{"actions":len(preview["actions"])},"preview_id":preview["preview_id"],"preview_status":preview["status"]}
        if stage == "BRAND_APPLY":
            if not options.get("brand_apply"):return {"stage_status":"SKIPPED"}
            preview=data.get("brand_apply_preview") or {}
            if not preview.get("preview_id"):return {"status":"MANUAL_ACTION_REQUIRED","manual_gate":"BRAND_MANUAL_APPLY","instructions":"브랜드 적용 preview가 필요합니다."}
            if not preview.get("write_themes"):
                return {"status":"MANUAL_ACTION_REQUIRED","manual_gate":"BRAND_MANUAL_APPLY","instructions":preview.get("instructions"),"preview_id":preview["preview_id"]}
            from .brand_automation import BrandThemeService
            result=BrandThemeService(db=self.db).apply(preview["preview_id"],confirmed=True)
            if result["status"]!="VERIFIED":return {"status":"MANUAL_ACTION_REQUIRED","manual_gate":"BRAND_MANUAL_APPLY","instructions":preview.get("instructions"),"result":result}
            return {"counts":{"verified":True},"result":result}
        if stage == "SOURCING":
            if not options.get("auto_sourcing"): return {"stage_status": "SKIPPED"}
            from .capture.campaign import CampaignService
            from .sourcing.planner import CategoryPlanner
            source_plan = (CategoryPlanner(self.db).get_plan(data["source_plan_id"]) if data.get("source_plan_id")
                           else CategoryPlanner(self.db).create_plan(store_id, int(options.get("source_target", 2000))))
            data["source_plan_id"] = source_plan["plan_id"]
            self._persist_stage_data(run["run_id"], stage, {"checkpoint": "SOURCE_PLAN_CREATED", "source_plan_id": source_plan["plan_id"]}, context_data=data)
            campaign = CampaignService(self.db).create_auto_store(source_plan["plan_id"])
            if campaign.get("status") in {"DRAFT", "PAUSED", "PAUSED_NEEDS_USER"}:
                if campaign.get("status") == "PAUSED_NEEDS_USER":
                    return {"status": "MANUAL_ACTION_REQUIRED", "manual_gate": "SOURCE_CAPTCHA", "campaign_id": campaign["campaign_id"],
                            "source_plan_id": source_plan["plan_id"],
                            "instructions": "Amazon presented a confirmation challenge. Resolve it manually; CAPTCHA bypass is not supported, then confirm to resume the existing campaign."}
                campaign = CampaignService(self.db).action(campaign["campaign_id"], "START")
            return {"status": "MANUAL_ACTION_REQUIRED", "manual_gate": "SOURCE_WORKER", "campaign_id": campaign["campaign_id"],
                    "source_plan_id": source_plan["plan_id"], "instructions": "Amazon sourcing is staged. Use the existing ShopSource browser worker; resume after the campaign reaches READY_FOR_SPARK."}
        if stage == "SOURCE_VALIDATION":
            from .classifier import classify_store
            result = classify_store(store_id, self.db)
            return {"counts": result["counts"], "processed": result["processed"]}
        if stage == "PRODUCT_SYNC_PREVIEW":
            if run["provider"] == "SPARK_FALLBACK" or not options.get("product_sync"): return {"stage_status": "SKIPPED"}
            if options.get("collection_design") and not data.get("collection_plan_id"):
                from .collection_planner import CollectionPlanner
                strategy = "TAG_PREFERRED" if run["provider"] == "DIRECT_SHOPIFY" else "TITLE_FALLBACK"
                planned = CollectionPlanner(self.db).create_plan(store_id, settings={"rule_strategy": strategy})
                data["collection_plan_id"] = planned["plan_id"]
            from .shopify_products import DirectShopifyProductPublisher
            publisher = DirectShopifyProductPublisher(db=self.db)
            publisher.set_media_mode(store_id, options.get("media_mode", "MANUAL_MEDIA"),
                                     source_media_rights_confirmed=options.get("source_media_rights_confirmed", False))
            result = publisher.preview(store_id, publish_status=options.get("publish_status", "DRAFT"))
            data["product_sync_preview"] = result
            return {"counts": result["counts"], "product_sync_run_id": result["run_id"], "product_input_hash": result["input_hash"]}
        if stage == "PRODUCT_SYNC":
            if run["provider"] == "SPARK_FALLBACK" or not options.get("product_sync"): return {"stage_status": "SKIPPED"}
            from .shopify_products import DirectShopifyProductPublisher
            preview = data.get("product_sync_preview") or {}
            result = DirectShopifyProductPublisher(db=self.db).sync(preview["run_id"], confirmed=True, expected_input_hash=preview["input_hash"])
            data["product_sync_result"] = result
            if result.get("status") == "PAUSED": return {"status": "PAUSED", "counts": result.get("counts", {}), "product_sync_run_id": result["run_id"]}
            return {"stage_status": "COMPLETE_WITH_WARNINGS" if result.get("status") == "COMPLETE_WITH_WARNINGS" else "COMPLETE",
                    "counts": result.get("counts", {}), "product_sync_run_id": result["run_id"]}
        if stage == "PRODUCT_VERIFY":
            if run["provider"] == "SPARK_FALLBACK":
                from .connectors.spark_center_package import SparkCenterPackageService
                package = SparkCenterPackageService().create(store_id=store_id, statuses=sorted({"PRIMARY", "RESERVE_A", "RESERVE_B", "RESERVE_C", "LOW_RESERVE", "HIGH_RESERVE", "REVIEW"}), limit=int(options.get("source_target", 2000)), db=self.db)
                from .connectors.spark_center_package import stage_package_for_spark_desktop
                staged = stage_package_for_spark_desktop(package["package_id"], db=self.db)
                return {"status": "MANUAL_ACTION_REQUIRED", "manual_gate": "SPARK_UPLOAD", "package_id": package["package_id"],
                        "staged_path": str(staged), "instructions": "Review the safe Spark Desktop staging folder, upload in SparkShopify manually, then resume after confirming upload."}
            product_run_id = data.get("product_sync_run_id") or data.get("product_sync_result", {}).get("run_id")
            if not product_run_id:
                return {"status": "FAILED", "verified": False, "instructions": "상품 동기화 실행 ID가 없어 결과를 검증할 수 없습니다."}
            with connect(self.db) as con:
                states = {row["status"]: row["n"] for row in con.execute(
                    "SELECT status,COUNT(*) n FROM shopify_product_sync_items WHERE run_id=? GROUP BY status", (product_run_id,))}
            failed = states.get("FAILED", 0) + states.get("VERIFY_FAILED", 0) + states.get("SYNCED_WITH_WARNINGS", 0)
            pending = states.get("PENDING", 0)
            verified = not failed and not pending
            if not verified:
                raise RuntimeError("Product verification failed; inspect VERIFY_FAILED/FAILED items and retry only failures.")
            return {"counts": states, "verified": True,
                    "instructions": "각 생성/수정 항목은 Shopify 재조회 검증 결과를 확인하세요."}
        if stage == "COLLECTION_PLAN":
            if not options.get("collection_design"): return {"stage_status": "SKIPPED"}
            from .collection_planner import CollectionPlanner
            if data.get("collection_plan_id"):
                plan = CollectionPlanner(self.db).get_plan(data["collection_plan_id"])
            else:
                strategy = "TAG_PREFERRED" if run["provider"] == "DIRECT_SHOPIFY" else "TITLE_FALLBACK"
                plan = CollectionPlanner(self.db).create_plan(store_id, settings={"rule_strategy": strategy})
                data["collection_plan_id"] = plan["plan_id"]
            return {"counts": {"collections": plan["collection_count"]}, "collection_plan_id": plan["plan_id"]}
        if stage == "COLLECTION_IMAGE":
            if not options.get("collection_images") or not options.get("paid_image_opt_in"): return {"stage_status": "SKIPPED", "reason": "Paid image generation not opted in"}
            from .collection_images import OpenAIImagesProvider, generate_collection_image
            from .collection_planner import CollectionPlanner
            from .shopify_collections import ShopifyCollectionPublisher
            plan = CollectionPlanner(self.db).get_plan(data["collection_plan_id"])
            success, failed = 0, []
            for definition in plan["collections"]:
                if ShopifyCollectionPublisher(db=self.db)._image_asset(store_id, definition["collection_key"]):
                    success += 1
                else:
                    try: generate_collection_image(store_id, definition, provider=OpenAIImagesProvider(), enabled=True); success += 1
                    except Exception as exc: failed.append({"collection_key": definition["collection_key"], "error": str(exc)[:300]})
                data["collection_image_progress"] = {"ready": success, "failed": len(failed), "failures": failed}
                self._persist_stage_data(run["run_id"], stage, {"counts": data["collection_image_progress"], "failures": failed}, context_data=data)
            data["image_counts"] = {"ready": success, "failed": len(failed)}
            return {"stage_status": "COMPLETE_WITH_WARNINGS" if failed else "COMPLETE", "counts": data["image_counts"], "failures": failed}
        if stage == "COLLECTION_SYNC_PREVIEW":
            if not options.get("collection_sync"): return {"stage_status": "SKIPPED"}
            from .collection_planner import CollectionPlanner
            from .shopify_collections import ShopifyCollectionPublisher
            plan = CollectionPlanner(self.db).get_plan(data["collection_plan_id"])
            preview = ShopifyCollectionPublisher(db=self.db).dry_run(plan, publish_online_store=bool(options.get("publish_collections")))
            data["collection_preview"] = preview
            return {"counts": preview["counts"], "collection_preview": preview}
        if stage == "COLLECTION_SYNC":
            if not options.get("collection_sync"): return {"stage_status": "SKIPPED"}
            from .collection_planner import CollectionPlanner
            from .shopify_collections import ShopifyCollectionPublisher
            plan = CollectionPlanner(self.db).get_plan(data["collection_plan_id"])
            result = ShopifyCollectionPublisher(db=self.db).sync(plan, confirmed=True, publish_online_store=bool(options.get("publish_collections")), expected_preview=data.get("collection_preview"))
            data["collection_sync_result"] = result
            return {"counts": result.get("summary", {})}
        if stage == "COLLECTION_VERIFY":
            if not options.get("collection_sync"):
                return {"verified": True, "stage_status": "SKIPPED"}
            result = data.get("collection_sync_result") or {}
            items = result.get("items") or []
            failed = sum(1 for item in items if item.get("result") == "FAILED")
            verified = bool(items) and not failed
            if not verified:
                raise RuntimeError("Collection verification failed; inspect collection sync results before continuing.")
            return {"verified": True, "counts": {"verified_or_unchanged": len(items), "failed": 0}}
        if stage == "HOMEPAGE_PLAN":
            if not options.get("homepage_plan"): return {"stage_status": "SKIPPED"}
            from .homepage_collections import HomepageCollectionService, ShopifyThemeReader, build_homepage_plan
            from .collection_planner import CollectionPlanner
            plan = CollectionPlanner(self.db).get_plan(data["collection_plan_id"])
            snapshot = ShopifyThemeReader(db=self.db).discover(store_id)
            if snapshot.get("status") != "CONNECTED":
                result = {"status": "MANUAL_PATCH_MODE", "manual_patch_mode": True, "current": None, "proposed": None,
                          "operations": [], "warnings": [snapshot.get("warning") or snapshot.get("status")]}
            else:
                with connect(self.db) as con:
                    handles = {row["collection_key"]: row["handle"] for row in con.execute("SELECT collection_key,handle FROM shopify_collection_mappings WHERE store_id=?", (store_id,))}
                result = build_homepage_plan(snapshot, plan, collection_handles=handles, db=self.db)
            if result.get("current") is not None and result.get("status") != "CONFLICT":
                data["homepage_backup"] = HomepageCollectionService(db=self.db).save_safe_patch(result, store_id=store_id)
            data["homepage_plan"] = result
            return {"status": "MANUAL_ACTION_REQUIRED", "manual_gate": "THEME_APPLY", "homepage_status": result.get("status"),
                    "instructions": "Review the local before/proposed/diff patch (if theme files were readable). Resolve any CONFLICT first, then add/update only the listed collection sections in Shopify Theme Editor. Theme API writes are disabled in this phase."}
        if stage == "FINAL_VERIFY":
            return {"counts": self._catalog_counts(store_id), "homepage_status": data.get("homepage_plan", {}).get("status", "SKIPPED")}
        if stage == "COMPLETE": return {}
        return {"stage_status": "SKIPPED"}

    @staticmethod
    def _required_confirmation(run):
        details = run["stage_data"].get(run["stage"], {})
        return {"SOURCE_WORKER": "source_complete", "SOURCE_CAPTCHA": "amazon_challenge_resolved",
                "SPARK_UPLOAD": "spark_upload_confirmed", "THEME_APPLY": "theme_manual_apply_confirmed",
                "BRAND_APPROVAL": "brand_assets_approved", "BRAND_MANUAL_APPLY": "brand_theme_manual_applied"}.get(details.get("manual_gate"), "manual_action_confirmed")

    def _default_final_status(self, run_id):
        run = self.get(run_id)
        if any(value == "COMPLETE_WITH_WARNINGS" for value in run["stages"].values()): return "COMPLETE_WITH_WARNINGS"
        return "COMPLETE"

    def _catalog_counts(self, store_id):
        with connect(self.db) as con:
            return {row["final_status"] or "NO_DECISION": row["count"] for row in con.execute("SELECT d.final_status,COUNT(*) count FROM products p LEFT JOIN store_product_decisions d ON d.product_id=p.id AND d.store_id=? GROUP BY d.final_status", (store_id,))}

    def _persist_stage_data(self, run_id, stage, output, *, context_data=None):
        with connect(self.db) as con:
            row = con.execute("SELECT stage_data_json,checkpoint_json,counts_json FROM store_build_runs WHERE run_id=?", (run_id,)).fetchone()
            stage_data = json.loads(row["stage_data_json"] or "{}")
            if context_data:
                stage_data.update(context_data)
            stage_data[stage] = output
            checkpoint = json.loads(row["checkpoint_json"] or "{}")
            checkpoint["stage"] = stage
            counts = json.loads(row["counts_json"] or "{}")
            counts[stage] = output.get("counts", {})
            con.execute("UPDATE store_build_runs SET stage_data_json=?,checkpoint_json=?,counts_json=?,updated_at=? WHERE run_id=?",
                        (json.dumps(stage_data, ensure_ascii=False), json.dumps(checkpoint), json.dumps(counts), _now(), run_id))

    def _set_stage(self, run_id, stage, status, *, counts=None, output=None, error=""):
        if status not in STAGE_STATES: raise ValueError(status)
        with connect(self.db) as con:
            con.execute("UPDATE store_build_stage_runs SET status=?,counts_json=?,output_hash=?,last_error=?,updated_at=? WHERE run_id=? AND stage=?",
                        (status, json.dumps(counts or {}), _hash(output or {}), error, _now(), run_id, stage))

    def _write_report(self, run_id):
        run = self.get(run_id)
        folder = self.export_dir / "store_build_reports" / run["store_id"] / run_id
        folder.mkdir(parents=True, exist_ok=True)
        failures = []
        for stage, data in run["stage_data"].items():
            for failure in data.get("failures", []): failures.append(_sanitize({"stage": stage, **failure}))
        product_sync = {}
        product_preview = run["stage_data"].get("product_sync_preview", {})
        product_output = run["stage_data"].get("PRODUCT_SYNC", {})
        product_run_id = product_output.get("product_sync_run_id") or product_preview.get("run_id")
        if product_run_id:
            try:
                with connect(self.db) as con:
                    product_sync = {row["status"]: row["count"] for row in con.execute(
                        "SELECT status,COUNT(*) count FROM shopify_product_sync_items WHERE run_id=? GROUP BY status", (product_run_id,))}
                    for row in con.execute("SELECT master_product_id,source_id,action,status,error,remote_id FROM shopify_product_sync_items WHERE run_id=? AND status IN ('FAILED','CONFLICT')", (product_run_id,)):
                        failures.append(_sanitize({"stage": "PRODUCT_SYNC", **dict(row)}))
            except Exception:
                product_sync = {}
        try:
            started = datetime.fromisoformat(run["started_at"]) if run.get("started_at") else None
            finished = datetime.fromisoformat(run["finished_at"]) if run.get("finished_at") else datetime.now(timezone.utc)
            duration = max(0, int((finished - started).total_seconds())) if started else None
        except (TypeError, ValueError): duration = None
        image_data = run["stage_data"].get("COLLECTION_IMAGE", {}).get("counts", {})
        collection_data = run["stage_data"].get("COLLECTION_PLAN", {}).get("counts", {})
        homepage_status = run["stage_data"].get("homepage_plan", {}).get("status", "NOT_PLANNED")
        report = {"run_id": run_id, "store_id": run["store_id"], "status": run["status"], "mode": run["mode"],
                  "provider": run["provider"], "stages": run["stages"], "counts": run["counts"],
                  "products": {"requested": product_preview.get("requested"), "eligible": product_preview.get("eligible"), "sync": product_sync},
                  "collections": collection_data, "images": image_data, "homepage_status": homepage_status,
                  "manual_action_required": run["status"] == "MANUAL_ACTION_REQUIRED", "duration_seconds": duration,
                  "retry_count": run["retry_count"], "errors": [_sanitize(run["last_error"])] if run["last_error"] else [],
                  "secrets_included": False}
        (folder / "summary.json").write_text(json.dumps(_sanitize(report), ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "failures.json").write_text(json.dumps(_sanitize(failures), ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [f"# Store build {run_id}", "", f"- Store: {run['store_id']}", f"- Status: {run['status']}", f"- Product route: {run['provider']}", "",
                 "## Stages", *[f"- {name}: {status}" for name, status in run["stages"].items()], "",
                 "## Manual action", run["stage_data"].get(run["stage"], {}).get("instructions", "None")]
        (folder / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(folder)
