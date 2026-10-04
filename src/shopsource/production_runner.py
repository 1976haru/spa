"""Evidence-driven, resumable read-only production audit runner.

The runner only gathers local evidence and invokes explicitly configured read
adapters. It never performs Shopify/theme writes or starts paid source checks.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .db import connect
from .production import GATES, ProductionGoldenPathService, _json

EVIDENCE_GATES = GATES[:14]
PASS = {"READY", "READY_WITH_WARNINGS", "VERIFIED"}
DOWNSTREAM = {
    "ENVIRONMENT_STORE_IDENTITY": EVIDENCE_GATES[7:14],
    "SOURCING_QUALITY": EVIDENCE_GATES[2:14],
    "SOURCE_SAFETY": ("PRODUCT_CONTENT", "PRODUCT_MEDIA", "PRICING_MARGIN", "COLLECTION_ARCHITECTURE", "COLLECTION_CATEGORY_MEDIA", "HOMEPAGE", "PRODUCT_COLLECTION_TEMPLATES", "SEO_ACCESSIBILITY_MOBILE", "COMMERCE_READINESS"),
    "PRODUCT_CONTENT": ("PRODUCT_MEDIA", "PRICING_MARGIN", "COLLECTION_ARCHITECTURE", "HOMEPAGE", "PRODUCT_COLLECTION_TEMPLATES", "SEO_ACCESSIBILITY_MOBILE"),
    "PRODUCT_MEDIA": ("COLLECTION_CATEGORY_MEDIA", "HOMEPAGE", "SEO_ACCESSIBILITY_MOBILE"),
    "PRICING_MARGIN": ("COMMERCE_READINESS",),
    "COLLECTION_ARCHITECTURE": ("COLLECTION_CATEGORY_MEDIA", "BRAND_HEADER_NAVIGATION", "HOMEPAGE", "PRODUCT_COLLECTION_TEMPLATES", "SEO_ACCESSIBILITY_MOBILE", "COMMERCE_READINESS"),
    "COLLECTION_CATEGORY_MEDIA": ("HOMEPAGE", "SEO_ACCESSIBILITY_MOBILE"),
    "BRAND_HEADER_NAVIGATION": ("HOMEPAGE", "SEO_ACCESSIBILITY_MOBILE", "COMMERCE_READINESS"),
    "HOMEPAGE": ("SEO_ACCESSIBILITY_MOBILE",),
    "PRODUCT_COLLECTION_TEMPLATES": ("SEO_ACCESSIBILITY_MOBILE",),
    "PAGES_POLICIES": ("COMMERCE_READINESS",),
    "SEO_ACCESSIBILITY_MOBILE": ("COMMERCE_READINESS",),
}


def _hash(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ProductionEvidenceRunner:
    """Advance available G0-G13 checks and persist evidence at each boundary."""

    def __init__(self, *, db=None, service=None, collectors=None, source_preview=None):
        self.db = db
        self.service = service or ProductionGoldenPathService(db=db)
        self.collectors = collectors or {}
        self.source_preview = source_preview
        with connect(db) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS production_runner_locks(
                store_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, acquired_at TEXT NOT NULL)""")

    def active_run(self, store_id):
        with connect(self.db) as con:
            row = con.execute("""SELECT run_id FROM production_runs WHERE store_id=?
                AND status NOT IN ('COMPLETE','CANCELLED')
                ORDER BY updated_at DESC LIMIT 1""", (str(store_id),)).fetchone()
        return self.service.get(row["run_id"]) if row else None

    def start_or_resume(self, store_id, *, new_run=False, confirmed=False):
        active = self.active_run(store_id)
        if active and not new_run:
            with connect(self.db) as con:
                lock = con.execute("SELECT run_id FROM production_runner_locks WHERE store_id=?", (str(store_id),)).fetchone()
                if lock and lock["run_id"] != active["run_id"]:
                    raise RuntimeError("another production evidence run owns this store")
                con.execute("INSERT INTO production_runner_locks(store_id,run_id,acquired_at) VALUES(?,?,?) ON CONFLICT(store_id) DO UPDATE SET run_id=excluded.run_id,acquired_at=excluded.acquired_at",
                            (str(store_id), active["run_id"], datetime.now(timezone.utc).isoformat()))
            return active
        if new_run and not confirmed:
            raise PermissionError("새 production 점검은 명시적 확인이 필요합니다.")
        now = datetime.now(timezone.utc).isoformat()
        with connect(self.db) as con:
            lock = con.execute("SELECT run_id FROM production_runner_locks WHERE store_id=?", (str(store_id),)).fetchone()
            if lock and lock["run_id"] == "STARTING":
                raise RuntimeError("a production evidence run is already starting for this store")
            con.execute("INSERT INTO production_runner_locks(store_id,run_id,acquired_at) VALUES(?,?,?) ON CONFLICT(store_id) DO UPDATE SET run_id=excluded.run_id,acquired_at=excluded.acquired_at",
                        (str(store_id), "STARTING", now))
        try:
            run = self.service.start(str(store_id))
            with connect(self.db) as con:
                con.execute("UPDATE production_runner_locks SET run_id=?,acquired_at=? WHERE store_id=? AND run_id='STARTING'",
                            (run["run_id"], datetime.now(timezone.utc).isoformat(), str(store_id)))
            return self.service.get(run["run_id"])
        except Exception:
            with connect(self.db) as con:
                con.execute("DELETE FROM production_runner_locks WHERE store_id=? AND run_id='STARTING'", (str(store_id),))
            raise

    def run(self, store_id, *, run_id=None):
        run = self.service.get(run_id) if run_id else (self.active_run(store_id) or self.start_or_resume(store_id))
        if str(run["store_id"]) != str(store_id):
            raise ValueError("production run store mismatch")
        with connect(self.db) as con:
            row = con.execute("SELECT run_id FROM production_runner_locks WHERE store_id=?", (str(store_id),)).fetchone()
            if row and row["run_id"] != run["run_id"]:
                raise RuntimeError("another production evidence run owns this store")
            con.execute("INSERT INTO production_runner_locks(store_id,run_id,acquired_at) VALUES(?,?,?) ON CONFLICT(store_id) DO UPDATE SET run_id=excluded.run_id,acquired_at=excluded.acquired_at",
                        (str(store_id), run["run_id"], datetime.now(timezone.utc).isoformat()))

        for key in EVIDENCE_GATES:
            run = self.service.get(run["run_id"])
            gate = next(item for item in run["gates"] if item["gate_key"] == key)
            # Existing verified evidence is re-used until its input fingerprint changes.
            result = self._collect(key, str(store_id), run)
            fingerprint = _hash(result.get("fingerprint_input", result))
            previous = gate.get("evidence") or {}
            if gate["status"] in PASS and previous.get("fingerprint") == fingerprint:
                continue
            if gate["status"] in PASS and previous.get("fingerprint") and previous.get("fingerprint") != fingerprint:
                self._invalidate_downstream(run["run_id"], key)
            result.pop("fingerprint_input", None)
            result["fingerprint"] = fingerprint
            self.service.update(run["run_id"], {key: result})
        run = self.service.get(run["run_id"])
        states = [g["status"] for g in run["gates"][:14]]
        if all(state in PASS for state in states):
            with connect(self.db) as con:
                con.execute("UPDATE production_runs SET status='READY_FOR_PILOT',updated_at=? WHERE run_id=?",
                            (datetime.now(timezone.utc).isoformat(), run["run_id"]))
            run = self.service.get(run["run_id"])
            run["status"] = "READY_FOR_PILOT"
        # The runner intentionally never advances CONTROLLED_LIVE_PILOT/G14.
        return run

    def _invalidate_downstream(self, run_id, changed_key):
        affected = DOWNSTREAM.get(changed_key, ())
        if not affected:
            return
        now = datetime.now(timezone.utc).isoformat()
        with connect(self.db) as con:
            for key in affected:
                con.execute("UPDATE production_gates SET status='NOT_STARTED',evidence_json='{}',blockers_json='[]',updated_at=? WHERE run_id=? AND gate_key=? AND status IN ('READY','READY_WITH_WARNINGS','VERIFIED')",
                            (now, run_id, key))

    def confirm_source_audit(self, run_id, observations, *, confirmed=False):
        if not confirmed:
            raise PermissionError("실제 Source 검사는 사용자 승인이 필요합니다.")
        from .source_safety import SourceMonitorService
        monitor = SourceMonitorService(self.db)
        preview = monitor.preview_due_checks(self.service.get(run_id)["store_id"], limit=50000)
        status = monitor.run_due_checks(self.service.get(run_id)["store_id"], observations=observations, limit=50000)
        # Provider failures remain errors/unknown and are never rewritten as OOS.
        counts = {"checked": status.get("checked_count", 0), "failed": status.get("failed_count", 0),
                  "preview_count": len(preview["items"])}
        if status["status"] == "COMPLETE":
            evidence = {"status": "VERIFIED", "verified": True, "counts": counts,
                        "provider_errors_are_not_oos": True,
                        "fingerprint_input": {"run_id": status["run_id"], "counts": counts}}
        else:
            evidence = {"status": "WAITING_FOR_INPUT", "missing_inputs": ["일부 원본 확인에 실패했습니다. 실패 항목만 재검토하세요."],
                        "counts": counts, "provider_errors_are_not_oos": True,
                        "fingerprint_input": {"run_id": status["run_id"], "counts": counts}}
        self.service.update(run_id, {"SOURCE_SAFETY": evidence})
        continued = self.run(self.service.get(run_id)["store_id"], run_id=run_id)
        return {**counts, "status": status["status"], "production_run": continued}

    def _collect(self, key, store_id, run):
        injected = self.collectors.get(key)
        if injected:
            return dict(injected(store_id, run) or {})
        if key == "ENVIRONMENT_STORE_IDENTITY": return self._environment(store_id)
        if key == "SOURCING_QUALITY": return self._sourcing(store_id)
        if key == "SOURCE_SAFETY": return self._source(store_id)
        if key == "PRODUCT_CONTENT": return self._content(store_id)
        if key == "PRODUCT_MEDIA": return self._media(store_id)
        if key == "PRICING_MARGIN": return self._pricing(store_id)
        if key == "COLLECTION_ARCHITECTURE": return self._collections(store_id)
        if key == "COLLECTION_CATEGORY_MEDIA": return self._collection_media(store_id)
        if key == "BRAND_HEADER_NAVIGATION": return self._navigation(store_id)
        if key == "HOMEPAGE": return self._homepage(store_id)
        if key == "PRODUCT_COLLECTION_TEMPLATES": return self._templates(store_id)
        if key == "PAGES_POLICIES": return self._read_adapter("pages", store_id)
        if key == "SEO_ACCESSIBILITY_MOBILE": return self._read_adapter("seo_mobile", store_id)
        if key == "COMMERCE_READINESS": return self._read_adapter("commerce", store_id)
        return {"status": "REVIEW_REQUIRED", "review_required": ["No evidence collector configured"]}

    def _environment(self, store_id):
        if store_id != "001":
            return {"status": "BLOCKED", "blockers": ["Expected Store 001 | Cabin Tidy"]}
        local = self._local_environment()
        try:
            from .shopify_collections import get_connection, get_shopify_token
            connection = get_connection(store_id, db=self.db)
            token, _source = get_shopify_token(store_id)
            if not connection or not token:
                return {**local, "status": "WAITING_FOR_CREDENTIALS", "missing_inputs": ["Configure Cabin Tidy Shopify connection and credential"],
                        "credential_present": bool(token), "shop_domain": (connection or {}).get("shop_domain"),
                        "fingerprint_input": {"connection": connection, "credential_present": bool(token)}}
            from .shopify_pilot import ShopifyLivePilot
            preflight = ShopifyLivePilot(db=self.db).connection_preflight(store_id)
            if preflight.get("errors"):
                return {**local, "status": "WAITING_FOR_INPUT", "shop_domain": connection.get("shop_domain"),
                        "api_version": connection.get("api_version"), "credential_present": True,
                        "missing_inputs": ["Shopify API 연결과 실제 권한을 확인하세요"],
                        "granted_scopes": sorted(name for name in ("read_products", "write_products", "read_publications", "write_publications", "write_files") if preflight.get(name)),
                        "secret_values_exposed": False, "fingerprint_input": {"domain": connection.get("shop_domain"), "scopes": preflight}}
            from .homepage_collections import ShopifyThemeReader
            theme = ShopifyThemeReader(db=self.db).discover(store_id)
            if theme.get("status") == "MISSING_READ_SCOPE":
                return {"status": "WAITING_FOR_INPUT", "missing_inputs": ["Grant Shopify read_themes scope"],
                        "shop_domain": connection["shop_domain"], "theme_status": theme.get("status"),
                        "fingerprint_input": {"domain": connection["shop_domain"], "scopes": theme.get("scopes")}}
            return {**local, "status": "VERIFIED" if theme.get("theme") else "REVIEW_REQUIRED",
                    "verified": bool(theme.get("theme")), "shop_domain": connection["shop_domain"],
                    "api_version": connection.get("api_version"), "theme_status": theme.get("status"),
                    "theme_name": (theme.get("theme") or {}).get("name"),
                    "missing_inputs": ["Shopify 관리자에서 primary domain 및 SSL 상태 확인"] if theme.get("theme") else [],
                    "granted_scopes": sorted(theme.get("scopes") or []),
                    "review_required": [] if theme.get("theme") else ["Published theme could not be verified"],
                    "credential_present": True, "secret_values_exposed": False,
                    "fingerprint_input": {"domain": connection["shop_domain"], "theme": theme.get("theme"), "scopes": theme.get("scopes")}}
        except (TimeoutError, ConnectionError) as exc:
            return {"status": "FAILED_TRANSIENT", "review_required": [type(exc).__name__], "secret_values_exposed": False}
        except Exception as exc:
            from .security import redact_text
            text = redact_text(str(exc))
            lowered = text.casefold()
            state = "WAITING_FOR_CREDENTIALS" if "credential" in lowered or "token" in lowered else "REVIEW_REQUIRED"
            field = "missing_inputs" if state == "WAITING_FOR_CREDENTIALS" else "review_required"
            return {"status": state, field: [text[:240]], "secret_values_exposed": False}

    def _local_environment(self):
        import importlib.util
        try:
            with connect(self.db) as con:
                db_ok = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        except Exception:
            db_ok = False
        return {"local_db_integrity": db_ok, "pillow_available": importlib.util.find_spec("PIL") is not None,
                "prompt_manual_image_fallback": True, "local_image_studio_optional": True,
                "secrets_exposed": False}

    def _sourcing(self, store_id):
        from .production import ProductionGoldenPathService
        rows = ProductionGoldenPathService(db=self.db).audit_master(store_id)
        counts = {name: sum(row.get("classification") == name for row in rows)
                  for name in ("PRODUCTION_CANDIDATE", "RESERVE", "REVIEW_REQUIRED", "REJECT_FOR_STORE", "RESTRICTED")}
        counts["total"] = len(rows)
        counts["duplicate"] = sum("DUPLICATE_OR_IDENTITY_CONFLICT" in row.get("reasons", []) for row in rows)
        counts["missing_source"] = sum("MISSING_SOURCE_REFERENCE" in row.get("reasons", []) for row in rows)
        counts["variant_bundle_fitment_review"] = sum(any("VARIANT" in x or "BUNDLE" in x or "FITMENT" in x for x in row.get("reasons", [])) for row in rows)
        return {"status": "VERIFIED" if rows else "BLOCKED", "verified": bool(rows), "counts": counts,
                "blockers": [] if rows else ["MASTER catalog is empty"], "fingerprint_input": counts}

    def _source(self, store_id):
        from .source_safety import SourceMonitorService
        preview = self.source_preview(store_id) if self.source_preview else SourceMonitorService(self.db).preview_due_checks(store_id, limit=50000)
        with connect(self.db) as con:
            freshness = {r["freshness_status"]: r["n"] for r in con.execute("SELECT freshness_status,COUNT(*) n FROM source_monitoring_state GROUP BY freshness_status")}
        count = len(preview.get("items", []))
        if count:
            return {"status": "WAITING_FOR_CONFIRMATION", "missing_inputs": ["Explicit approval required for live source checks"],
                    "target_count": count, "estimated_batches": preview.get("estimated_batches", 0),
                    "estimated_tokens": preview.get("estimated_tokens"), "fresh": freshness.get("FRESH", 0),
                    "stale": freshness.get("STALE_WARNING", 0) + freshness.get("STALE_BLOCKED", 0),
                    "never_verified": freshness.get("NEVER_VERIFIED", 0), "preview_only": True,
                    "fingerprint_input": {"target_count": count, "freshness": freshness}}
        return {"status": "VERIFIED", "verified": True, "target_count": 0, "preview_only": True,
                "fingerprint_input": freshness}

    def _catalog_rows(self, store_id):
        from .production import ProductionGoldenPathService
        return ProductionGoldenPathService(db=self.db).audit_master(store_id)

    def _content(self, store_id):
        rows = self._catalog_rows(store_id)
        issues = sum(any(reason.startswith("CONTENT_") for reason in row.get("reasons", [])) for row in rows)
        return {"status": "VERIFIED" if rows and not issues else ("REVIEW_REQUIRED" if rows else "BLOCKED"),
                "ready": bool(rows) and not issues, "counts": {"total": len(rows), "content_review": issues},
                "review_required": [] if rows and not issues else ["Local title/description/features/variant/SEO/handle evidence needs review"],
                "shopify_write_performed": False, "fingerprint_input": {"total": len(rows), "content_review": issues}}

    def _media(self, store_id):
        rows = self._catalog_rows(store_id)
        with connect(self.db) as con:
            rights = {r["product_id"]: r["policy"] for r in con.execute("SELECT product_id,policy FROM product_media_rights WHERE store_id=?", (store_id,))}
        missing = sum(rights.get(row.get("product_id"), row.get("media_policy")) not in {"SUPPLIER_AUTHORIZED", "MERCHANT_OWNED", "LICENSED"} for row in rows)
        unresolved = missing or not rows
        return {"status": "WAITING_FOR_INPUT" if unresolved else "VERIFIED", "verified": not unresolved,
                "counts": {"total": len(rows), "rights_review_required": missing},
                "missing_inputs": (["Review product-image usage rights in bulk"] if missing else []) + (["No production candidates are available for exact-media review"] if not rows else []),
                "rights_auto_assigned": False, "fingerprint_input": rights}

    def _pricing(self, store_id):
        from .source_safety import SourceSafetyService
        policy = SourceSafetyService(self.db).settings(store_id)["price_policy"]
        ready = bool(policy.get("enabled") and policy.get("min_margin_amount") is not None and policy.get("min_margin_percent") is not None)
        return {"status": "VERIFIED" if ready else "WAITING_FOR_INPUT", "verified": ready,
                "missing_inputs": [] if ready else ["Set currency, cost buffers, minimum margin and unknown-fee handling"],
                "auto_reprice_enabled": bool(policy.get("auto_reprice_enabled")), "policy_configured": ready,
                "fingerprint_input": policy}

    def _collections(self, store_id):
        from .collection_planner import CollectionPlanner
        planner = CollectionPlanner(self.db)
        plan = planner._latest_plan(store_id)
        if not plan:
            try:
                plan = planner.create_plan(store_id)
            except Exception:
                plan = None
        if not plan:
            return {"status": "WAITING_FOR_INPUT", "missing_inputs": ["Local collection plan cannot be derived from current store data"], "fingerprint_input": {"plan": None}}
        rows = plan.get("collections", [])
        empty = sum(not int(row.get("estimated_product_count") or 0) for row in rows)
        return {"status": "VERIFIED" if rows and not empty else "REVIEW_REQUIRED", "verified": bool(rows and not empty),
                "counts": {"collections": len(rows), "empty": empty, "unmatched": plan.get("unmatched_product_count", 0)},
                "review_required": [] if rows and not empty else ["Empty or missing collection coverage needs review"],
                "plan_id": plan.get("plan_id"), "fingerprint_input": {"plan_id": plan.get("plan_id"), "collections": rows}}

    def _collection_media(self, store_id):
        from .collection_images import approved_collection_images
        approved = approved_collection_images(store_id, db=self.db)
        from .collection_planner import CollectionPlanner
        plan = CollectionPlanner(self.db)._latest_plan(store_id)
        definitions = (plan or {}).get("collections", [])
        count = len(definitions)
        missing_keys = [row.get("collection_key") for row in definitions if row.get("collection_key") not in approved]
        missing = len(missing_keys)
        return {"status": "WAITING_FOR_INPUT" if missing else "VERIFIED", "verified": not missing,
                "counts": {"collections": count, "approved_images": len(approved), "missing_images": missing},
                "missing_collection_keys": missing_keys,
                "missing_inputs": ["Prepare and approve collection/category images"] if missing else [],
                "fingerprint_input": {"approved": approved}}

    def _read_adapter(self, name, store_id):
        adapter = self.collectors.get("remote_" + name)
        if not adapter:
            return {"status": "WAITING_FOR_INPUT", "missing_inputs": [f"Read-only {name} evidence is unavailable; verify in Shopify"],
                    "fingerprint_input": {"adapter": None}}
        evidence = dict(adapter(store_id) or {})
        if name == "seo_mobile" and not evidence.get("human_visual_signed_off"):
            evidence["status"] = "WAITING_FOR_INPUT"
            evidence["missing_inputs"] = list(evidence.get("missing_inputs") or []) + ["Desktop/mobile visual review sign-off is required"]
            evidence["wcag_automatically_certified"] = False
        evidence.setdefault("fingerprint_input", evidence.copy())
        return evidence

    def _navigation(self, store_id):
        adapter = self.collectors.get("remote_navigation")
        try:
            if adapter:
                data = dict(adapter(store_id) or {})
            else:
                from .navigation import NavigationService
                data = NavigationService(db=self.db).discover_main_menu(store_id)
            menu = data.get("menu") or {}
            flat = menu.get("items") or []
            links = []
            duplicates = set()
            seen = {}
            stack = list(flat)
            while stack:
                item = stack.pop()
                stack.extend(item.get("items") or [])
                target = item.get("resourceId") or item.get("url")
                links.append({"label": item.get("title"), "target": target})
                if not target or str(target).strip() == "#":
                    duplicates.add("blank/# link")
                if target:
                    seen.setdefault(str(target), set()).add(str(item.get("title") or ""))
            wrong = [target for target, labels in seen.items() if len(labels) > 1]
            if wrong:
                duplicates.add("duplicate collection target")
            ready = data.get("status") == "FOUND" and not duplicates
            state = "VERIFIED" if ready else ("WAITING_FOR_INPUT" if data.get("status") in {"NOT_CONNECTED", "MANUAL_ACTION_REQUIRED"} else "REVIEW_REQUIRED")
            brand_assets = {"logo": "UNKNOWN", "favicon": "UNKNOWN"}
            try:
                from .brand_automation import detect_brand_settings
                theme = self._theme_snapshot(store_id)
                if theme.get("status") == "CONNECTED":
                    detected = detect_brand_settings((theme.get("theme_files") or {}).get("config/settings_schema.json", "[]"),
                                                     (theme.get("theme_files") or {}).get("config/settings_data.json", "{}"))
                    brand_assets = {kind: (value.get("status") if value.get("current") else "MISSING")
                                    for kind, value in detected.items()}
            except Exception:
                pass
            if "MISSING" in brand_assets.values() or "UNKNOWN" in brand_assets.values():
                ready = False
                state = "WAITING_FOR_INPUT"
            return {"status": state, "verified": ready, "menu_status": data.get("status"),
                    "link_count": len(links), "issues": sorted(duplicates),
                    "brand_assets": brand_assets,
                    "missing_inputs": ([data.get("reason") or "Shopify navigation requires review"] if data.get("status") != "FOUND" else []) + (sorted(duplicates)) + (["Verify existing logo and favicon in the published theme"] if "MISSING" in brand_assets.values() or "UNKNOWN" in brand_assets.values() else []),
                    "write_performed": False,
                    "fingerprint_input": {"menu": menu, "status": data.get("status"), "scopes": data.get("scopes")}}
        except (TimeoutError, ConnectionError) as exc:
            return {"status": "FAILED_TRANSIENT", "review_required": [type(exc).__name__], "write_performed": False}

    def _theme_snapshot(self, store_id):
        from .homepage_collections import ShopifyThemeReader
        return ShopifyThemeReader(db=self.db).discover(store_id)

    def _homepage(self, store_id):
        try:
            data = self._theme_snapshot(store_id)
            if data.get("status") != "CONNECTED":
                return {"status": "WAITING_FOR_INPUT", "missing_inputs": [data.get("warning") or "Published theme cannot be read"],
                        "theme_status": data.get("status"), "write_performed": False,
                        "fingerprint_input": {"status": data.get("status"), "theme": data.get("theme")}}
            from .homepage_automation import discover_homepage_sections
            sections = discover_homepage_sections(data.get("theme_files") or {})
            template = data.get("template") or {}
            homepage_sections = [value for value in (template.get("sections") or {}).values() if isinstance(value, dict)]
            section_types = {value.get("type") for value in homepage_sections}
            hero_schema, category_schema = sections.get("hero") or {}, sections.get("category") or {}
            hero_present = hero_schema.get("type") in section_types
            category_present = category_schema.get("type") in section_types
            hero_settings = [value.get("settings") or {} for value in homepage_sections if value.get("type") == hero_schema.get("type")]
            hero_configured = any(any(settings.get(key) for key in ("image", "image_desktop", "banner_image")) and
                                  any(settings.get(key) for key in ("button_link", "link", "url")) for settings in hero_settings)
            complete = hero_present and hero_configured and category_present
            return {"status": "VERIFIED" if complete else "WAITING_FOR_INPUT", "verified": complete,
                    "hero_detected": hero_present, "hero_image_and_cta_configured": hero_configured,
                    "category_section_detected": category_present,
                    "theme_id": (data.get("theme") or {}).get("id"), "section_count": len(section_types),
                    "missing_inputs": [] if complete else ["Published homepage must contain a configured Hero image/CTA and category section; inspect preview/diff"],
                    "theme_write_performed": False,
                    "fingerprint_input": {"theme": data.get("theme"), "template": template, "sections": sections}}
        except (TimeoutError, ConnectionError) as exc:
            return {"status": "FAILED_TRANSIENT", "review_required": [type(exc).__name__], "theme_write_performed": False}

    def _templates(self, store_id):
        try:
            data = self._theme_snapshot(store_id)
            if data.get("status") != "CONNECTED":
                return {"status": "WAITING_FOR_INPUT", "missing_inputs": [data.get("warning") or "Theme schema is unavailable"],
                        "fingerprint_input": {"status": data.get("status")}}
            from .store_completion import inspect_product_template, inspect_collection_template
            files = data.get("theme_files") or {}
            product = inspect_product_template(files)
            collection = inspect_collection_template(files)
            complete = product.get("status") == "PRESENT" and collection.get("status") == "PRESENT"
            return {"status": "VERIFIED" if complete else "WAITING_FOR_INPUT", "verified": complete,
                    "product_template": product, "collection_template": collection,
                    "missing_inputs": [] if complete else ["Unknown or missing theme template features require manual review"],
                    "fingerprint_input": {"theme": data.get("theme"), "product": product, "collection": collection}}
        except (TimeoutError, ConnectionError) as exc:
            return {"status": "FAILED_TRANSIENT", "review_required": [type(exc).__name__]}
