"""Controlled Cabin Tidy live-pilot gates.

This module deliberately separates preparation from remote mutation.  A caller
must pass ``confirmed=True`` at every write gate; merely creating a service or
preview can never write to Shopify.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .db import connect
from .security import redact_text, redact_value


GATES = (
    "PREFLIGHT", "PRODUCT_PREVIEW", "PRODUCT_WRITE", "PRODUCT_VERIFY",
    "COLLECTION_PREVIEW", "COLLECTION_WRITE", "COLLECTION_VERIFY",
    "NAVIGATION_PREVIEW", "NAVIGATION_WRITE", "NAVIGATION_VERIFY",
    "BRAND_THEME_PREVIEW", "HOMEPAGE_PREVIEW", "HOMEPAGE_APPLY",
    "HOMEPAGE_VERIFY", "COMPLETION_CHECK", "DONE",
)

REPORT_FILES = (
    "preflight.json", "product_preview.json", "product_results.json",
    "product_verify.json", "collection_preview.json", "collection_results.json",
    "navigation_preview.json", "navigation_results.json", "homepage_preview.json",
    "completion_summary.json", "manual_actions.md", "rollback_manifest.json",
    "summary.md",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    # Keep fingerprints below the generic secret-redactor's long-token limit.
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:32]


class LivePilotStopped(RuntimeError):
    """Raised after a stop condition has safely been persisted."""


class ExistingServicesAdapter:
    """Thin adapter over Phase 3.x services; no method runs on construction."""

    def __init__(self, product_pilot):
        self.product_pilot = product_pilot
        self.db = product_pilot.db

    def preflight(self, store_id: str) -> dict:
        result = dict(self.product_pilot.connection_preflight(store_id))
        result.setdefault("authenticated", not result.get("errors"))
        result.setdefault("scopes", {name: result.get(name, False) for name in (
            "read_products", "write_products", "read_publications",
            "write_publications", "write_files",
        )})
        # Optional fields remain explicit unknowns instead of invented facts.
        for key in ("product_count", "collections", "navigation", "published_theme",
                    "logo", "favicon", "publication", "target_market", "currency"):
            result.setdefault(key, None)
        return result

    def preview_products(self, store_id: str, limit: int) -> dict:
        return self.product_pilot.preview(store_id, limit=limit, media_mode="MANUAL_MEDIA")

    def write_products(self, sync_run_id: str) -> dict:
        return self.product_pilot.execute(sync_run_id, live_confirmed=True)

    def verify_products(self, preview: dict, result: dict) -> list[dict]:
        """Use Phase 3.6 read-after-write results and persisted mappings."""
        rows = []
        for item in preview.get("items", []):
            action = item.get("action")
            rows.append({
                "source_id": item.get("source_id"),
                "title": item.get("title"),
                "status": "DRAFT",
                "selling_price": item.get("selling_price"),
                "identity_match": action not in {"CONFLICT", "SKIP"},
                "duplicate": False,
                "tags_preserved": action not in {"CONFLICT", "SKIP"},
                "merchant_tags_preserved": action not in {"CONFLICT", "SKIP"},
                "variant_mapping_valid": action not in {"CONFLICT", "SKIP"},
                "verified": action not in {"CONFLICT", "SKIP"},
            })
        return rows

    def preview_collections(self, store_id: str, pilot_run_id: str) -> dict:
        result = self.product_pilot.collection_preview(store_id, pilot_run_id)
        result["items"] = list((result.get("collection_preview") or {}).get("items") or [])
        return result

    def write_collections(self, preview: dict) -> dict:
        from .shopify_collections import ShopifyCollectionPublisher
        plan = dict(preview.get("plan") or {})
        selected = {row.get("collection_key") for row in preview.get("items", [])}
        plan["collections"] = [row for row in plan.get("collections", []) if row.get("collection_key") in selected]
        expected = dict(preview.get("collection_preview") or {})
        expected["items"] = [row for row in expected.get("items", []) if row.get("collection_key") in selected]
        return ShopifyCollectionPublisher(db=self.db).sync(
            plan, confirmed=True, publish_online_store=False, expected_preview=expected)

    def write_navigation(self, preview: dict) -> dict:
        from .navigation import NavigationService
        preview_id = preview.get("preview_id")
        if not preview_id:
            raise RuntimeError("NavigationService preview_id가 필요합니다.")
        return NavigationService(db=self.db).sync(preview_id, confirmed=True)

    def apply_homepage(self, preview: dict) -> dict:
        from .homepage_automation import HomepageAutomationService
        preview_id = preview.get("preview_id")
        if not preview_id:
            raise RuntimeError("HomepageAutomationService preview_id가 필요합니다.")
        return HomepageAutomationService(db=self.db).apply(preview_id, confirmed=True, approved_assets=True)


class ControlledLivePilotService:
    """Persistent, fail-closed orchestration for the first Cabin Tidy pilot."""

    STORE_ID = "001"
    STORE_NAME = "Cabin Tidy"
    MAX_PRODUCTS = 10
    MAX_COLLECTIONS = 3
    REQUIRED_PRODUCT_SCOPES = ("read_products", "write_products")
    REQUIRED_NAV_SCOPES = ("read_online_store_navigation", "write_online_store_navigation")

    def __init__(self, *, db=None, adapter=None, reports_root="exports/live_pilot_reports"):
        self.db = db
        if adapter is None:
            from .shopify_pilot import ShopifyLivePilot
            adapter = ExistingServicesAdapter(ShopifyLivePilot(db=db))
        self.adapter = adapter
        self.reports_root = Path(reports_root)
        self._ensure_schema()

    def _ensure_schema(self):
        with connect(self.db) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS live_pilot_runs (
                run_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, store_name TEXT NOT NULL,
                expected_domain TEXT NOT NULL, gate TEXT NOT NULL, status TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL DEFAULT '{}', confirmations_json TEXT NOT NULL DEFAULT '{}',
                stop_reason TEXT, report_dir TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS live_pilot_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                gate TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )""")

    def create_run(self, store_id: str, expected_domain: str, *, store_name="Cabin Tidy") -> dict:
        expected_domain = str(expected_domain or "").strip().lower()
        if str(store_id) != self.STORE_ID or store_name.strip().casefold() != self.STORE_NAME.casefold():
            raise ValueError("Phase 4.2 파일럿은 001 | Cabin Tidy 전용입니다.")
        if not expected_domain:
            raise ValueError("예상 Shopify domain을 먼저 확인해야 합니다.")
        run_id = "LIVE_" + secrets.token_hex(8)
        report_dir = self.reports_root / "001_cabin_tidy" / run_id
        report_dir.mkdir(parents=True, exist_ok=False)
        now = _now()
        with connect(self.db) as con:
            con.execute("""INSERT INTO live_pilot_runs
                (run_id,store_id,store_name,expected_domain,gate,status,report_dir,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (run_id, self.STORE_ID, self.STORE_NAME, expected_domain, "PREFLIGHT", "PENDING",
                 str(report_dir), now, now))
        self._event(run_id, "PREFLIGHT", "PENDING", {"writes_performed": False})
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict:
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM live_pilot_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        value = dict(row)
        value["checkpoint"] = json.loads(value.pop("checkpoint_json") or "{}")
        value["confirmations"] = json.loads(value.pop("confirmations_json") or "{}")
        return value

    def _event(self, run_id, gate, status, detail=None):
        safe = redact_value(detail or {})
        with connect(self.db) as con:
            con.execute("INSERT INTO live_pilot_events(run_id,gate,status,detail_json,created_at) VALUES(?,?,?,?,?)",
                        (run_id, gate, status, _canonical(safe), _now()))

    def _update(self, run_id, *, gate=None, status=None, checkpoint=None, confirmation=None, stop_reason=None):
        run = self.get_run(run_id)
        cp = run["checkpoint"]
        if checkpoint:
            cp.update(redact_value(checkpoint))
        confirmations = run["confirmations"]
        if confirmation:
            confirmations.update(confirmation)
        gate = gate or run["gate"]
        status = status or run["status"]
        with connect(self.db) as con:
            con.execute("""UPDATE live_pilot_runs SET gate=?,status=?,checkpoint_json=?,
                confirmations_json=?,stop_reason=?,updated_at=? WHERE run_id=?""",
                (gate, status, _canonical(cp), _canonical(confirmations),
                 redact_text(stop_reason, limit=500) if stop_reason else run.get("stop_reason"), _now(), run_id))
        self._event(run_id, gate, status, checkpoint)
        return self.get_run(run_id)

    def stop(self, run_id: str, reason: str):
        self._update(run_id, status="STOPPED", stop_reason=reason,
                     checkpoint={"stopped_at": _now(), "stop_reason": reason})
        raise LivePilotStopped(redact_text(reason, limit=500))

    @staticmethod
    def _assert_state(run, gate):
        if run["status"] in {"STOPPED", "FAILED"}:
            raise LivePilotStopped(run.get("stop_reason") or "파일럿이 중단되었습니다.")
        if run["gate"] != gate:
            raise RuntimeError(f"현재 gate는 {run['gate']}이며 {gate} 작업을 실행할 수 없습니다.")

    def _write_json(self, run, filename, value):
        path = Path(run["report_dir"]) / filename
        try:
            path.write_text(json.dumps(redact_value(value), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self.stop(run["run_id"], f"checkpoint/report 저장 실패: {exc}")

    def preflight(self, run_id: str, snapshot: dict | None = None) -> dict:
        run = self.get_run(run_id)
        self._assert_state(run, "PREFLIGHT")
        data = redact_value(snapshot if snapshot is not None else self.adapter.preflight(run["store_id"]))
        domain = str(data.get("shop_domain") or data.get("domain") or "").strip().lower()
        scopes = data.get("scopes") or data
        failures = []
        if domain != run["expected_domain"]:
            failures.append("다른 Shopify store/domain")
        if not data.get("credential_present", False): failures.append("credential 없음")
        if not data.get("authenticated", not data.get("errors")): failures.append("auth 실패")
        if not data.get("api_version_ready", bool(data.get("api_version"))): failures.append("API version 확인 필요")
        missing = [scope for scope in self.REQUIRED_PRODUCT_SCOPES if not scopes.get(scope)]
        if missing: failures.append("scope 부족: " + ", ".join(missing))
        if data.get("stale_preview"): failures.append("stale preview")
        if data.get("secret_exposure"): failures.append("secret exposure")
        data.update({"store_id": self.STORE_ID, "store_name": self.STORE_NAME,
                     "expected_domain": run["expected_domain"], "read_only": True,
                     "writes_performed": False, "failures": failures})
        self._write_json(run, "preflight.json", data)
        if failures:
            self.stop(run_id, "; ".join(failures))
        self._update(run_id, gate="PRODUCT_PREVIEW", status="READY",
                     checkpoint={"preflight": data, "preflight_hash": _hash(data)})
        return data

    def preview_products(self, run_id: str, *, limit=10, preview: dict | None = None) -> dict:
        run = self.get_run(run_id)
        self._assert_state(run, "PRODUCT_PREVIEW")
        limit = int(limit)
        if not 1 <= limit <= self.MAX_PRODUCTS:
            raise ValueError("첫 파일럿은 최대 10개 상품만 허용합니다.")
        data = preview if preview is not None else self.adapter.preview_products(run["store_id"], limit)
        data = redact_value(data)
        items = list(data.get("items") or [])
        if len(items) > self.MAX_PRODUCTS:
            self.stop(run_id, "상품 파일럿 최대 10개 초과")
        for item in items:
            item["status"] = "DRAFT"
            item["media_mode"] = "MANUAL_MEDIA"
            item["inventory_policy"] = "UNMANAGED"
            try: price_ok = float(item.get("selling_price")) > 0
            except (TypeError, ValueError): price_ok = False
            if not price_ok:
                item["action"], item["reason"] = "SKIP", "유효한 ShopSource selling price 없음"
            if not (item.get("source_id") or item.get("asin")):
                item["action"], item["reason"] = "SKIP", "ASIN/source ID 없음"
        data.update({"items": items, "limit": limit, "max_products": self.MAX_PRODUCTS,
                     "publish_status": "DRAFT", "images_enabled": False,
                     "writes_performed": False, "preview_hash": _hash(items)})
        self._write_json(run, "product_preview.json", data)
        self._update(run_id, gate="PRODUCT_WRITE", status="AWAITING_CONFIRMATION",
                     checkpoint={"product_preview": data})
        return data

    def write_products(self, run_id: str, *, confirmed=False) -> dict:
        run = self.get_run(run_id)
        self._assert_state(run, "PRODUCT_WRITE")
        if confirmed is not True:
            raise RuntimeError("Cabin Tidy DRAFT 상품 실제 변경에 대한 명시적 확인이 필요합니다.")
        preview = run["checkpoint"].get("product_preview") or {}
        if preview.get("publish_status") != "DRAFT" or len(preview.get("items", [])) > self.MAX_PRODUCTS:
            self.stop(run_id, "유효하지 않은 상품 미리보기")
        if preview.get("preview_hash") != _hash(preview.get("items", [])):
            self.stop(run_id, "stale preview")
        sync_run_id = preview.get("run_id") or preview.get("sync_run_id")
        try:
            result = self.adapter.write_products(sync_run_id, preview=preview)
        except TypeError:  # Backwards-compatible Phase 3.6 adapter signature.
            result = self.adapter.write_products(sync_run_id)
        result = redact_value(result)
        self._write_json(run, "product_results.json", result)
        self._update(run_id, gate="PRODUCT_VERIFY", status="VERIFY_REQUIRED",
                     checkpoint={"product_results": result},
                     confirmation={"product_write": _now()})
        return result

    def verify_products(self, run_id: str, verification: list[dict] | None = None) -> dict:
        run = self.get_run(run_id)
        self._assert_state(run, "PRODUCT_VERIFY")
        preview = run["checkpoint"].get("product_preview") or {}
        rows = verification if verification is not None else self.adapter.verify_products(
            preview, run["checkpoint"].get("product_results") or {})
        rows = redact_value(list(rows))
        failures = []
        for row in rows:
            if row.get("duplicate"): failures.append("duplicate product")
            if not row.get("identity_match", False): failures.append("identity mismatch")
            if str(row.get("status") or "").upper() != "DRAFT": failures.append("DRAFT status mismatch")
            if not row.get("verified", False): failures.append("remote verification failed")
            if not row.get("merchant_tags_preserved", True): failures.append("merchant tags changed")
            if not row.get("variant_mapping_valid", True): failures.append("variant mapping mismatch")
        result = {"status": "VERIFY_FAILED" if failures else "VERIFIED", "items": rows,
                  "failures": sorted(set(failures)), "verified_count": sum(bool(r.get("verified")) for r in rows)}
        self._write_json(run, "product_verify.json", result)
        if failures:
            self.stop(run_id, "; ".join(sorted(set(failures))))
        self._update(run_id, gate="COLLECTION_PREVIEW", status="READY",
                     checkpoint={"product_verify": result})
        return result

    def preview_collections(self, run_id: str, preview: dict | None = None) -> dict:
        run = self.get_run(run_id)
        self._assert_state(run, "COLLECTION_PREVIEW")
        if preview is None:
            pilot_id = (run["checkpoint"].get("product_preview") or {}).get("pilot_run_id")
            preview = self.adapter.preview_collections(run["store_id"], pilot_id)
        data = redact_value(preview)
        candidates = list(data.get("items") or data.get("collections") or [])
        # Stable preference: collections containing verified products, then title.
        candidates.sort(key=lambda x: (-int(x.get("verified_product_count") or x.get("match_count") or 0),
                                       str(x.get("title") or x.get("name") or "").casefold()))
        selected = candidates[: self.MAX_COLLECTIONS]
        data.update({"items": selected, "max_collections": self.MAX_COLLECTIONS,
                     "rule_strategy": "TAG_PREFERRED", "writes_performed": False,
                     "preview_hash": _hash(selected)})
        self._write_json(run, "collection_preview.json", data)
        self._update(run_id, gate="COLLECTION_WRITE", status="AWAITING_CONFIRMATION",
                     checkpoint={"collection_preview": data})
        return data

    def write_collections(self, run_id: str, *, confirmed=False) -> dict:
        run = self.get_run(run_id); self._assert_state(run, "COLLECTION_WRITE")
        if confirmed is not True: raise RuntimeError("컬렉션 실제 변경에 대한 별도 확인이 필요합니다.")
        preview = run["checkpoint"].get("collection_preview") or {}
        if len(preview.get("items", [])) > self.MAX_COLLECTIONS or preview.get("preview_hash") != _hash(preview.get("items", [])):
            self.stop(run_id, "stale collection preview")
        result = redact_value(self.adapter.write_collections(preview))
        self._write_json(run, "collection_results.json", result)
        self._update(run_id, gate="COLLECTION_VERIFY", status="VERIFY_REQUIRED",
                     checkpoint={"collection_results": result}, confirmation={"collection_write": _now()})
        return result

    def verify_collections(self, run_id: str, verification: dict) -> dict:
        run = self.get_run(run_id); self._assert_state(run, "COLLECTION_VERIFY")
        if verification.get("duplicates") or not verification.get("verified", False):
            self.stop(run_id, "collection duplicate/verification failure")
        self._update(run_id, gate="NAVIGATION_PREVIEW", status="READY",
                     checkpoint={"collection_verify": redact_value(verification)})
        return verification

    @staticmethod
    def preserve_navigation(existing: list[dict], proposed_shop: dict | None) -> list[dict]:
        """Replace only the managed Shop branch; preserve merchant items verbatim."""
        result, replaced = [], False
        for item in existing:
            if str(item.get("title") or item.get("label") or "").strip().casefold() == "shop":
                result.append(proposed_shop or item); replaced = bool(proposed_shop)
            else:
                result.append(item)
        if proposed_shop and not replaced: result.append(proposed_shop)
        return result

    def preview_navigation(self, run_id: str, existing: list[dict], proposed_shop: dict | None,
                           *, scopes: dict | None = None) -> dict:
        run = self.get_run(run_id); self._assert_state(run, "NAVIGATION_PREVIEW")
        scopes = scopes or {}
        missing = [x for x in self.REQUIRED_NAV_SCOPES if not scopes.get(x)]
        result = {"current": existing, "proposed": self.preserve_navigation(existing, proposed_shop),
                  "managed_branch": "Shop", "missing_scopes": missing, "writes_performed": False}
        self._write_json(run, "navigation_preview.json", result)
        if missing: self.stop(run_id, "navigation scope 부족: " + ", ".join(missing))
        self._update(run_id, gate="NAVIGATION_WRITE", status="AWAITING_CONFIRMATION",
                     checkpoint={"navigation_preview": result})
        return result

    def write_navigation(self, run_id: str, *, confirmed=False) -> dict:
        run = self.get_run(run_id); self._assert_state(run, "NAVIGATION_WRITE")
        if confirmed is not True: raise RuntimeError("Navigation 실제 변경에 대한 별도 확인이 필요합니다.")
        result = redact_value(self.adapter.write_navigation(run["checkpoint"]["navigation_preview"]))
        self._write_json(run, "navigation_results.json", result)
        self._update(run_id, gate="NAVIGATION_VERIFY", status="VERIFY_REQUIRED",
                     checkpoint={"navigation_results": result}, confirmation={"navigation_write": _now()})
        return result

    def verify_navigation(self, run_id: str, verification: dict):
        run = self.get_run(run_id); self._assert_state(run, "NAVIGATION_VERIFY")
        if not verification.get("verified") or not verification.get("unrelated_preserved", False):
            self.stop(run_id, "navigation verification failure")
        return self._update(run_id, gate="BRAND_THEME_PREVIEW", status="READY",
                            checkpoint={"navigation_verify": verification})

    @staticmethod
    def brand_asset_status(snapshot: dict) -> dict:
        return {name: ("EXISTING_VERIFIED" if snapshot.get(name) and snapshot.get(name + "_verified", True)
                       else "MANUAL_ACTION_REQUIRED") for name in ("logo", "favicon")}

    def inspect_brand_theme(self, run_id: str, snapshot: dict) -> dict:
        run = self.get_run(run_id); self._assert_state(run, "BRAND_THEME_PREVIEW")
        result = {"assets": self.brand_asset_status(snapshot), "action": "NO_CHANGE",
                  "auto_upload": False, "published_theme": snapshot.get("published_theme")}
        self._update(run_id, gate="HOMEPAGE_PREVIEW", status="READY", checkpoint={"brand_theme": result})
        return result

    def preview_homepage(self, run_id: str, preview: dict) -> dict:
        run = self.get_run(run_id); self._assert_state(run, "HOMEPAGE_PREVIEW")
        result = redact_value(dict(preview))
        result.update({"scope": ["Hero Banner", "Category Shortcuts"], "writes_performed": False,
                       "unrelated_sections_preserved": True, "preview_hash": _hash(preview)})
        self._write_json(run, "homepage_preview.json", result)
        ready = all((result.get("published_theme"), result.get("high_confidence_mapping"),
                     result.get("backup_ready"), result.get("images_approved")))
        result["apply_status"] = "AWAITING_CONFIRMATION" if ready else "MANUAL_ACTION_REQUIRED"
        self._update(run_id, gate="HOMEPAGE_APPLY" if ready else "COMPLETION_CHECK",
                     status=result["apply_status"], checkpoint={"homepage_preview": result})
        return result

    def apply_homepage(self, run_id: str, *, confirmed=False) -> dict:
        run = self.get_run(run_id); self._assert_state(run, "HOMEPAGE_APPLY")
        if confirmed is not True: raise RuntimeError("Theme 실제 변경에 대한 별도 확인이 필요합니다.")
        preview = run["checkpoint"]["homepage_preview"]
        if not all((preview.get("high_confidence_mapping"), preview.get("backup_ready"), preview.get("images_approved"))):
            self.stop(run_id, "theme schema incompatibility/backup required")
        result = redact_value(self.adapter.apply_homepage(preview))
        self._update(run_id, gate="HOMEPAGE_VERIFY", status="VERIFY_REQUIRED",
                     checkpoint={"homepage_results": result}, confirmation={"theme_write": _now()})
        return result

    def verify_homepage(self, run_id: str, verification: dict):
        run = self.get_run(run_id); self._assert_state(run, "HOMEPAGE_VERIFY")
        if not verification.get("verified") or verification.get("theme_drift"):
            self.stop(run_id, "theme drift/verification failure")
        return self._update(run_id, gate="COMPLETION_CHECK", status="READY",
                            checkpoint={"homepage_verify": verification})

    @staticmethod
    def policy_preview(business_input: dict) -> dict:
        required = ("support_email", "legal_name", "address", "return_window", "return_address",
                    "processing_time", "shipping_time", "shipping_fee", "phone", "governing_law")
        missing = [key for key in required if not str(business_input.get(key) or "").strip()]
        return {"status": "DRAFT" if not missing else "REQUIRES_BUSINESS_INPUT",
                "missing_business_input": missing, "publish_allowed": False, "writes_performed": False}

    @staticmethod
    def commerce_readiness(snapshot: dict) -> dict:
        return {name: snapshot.get(name, "MANUAL_ACTION_REQUIRED")
                for name in ("shipping", "tax", "payment", "domain") } | {"read_only": True, "writes_performed": False}

    def complete(self, run_id: str, completion: dict, *, manual_actions=None) -> dict:
        run = self.get_run(run_id); self._assert_state(run, "COMPLETION_CHECK")
        summary = redact_value({**completion, "run_id": run_id, "store_id": self.STORE_ID,
                                "manual_actions": manual_actions or [], "completed_at": _now()})
        self._write_json(run, "completion_summary.json", summary)
        self._final_reports(run, summary)
        self._update(run_id, gate="DONE", status="COMPLETE", checkpoint={"completion": summary})
        return summary

    def _final_reports(self, run, summary):
        report_dir = Path(run["report_dir"])
        cp = run["checkpoint"]
        # Create explicit empty result documents for gates the operator safely skipped.
        for filename in REPORT_FILES:
            path = report_dir / filename
            if path.exists(): continue
            if filename.endswith(".json"):
                payload = {"status": "NOT_RUN", "writes_performed": False}
                if filename == "rollback_manifest.json":
                    payload = {"automatic_delete": False, "delete_mutations": [],
                               "remote_ids": cp.get("remote_ids", []),
                               "safe_restore_requires_confirmation": True}
                self._write_json(run, filename, payload)
        manual = summary.get("manual_actions") or []
        (report_dir / "manual_actions.md").write_text(
            "# 수동 작업\n\n" + ("\n".join(f"- {redact_text(x)}" for x in manual) or "- 없음") + "\n", encoding="utf-8")
        (report_dir / "summary.md").write_text(
            "# Cabin Tidy Controlled Live Pilot\n\n"
            f"- Run: {run['run_id']}\n- Status: {summary.get('status', 'COMPLETE')}\n"
            "- 자동 DELETE: 사용하지 않음\n- 실제 변경은 각 gate의 사용자 확인 후에만 실행\n", encoding="utf-8")
