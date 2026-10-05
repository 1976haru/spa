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


class _KeepaObservationProvider:
    """Opt-in Keepa adapter; only documented normalized offer evidence counts."""
    def __init__(self, api_key=None, *, client=None):
        from .sourcing.providers.keepa import KeepaProvider
        self.client = client or KeepaProvider(api_key=api_key)
        self.last_tokens_consumed = 0

    def health(self):
        return self.client.health()

    def observe_batch(self, items):
        from .source_safety import keepa_source_observation
        asins = [str(row.get("asin") or "").upper() for row in items if row.get("asin")]
        batch = self.client.hydrate(asins)
        telemetry = getattr(batch, "telemetry", None)
        self.last_tokens_consumed = int(getattr(telemetry, "tokens_consumed", 0) or 0)
        products = {str(row.get("asin") or "").upper(): row for row in batch.products}
        result = {}
        for item in items:
            asin = str(item.get("asin") or "").upper()
            raw = products.get(asin)
            if raw is None:
                result[item["product_id"]] = keepa_source_observation(None, provider_error="missing product")
            else:
                stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
                current = stats.get("current") if isinstance(stats.get("current"), list) else []
                product_type = raw.get("productType")
                # Keepa documents stats.current[1]=Marketplace NEW price and
                # [11]=COUNT_NEW. Values are cents for Amazon US (domain 1).
                new_price = current[1] if len(current) > 1 else None
                new_count = current[11] if len(current) > 11 else None
                if product_type != 0 or raw.get("domainId") != 1 or not isinstance(new_count, int) or new_count < 0:
                    normalized = keepa_source_observation({})  # UNKNOWN: no usable offer evidence
                else:
                    normalized = keepa_source_observation({
                        "current_new_price": new_price / 100 if isinstance(new_price, int) and new_price > 0 else None,
                        "current_new_offer_count": new_count,
                        "documented_no_new_offer": new_count == 0,
                        "currency": "USD", "provider_updated_at": str(raw.get("lastUpdate") or "") or None,
                    })
                normalized["evidence"] = {"provider": "Keepa", "domain_id": raw.get("domainId"),
                                          "product_type": product_type, "new_offer_count": new_count,
                                          "stat_price_type": "NEW", "offer_request_used": False}
                normalized["source_platform"] = "AMAZON"
                normalized["source_kind"] = "KEEPA"
                result[item["product_id"]] = normalized
        return result


class ProductionEvidenceRunner:
    """Advance available G0-G13 checks and persist evidence at each boundary."""

    def __init__(self, *, db=None, service=None, collectors=None, source_preview=None, source_provider=None):
        self.db = db
        self.service = service or ProductionGoldenPathService(db=db)
        self.collectors = collectors or {}
        self.source_preview = source_preview
        self.source_provider = source_provider
        from .source_provider_profiles import SourceProviderProfiles
        self.source_profiles = SourceProviderProfiles(db)
        with connect(db) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS production_runner_locks(
                store_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, acquired_at TEXT NOT NULL)""")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS production_manual_evidence(
                  run_id TEXT NOT NULL, gate_key TEXT NOT NULL, evidence_kind TEXT NOT NULL,
                  fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL, confirmed_at TEXT NOT NULL,
                  PRIMARY KEY(run_id,gate_key,evidence_kind));
                CREATE TABLE IF NOT EXISTS production_business_inputs(
                  store_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)

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

    def _legacy_confirm_source_audit(self, run_id, observations, *, confirmed=False):
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

    def confirm_source_audit(self, run_id, observations=None, *, confirmed=False):
        """Run explicitly approved source checks in durable 100-item batches."""
        from .source_safety import SourceMonitorService
        run = self.service.get(run_id)
        current_checkpoint = run.get("checkpoint", {}).get("source_audit", {})
        if not confirmed and not current_checkpoint.get("approved_at"):
            raise PermissionError("Explicit user confirmation is required before source-provider calls")
        store_id = str(run["store_id"])
        monitor = SourceMonitorService(self.db)
        prior_checkpoint = run.get("checkpoint", {}).get("source_audit", {})
        resume_offset = int(prior_checkpoint.get("completed", 0))
        # Previously successful checks are scheduled for a later interval, so
        # the due-only preview naturally contains only unfinished/retry items.
        preview = monitor.preview_due_checks(store_id, limit=50000)
        production_profile = self.source_profiles.for_store(store_id)
        injected_fixture = observations is not None or self.source_provider is not None
        if preview["items"] and observations is None and self.source_provider is None:
            try:
                if not production_profile:
                    production_profile = self.source_profiles.register_env_compatibility(store_id)
                if not production_profile:
                    raise RuntimeError("not configured")
                preflight = self.source_profiles.preflight(store_id, target_count=len(preview["items"]), batch_size=100)
                pilot = prior_checkpoint.get("provider_pilot", {})
                if preflight["status"] != "READY" or pilot.get("status") != "PASS":
                    raise PermissionError("Provider health and a passing 100-item provider pilot are required before full source audit")
                client, _ = self.source_profiles.create_keepa_provider(production_profile["profile_id"])
                self.source_provider = _KeepaObservationProvider(client=client)
            except Exception:
                # Crucially, approval is not checkpointed until all local/provider gates pass.
                raise RuntimeError("Source provider is not ready. Configure credentials, pass Health check and Provider Pilot, then approve the full audit.") from None
        if confirmed and not current_checkpoint.get("approved_at"):
            if preview["items"] and not injected_fixture:
                preflight = self.source_profiles.preflight(store_id, target_count=len(preview["items"]), batch_size=100)
                if preflight["status"] != "READY" or current_checkpoint.get("provider_pilot", {}).get("status") != "PASS":
                    raise PermissionError("Provider preflight and pilot must pass before approval is recorded")
            current_checkpoint["approved_at"] = datetime.now(timezone.utc).isoformat()
            current_checkpoint["control"] = "RUNNING"
            with connect(self.db) as con:
                con.execute("UPDATE production_runs SET checkpoint_json=?,updated_at=? WHERE run_id=?",
                            (_json({**run.get("checkpoint", {}), "source_audit": current_checkpoint}),
                             datetime.now(timezone.utc).isoformat(), run_id))
        checked = failed = provider_errors = 0
        target_count = int(prior_checkpoint.get("target_count") or (len(preview["items"]) + resume_offset))
        checkpoint = {"target_count": target_count, "completed": resume_offset, "batch_size": 100,
                      "approved_at": current_checkpoint.get("approved_at"), "control": "RUNNING"}
        for offset in range(0, len(preview["items"]), 100):
            current = self.service.get(run_id).get("checkpoint", {}).get("source_audit", {})
            if current.get("control") in {"PAUSED", "STOPPED"}:
                checkpoint.update({"completed": resume_offset + offset, "control": current["control"],
                                   "approved_at": current.get("approved_at")})
                with connect(self.db) as con:
                    con.execute("UPDATE production_runs SET checkpoint_json=?,updated_at=? WHERE run_id=?",
                                (_json({**run.get("checkpoint", {}), "source_audit": checkpoint}),
                                 datetime.now(timezone.utc).isoformat(), run_id))
                break
            batch = preview["items"][offset:offset + 100]
            errors_before_batch = provider_errors
            if observations is not None:
                batch_observations = observations
            else:
                batch_observations = None
                for attempt in range(4):
                    try:
                        batch_observations = self.source_provider.observe_batch(batch)
                        break
                    except (TimeoutError, ConnectionError):
                        if attempt == 3: break
                        import time
                        time.sleep(0.25 * (2 ** attempt))
                    except Exception:
                        break
                if batch_observations is None:
                    batch_observations = {row["product_id"]: TimeoutError("source provider unavailable") for row in batch}
            normalized = {}
            for row in batch:
                value = (batch_observations or {}).get(row["product_id"])
                if isinstance(value, Exception):
                    value = {"availability": "SOURCE_ERROR", "availability_confidence": "UNKNOWN",
                             "evidence_kind": "PROVIDER_ERROR", "evidence": {"error_type": type(value).__name__}}
                if value: normalized[row["product_id"]] = value
                if isinstance(value, dict) and value.get("availability") == "SOURCE_ERROR":
                    provider_errors += 1
            # Each previous batch is rescheduled into the future; the next batch
            # is therefore at offset 0 in the newly-computed due set.
            status = monitor.run_due_checks(store_id, observations=normalized, limit=len(batch), offset=0)
            checked += int(status.get("checked_count", 0)); failed += int(status.get("failed_count", 0))
            control_after_batch = self.service.get(run_id).get("checkpoint", {}).get("source_audit", {}).get("control", "RUNNING")
            checkpoint.update({"completed": (resume_offset + offset + len(batch)
                                               if failed == 0 and provider_errors == errors_before_batch
                                               else resume_offset + offset), "failed": failed,
                               "last_source_check_run_id": status["run_id"], "last_batch_offset": offset,
                               "approved_at": current_checkpoint.get("approved_at"), "control": control_after_batch})
            with connect(self.db) as con:
                con.execute("UPDATE production_runs SET checkpoint_json=?,updated_at=? WHERE run_id=?",
                            (_json({**run.get("checkpoint", {}), "source_audit": checkpoint}),
                             datetime.now(timezone.utc).isoformat(), run_id))
            if failed or provider_errors > errors_before_batch or control_after_batch in {"PAUSED", "STOPPED"}: break
        with connect(self.db) as con:
            ids = [row[0] for row in con.execute("SELECT product_id FROM source_monitoring_state ORDER BY product_id")]
        monitor.safety.evaluate_many(store_id, ids)
        counts = {"checked": resume_offset + checked, "failed": failed, "provider_errors": provider_errors,
                  "preview_count": target_count,
                  "batch_size": 100, "estimated_tokens": preview.get("estimated_tokens")}
        complete = counts["checked"] == counts["preview_count"] and failed == 0 and provider_errors == 0
        evidence = {"status": "VERIFIED" if complete else "WAITING_FOR_INPUT", "verified": complete,
                    "missing_inputs": [] if complete else ["Some source checks failed or remain unresolved; retry failed items or provide source evidence."],
                    "counts": counts, "provider_errors_are_not_oos": True,
                    "fingerprint_input": {"counts": counts, "last_source_check_run_id": checkpoint.get("last_source_check_run_id")}}
        self.service.update(run_id, {"SOURCE_SAFETY": evidence})
        continued = self.run(store_id, run_id=run_id)
        return {**counts, "status": "COMPLETE" if complete else "FAILED", "production_run": continued}

    def source_provider_preflight(self, run_id, *, pilot=False):
        """Return a local-only cost/token/health preflight; never contacts Keepa."""
        from .source_safety import SourceMonitorService
        run = self.service.get(run_id)
        preview = SourceMonitorService(self.db).preview_due_checks(str(run["store_id"]), limit=50000)
        profile = self.source_profiles.for_store(str(run["store_id"]))
        if not profile:
            profile = self.source_profiles.register_env_compatibility(str(run["store_id"]))
        target = min(100, len(preview["items"])) if pilot else len(preview["items"])
        result = self.source_profiles.preflight(str(run["store_id"]), target_count=target, batch_size=100)
        result.update({"pilot": bool(pilot), "full_due_count": len(preview["items"]),
                       "fresh": sum(row.get("freshness_status") == "FRESH" for row in preview["items"]),
                       "stale": sum(row.get("freshness_status") in {"STALE_WARNING", "STALE_BLOCKED"} for row in preview["items"]),
                       "never_verified": sum(row.get("freshness_status") == "NEVER_VERIFIED" for row in preview["items"]),
                       "estimated_cost": "UNKNOWN"})
        checkpoint = run.get("checkpoint", {}).get("source_audit", {})
        result["pilot_status"] = checkpoint.get("provider_pilot", {}).get("status", "NOT_RUN")
        result["can_run_pilot"] = result["usable"] and target > 0
        result["can_run_full"] = result["usable"] and result["pilot_status"] == "PASS" and len(preview["items"]) > 0
        return result

    def source_provider_health_check(self, run_id, *, provider=None):
        run = self.service.get(run_id)
        store_id = str(run["store_id"])
        if not self.source_profiles.for_store(store_id):
            self.source_profiles.register_env_compatibility(store_id)
        return self.source_profiles.health_check(store_id, provider=provider)

    def run_source_provider_pilot(self, run_id, *, confirmed=False, observations=None):
        """Run one explicitly approved provider-normalization batch (max 100); never verifies G2."""
        if not confirmed:
            raise PermissionError("Explicit approval is required for the 100-item source-provider pilot")
        from .source_safety import AVAILABILITY, SourceMonitorService
        run = self.service.get(run_id); store_id = str(run["store_id"])
        monitor = SourceMonitorService(self.db)
        preview = monitor.preview_due_checks(store_id, limit=100)
        if not preview["items"]:
            raise ValueError("No due source products are available for provider pilot")
        preflight = self.source_profiles.preflight(store_id, target_count=len(preview["items"]), batch_size=100)
        if self.source_provider is None and preflight["status"] != "READY":
            raise PermissionError("Provider credential and passing health check are required")
        provider = self.source_provider
        if observations is None:
            if provider is None:
                profile = self.source_profiles.for_store(store_id)
                client, _ = self.source_profiles.create_keepa_provider(profile["profile_id"])
                provider = _KeepaObservationProvider(client=client)
            try:
                observations = provider.observe_batch(preview["items"])
            except Exception as exc:
                raise RuntimeError(f"Provider pilot failed safely ({type(exc).__name__}); no G2 approval was recorded") from None
        normalized = {}
        invalid = 0; unknown = 0; provider_errors = 0
        for row in preview["items"]:
            value = (observations or {}).get(row["product_id"])
            if not isinstance(value, dict) or str(value.get("availability", "")).upper() not in AVAILABILITY:
                invalid += 1; continue
            availability = str(value.get("availability")).upper()
            if availability == "UNKNOWN": unknown += 1
            if availability == "SOURCE_ERROR": provider_errors += 1
            normalized[row["product_id"]] = value
        if invalid:
            raise ValueError("Provider pilot response could not be normalized; no G2 approval was recorded")
        # The provider pilot validates quota and response normalization only.
        # It deliberately does not create source snapshots or mark catalog rows
        # checked; the subsequent full audit must still cover the whole due set.
        source_run_failed = invalid > 0
        pilot_status = "PASS" if not source_run_failed and unknown == 0 and provider_errors == 0 else "REVIEW_REQUIRED"
        checkpoint = dict(run.get("checkpoint", {}).get("source_audit", {}))
        checkpoint["provider_pilot"] = {"status": pilot_status, "checked": len(normalized),
            "target_count": len(preview["items"]), "unknown": unknown, "provider_errors": provider_errors,
            "failed": int(source_run_failed),
            "tokens_consumed": int(getattr(provider, "last_tokens_consumed", 0) or 0),
            "completed_at": datetime.now(timezone.utc).isoformat()}
        profile = self.source_profiles.for_store(store_id)
        if profile and checkpoint["provider_pilot"]["tokens_consumed"]:
            self.source_profiles.record_usage(profile["profile_id"], checkpoint["provider_pilot"]["tokens_consumed"])
        with connect(self.db) as con:
            con.execute("UPDATE production_runs SET checkpoint_json=?,updated_at=? WHERE run_id=?",
                        (_json({**run.get("checkpoint", {}), "source_audit": checkpoint}), datetime.now(timezone.utc).isoformat(), run_id))
        evidence = {"status": "WAITING_FOR_CONFIRMATION" if pilot_status == "PASS" else "WAITING_FOR_INPUT",
                    "verified": False, "provider_pilot": checkpoint["provider_pilot"],
                    "target_count": checkpoint["provider_pilot"]["target_count"],
                    "pilot_does_not_verify_full_catalog": True,
                    "fingerprint_input": {"provider_pilot": checkpoint["provider_pilot"]}}
        self.service.update(run_id, {"SOURCE_SAFETY": evidence})
        return {"status": pilot_status, "provider_pilot": checkpoint["provider_pilot"],
                "production_run": self.service.get(run_id)}

    def source_audit_control(self, run_id, action):
        action = str(action).upper()
        if action not in {"PAUSE", "RESUME", "STOP", "RETRY_FAILED"}:
            raise ValueError("unsupported source audit control")
        run = self.service.get(run_id)
        checkpoint = dict(run.get("checkpoint", {}).get("source_audit", {}))
        if not checkpoint.get("approved_at"):
            raise PermissionError("Source audit requires prior explicit approval")
        if action == "STOP": checkpoint["control"] = "STOPPED"
        elif action == "PAUSE": checkpoint["control"] = "PAUSED"
        else:
            if checkpoint.get("control") == "STOPPED": raise ValueError("Stopped audit cannot be resumed")
            checkpoint["control"] = "RUNNING"
        with connect(self.db) as con:
            con.execute("UPDATE production_runs SET checkpoint_json=?,updated_at=? WHERE run_id=?",
                        (_json({**run.get("checkpoint", {}), "source_audit": checkpoint}),
                         datetime.now(timezone.utc).isoformat(), run_id))
        if action in {"RESUME", "RETRY_FAILED"}:
            return self.confirm_source_audit(run_id, confirmed=False)
        return {"run_id": run_id, "status": checkpoint["control"], "checkpoint": checkpoint}

    def review_media_rights(self, run_id, selections, policy, *, confirmed=False, notes=None):
        """Persist rights only for products explicitly selected by the user."""
        from .production import ALLOWED_PRODUCT_MEDIA, ProductionGoldenPathService
        policy = str(policy or "").upper()
        if policy not in (ALLOWED_PRODUCT_MEDIA | {"MANUAL_REVIEW_REQUIRED", "NO_RIGHTS_CONFIRMED"}):
            raise ValueError("unsupported product media rights policy")
        if not confirmed: raise PermissionError("Explicit confirmation is required for selected media rights")
        run = self.service.get(run_id); selected = sorted({int(value) for value in selections})
        if not selected: raise ValueError("Select at least one product")
        with connect(self.db) as con:
            placeholders = ",".join("?" for _ in selected)
            product_rows = con.execute(f"SELECT id,images_json FROM products WHERE id IN ({placeholders})", selected).fetchall()
        found_ids = {row["id"] for row in product_rows}
        if found_ids != set(selected): raise ValueError("One or more selected products are not in the local catalog")
        if policy in ALLOWED_PRODUCT_MEDIA:
            for row in product_rows:
                try: images = json.loads(row["images_json"] or "[]")
                except (json.JSONDecodeError, TypeError): images = []
                if not images: raise ValueError("Rights cannot be confirmed for a product with no exact product image")
        service = ProductionGoldenPathService(db=self.db)
        for product_id in selected:
            service.set_media_rights(run["store_id"], product_id, policy, reviewed=True,
                                     notes=(notes or {}).get(product_id, ""))
        self.run(run["store_id"], run_id=run_id)
        return {"selected_count": len(selected), "policy": policy, "production_run": self.service.get(run_id)}

    def save_pricing_policy(self, run_id, policy, *, confirmed=False):
        if not confirmed: raise PermissionError("Explicit pricing-policy confirmation is required")
        required = ("currency", "source_cost_buffer_fixed", "source_cost_buffer_percent", "min_margin_amount",
                    "min_margin_percent", "unknown_fee_handling", "warning_source_price_change_percent")
        missing = [key for key in required if policy.get(key) in (None, "")]
        if missing: raise ValueError("Missing pricing-policy inputs: " + ", ".join(missing))
        normalized = dict(policy); normalized["enabled"] = True
        normalized["auto_reprice_enabled"] = bool(normalized.get("auto_reprice_enabled", False))
        if normalized["auto_reprice_enabled"]: raise ValueError("Automatic repricing remains disabled in this workflow")
        from .source_safety import SourceSafetyService
        store_id = self.service.get(run_id)["store_id"]
        SourceSafetyService(self.db).save_settings(store_id, price_policy=normalized)
        candidate_ids = [row.get("product_id") for row in self._catalog_rows(store_id)]
        SourceSafetyService(self.db).evaluate_many(store_id, candidate_ids)
        self.run(store_id, run_id=run_id)
        return self.service.get(run_id)

    def save_business_inputs(self, store_id, values, *, confirmed=False):
        """Store only operator-supplied legal/business facts locally."""
        if not confirmed: raise PermissionError("Explicit confirmation is required to save business inputs")
        allowed = {"support_email", "legal_name", "business_address", "phone", "return_window",
                   "return_address", "processing_time", "shipping_time", "shipping_fee", "governing_law"}
        unknown = set(values) - allowed
        if unknown: raise ValueError("Unsupported business input field(s): " + ", ".join(sorted(unknown)))
        payload = {key: str(value).strip()[:500] for key, value in values.items() if str(value or "").strip()}
        now = datetime.now(timezone.utc).isoformat()
        with connect(self.db) as con:
            con.execute("""INSERT INTO production_business_inputs(store_id,payload_json,updated_at) VALUES(?,?,?)
                ON CONFLICT(store_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (str(store_id), _json(payload), now))
        return {"store_id": str(store_id), "saved_fields": sorted(payload), "updated_at": now,
                "local_only": True, "remote_write_performed": False}

    def save_manual_evidence(self, run_id, gate_key, evidence_kind, payload, *, confirmed=False, fingerprint=None):
        """Persist human evidence separately from Shopify/API-collected evidence."""
        if not confirmed: raise PermissionError("Explicit human confirmation is required")
        if gate_key not in EVIDENCE_GATES: raise ValueError("manual evidence must target a pre-pilot gate")
        now = datetime.now(timezone.utc).isoformat(); fingerprint = fingerprint or self.current_fingerprint(gate_key, run_id)
        with connect(self.db) as con:
            con.execute("""INSERT INTO production_manual_evidence(run_id,gate_key,evidence_kind,fingerprint,payload_json,confirmed_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,gate_key,evidence_kind) DO UPDATE SET fingerprint=excluded.fingerprint,
                payload_json=excluded.payload_json,confirmed_at=excluded.confirmed_at""",
                (run_id, gate_key, evidence_kind, fingerprint, _json(payload), now))
        self.run(self.service.get(run_id)["store_id"], run_id=run_id)
        return {"confirmed_at": now, "fingerprint": fingerprint, "production_run": self.service.get(run_id)}

    def current_fingerprint(self, gate_key, run_id):
        gate = next(item for item in self.service.get(run_id)["gates"] if item["gate_key"] == gate_key)
        return str((gate.get("evidence") or {}).get("fingerprint") or "")

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
        if key == "PAGES_POLICIES": return self._pages_policies(store_id, run)
        if key == "SEO_ACCESSIBILITY_MOBILE": return self._seo_mobile(store_id, run)
        if key == "COMMERCE_READINESS": return self._commerce(store_id, run)
        return {"status": "REVIEW_REQUIRED", "review_required": ["No evidence collector configured"]}

    def _environment_legacy(self, store_id):
        if store_id != "001":
            return {"status": "BLOCKED", "blockers": ["Expected Store 001 | Cabin Tidy"]}
        local = self._local_environment()
        try:
            from .shopify_collections import get_connection, get_shopify_token
            connection = get_connection(store_id, db=self.db)
            token, _source = get_shopify_token(store_id,db=self.db)
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
                    "missing_inputs": [],
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

    def _environment(self, store_id):
        if store_id != "001":
            return {"status":"BLOCKED","blockers":["Expected Store 001 | Cabin Tidy"]}
        local=self._local_environment()
        try:
            from .shopify_collections import ShopifyReadOnlyVerificationService
            evidence=ShopifyReadOnlyVerificationService(db=self.db).verify(store_id)
            result={**local,**evidence,"verified":evidence.get("status")=="VERIFIED",
                    "missing_required_scopes":evidence.get("missing_read_scopes",[]),
                    "missing_inputs":[],"review_required":[],"secret_values_exposed":False,
                    "fingerprint_input":{"shop_id":evidence.get("shop_id"),
                        "configured_domain":evidence.get("shop_domain"),
                        "primary_domain":evidence.get("primary_domain_host"),
                        "myshopify_domain":evidence.get("actual_shop_domain"),
                        "theme":evidence.get("theme_name"),"scopes":evidence.get("granted_scopes",[]),
                        "publications":evidence.get("publications",[])}}
            if evidence.get("status")=="WAITING_FOR_CREDENTIALS":
                result["missing_inputs"]=["Cabin Tidy Shopify 연결 정보와 자격 증명을 확인하세요."]
            elif evidence.get("status")=="WAITING_FOR_INPUT":
                if evidence.get("app_binding_status")=="NOT_BOUND":
                    result["missing_inputs"]=["Target Production App이 아직 Store에 연결되지 않았습니다. app.apiKey와 Shop ID를 검증해 Production App 프로필을 연결하세요."]
                else:
                    result["missing_inputs"]=["Shopify 앱에 필수 읽기 권한(read_themes)을 부여하고 다시 확인하세요."]
            elif evidence.get("status")=="EXTERNAL_ORG_OAUTH_REQUIRED":
                result["status"]="WAITING_FOR_INPUT"
                result["missing_inputs"]=["이 Shopify 스토어는 Client Credentials 설치에 허용되지 않았습니다. 별도의 OAuth 연결 방식이 필요합니다."]
            elif evidence.get("status")=="BLOCKED":
                result["blockers"]=["Shopify에서 확인한 스토어 도메인이 저장된 도메인과 일치하지 않습니다."]
            elif evidence.get("status")=="APP_IDENTITY_MISMATCH":
                result["status"]="BLOCKED"
                result["blockers"]=[f"Shopify 앱 신원이 다릅니다. 인증된 앱: {evidence.get('authenticated_app_title') or '확인된 이름 없음'} ({evidence.get('authenticated_app_id') or 'GID 미확인'}); Production App을 연결하세요."]
                result["authenticated_app_title"]=evidence.get("authenticated_app_title")
                result["authenticated_app_gid"]=evidence.get("authenticated_app_id")
            elif evidence.get("status")=="STORE_IDENTITY_MISMATCH":
                result["status"]="BLOCKED"
                result["blockers"]=["인증된 Shopify 스토어 ID가 이 연결에 저장된 Shop ID와 다릅니다."]
            elif evidence.get("status")=="REVIEW_REQUIRED":
                result["review_required"]=["Shopify MAIN theme 읽기 결과를 확인할 수 없습니다."]
            return result
        except (TimeoutError,ConnectionError) as exc:
            return {**local,"status":"FAILED_TRANSIENT","review_required":[type(exc).__name__],"secret_values_exposed":False}
        except Exception as exc:
            from .security import redact_text
            from .shopify_auth import ShopifyAuthError
            if isinstance(exc,ShopifyAuthError):
                if exc.code in {"APP_IDENTITY_MISMATCH","STORE_IDENTITY_MISMATCH"}:
                    return {**local,"status":"BLOCKED","blockers":[str(exc)],
                            "identity_details":getattr(exc,"details",{}),"secret_values_exposed":False}
                if exc.code=="SHOP_NOT_PERMITTED":
                    return {**local,"status":"EXTERNAL_ORG_OAUTH_REQUIRED","missing_inputs":[str(exc)],"secret_values_exposed":False}
                if exc.code in {"MISSING_CREDENTIALS","BAD_CREDENTIAL"}:
                    return {**local,"status":"WAITING_FOR_CREDENTIALS","missing_inputs":[str(exc)],"secret_values_exposed":False}
                if exc.code=="NETWORK_ERROR":
                    return {**local,"status":"FAILED_TRANSIENT","review_required":[str(exc)],"secret_values_exposed":False}
            text=redact_text(str(exc)); lowered=text.casefold()
            state="WAITING_FOR_CREDENTIALS" if "credential" in lowered or "token" in lowered else "REVIEW_REQUIRED"
            field="missing_inputs" if state=="WAITING_FOR_CREDENTIALS" else "review_required"
            return {**local,"status":state,field:[text[:240]],"secret_values_exposed":False}

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
        from .source_safety import FreeSourceSafetyService, SourceMonitorService, SourceSafetyService
        if self.source_provider is None:
            free = FreeSourceSafetyService(self.db)
            audit = free.evidence_audit(store_id)
            preview = free.preview_batch(store_id, "DRAFT_PILOT")
            latest_batch = None
            with connect(self.db) as con:
                latest_batch = con.execute("""SELECT * FROM source_safety_release_batches
                    WHERE store_id=? ORDER BY created_at DESC LIMIT 1""", (str(store_id),)).fetchone()
                freshness = {r["freshness_status"]: r["n"] for r in con.execute(
                    "SELECT freshness_status,COUNT(*) n FROM source_monitoring_state GROUP BY freshness_status")}
            if latest_batch and latest_batch["status"] == "VERIFIED":
                batch = dict(latest_batch)
                with connect(self.db) as con:
                    batch_items = [dict(row) for row in con.execute("""SELECT i.*,s.last_checked_at,
                        s.latest_availability,s.latest_source_price FROM source_safety_release_items i
                        LEFT JOIN source_monitoring_state s ON s.product_id=i.product_id
                        WHERE i.batch_id=? ORDER BY i.product_id""", (batch["batch_id"],))]
                freshness_policy = SourceSafetyService(self.db).settings(store_id)["freshness"]
                checked_at = datetime.now(timezone.utc)
                fresh_items = 0
                for item in batch_items:
                    try:
                        observed = datetime.fromisoformat(str(item.get("observed_at")).replace("Z", "+00:00"))
                        age = checked_at - observed
                        if age.total_seconds() < 0 or age.total_seconds() > float(freshness_policy["pre_list_max_age_minutes"]) * 60:
                            age_state = "STALE_BLOCKED"
                        elif age.total_seconds() > float(freshness_policy["stale_warning_hours"]) * 3600:
                            age_state = "STALE_WARNING"
                        else:
                            age_state = "FRESH"
                    except (TypeError, ValueError, OverflowError):
                        age_state = "NEVER_VERIFIED"
                    current = (item.get("last_checked_at") == item.get("observed_at") and
                               item.get("latest_availability") == item.get("availability") and
                               item.get("latest_source_price") == item.get("source_price"))
                    item["freshness"] = age_state
                    item["evidence_current"] = bool(current)
                    fresh_items += int(age_state == "FRESH" and current and
                        item.get("availability") == "IN_STOCK" and item.get("price_status") == "OBSERVED")
                batch_ready = bool(batch_items) and fresh_items == int(batch["target_count"])
                if batch_ready:
                    return {"status": "VERIFIED", "verified": True, "provider": "FREE_LOCAL_SOURCE_CHECK",
                        "batch_scoped": True, "batch_kind": batch["batch_kind"], "target_count": batch["target_count"],
                        "counts": {"checked": batch["checked_count"]}, "spark_evidence_audit": audit,
                        "master_total": preview["master_total"], "eligible_upload_candidates": preview["eligible_upload_candidates"],
                        "fingerprint_input": {"batch_id": batch["batch_id"], "updated_at": batch["updated_at"],
                                               "evidence": batch["evidence_json"], "items": batch_items}}
                return {"status":"WAITING_FOR_INPUT","verified":False,"provider":"FREE_LOCAL_SOURCE_CHECK",
                    "batch_scoped":True,"batch_kind":batch["batch_kind"],"target_count":batch["target_count"],
                    "fresh_verified_count":fresh_items,"missing_inputs":["Source evidence expired or changed after verification. Recheck this release batch."],
                    "fingerprint_input":{"batch_id":batch["batch_id"],"items":batch_items}}
            local = free.inspect_local_batch(store_id, "DRAFT_PILOT")
            return {"status": "WAITING_FOR_INPUT", "verified": False, "provider": "FREE_LOCAL_SOURCE_CHECK",
                    "provider_mode": "FREE_SPARK_PLUS_LOCAL_BROWSER", "keepa_optional": True,
                    "master_total": preview["master_total"], "eligible_upload_candidates": preview["eligible_upload_candidates"],
                    "current_source_safety_target": preview["target_count"], "target_count": preview["target_count"],
                    "draft_pilot_preview": local, "spark_evidence_audit": audit, "freshness": freshness,
                    "missing_inputs": ["Fresh browser source observations are required; imported Spark presence is not stock evidence."],
                    "provider_errors_are_not_oos": True,
                    "fingerprint_input": {"audit": audit, "target_ids": preview["target_product_ids"], "freshness": freshness}}
        preview = self.source_preview(store_id) if self.source_preview else SourceMonitorService(self.db).preview_due_checks(store_id, limit=50000)
        with connect(self.db) as con:
            freshness = {r["freshness_status"]: r["n"] for r in con.execute("SELECT freshness_status,COUNT(*) n FROM source_monitoring_state GROUP BY freshness_status")}
            source_errors = con.execute("SELECT COUNT(*) FROM source_monitoring_state WHERE latest_availability='SOURCE_ERROR'").fetchone()[0]
            latest_run = con.execute("SELECT run_id,checked_count,failed_count,token_estimate FROM source_check_runs WHERE store_id=? ORDER BY started_at DESC LIMIT 1", (store_id,)).fetchone()
        count = len(preview.get("items", []))
        if source_errors:
            return {"status": "WAITING_FOR_INPUT", "verified": False, "target_count": count, "source_errors": source_errors,
                    "counts": {"checked": latest_run["checked_count"], "failed": latest_run["failed_count"]} if latest_run else {},
                    "provider_errors_are_not_oos": True,
                    "missing_inputs": ["Some source provider checks failed; retry those items before completing G2."],
                    "preview_only": True, "fingerprint_input": {"freshness": freshness, "source_errors": source_errors}}
        profile = self.source_profiles.for_store(str(store_id)) or self.source_profiles.register_env_compatibility(str(store_id))
        provider_preflight = self.source_profiles.preflight(str(store_id), target_count=count, batch_size=100)
        if count:
            return {"status": "WAITING_FOR_CONFIRMATION", "missing_inputs": ["Explicit approval required for live source checks"],
                    "target_count": count, "estimated_batches": preview.get("estimated_batches", 0),
                    "estimated_tokens": preview.get("estimated_tokens"), "estimated_cost_usd": preview.get("estimated_cost_usd"),
                    "cost_estimate_note": preview.get("cost_estimate_note"), "fresh": freshness.get("FRESH", 0),
                    "stale": freshness.get("STALE_WARNING", 0) + freshness.get("STALE_BLOCKED", 0),
                    "never_verified": freshness.get("NEVER_VERIFIED", 0), "preview_only": True,
                    "provider_preflight": provider_preflight,
                    "status": "WAITING_FOR_INPUT" if provider_preflight["status"] != "READY" else "WAITING_FOR_CONFIRMATION",
                    "missing_inputs": (["Select a source provider, save its credential and pass the provider health check."]
                                        if provider_preflight["status"] != "READY" else ["Provider pilot and explicit approval are required before full source checks."]),
                    "fingerprint_input": {"target_count": count, "freshness": freshness, "provider_status": provider_preflight["status"]}}
        return {"status": "VERIFIED", "verified": True, "target_count": 0,
                "counts": {"checked": latest_run["checked_count"], "failed": latest_run["failed_count"]} if latest_run else {"checked": 0, "failed": 0},
                "provider_errors_are_not_oos": True, "preview_only": True,
                "fingerprint_input": {"freshness": freshness, "source_errors": source_errors,
                                      "latest_source_check_run": dict(latest_run) if latest_run else None}}

    def free_source_safety_preflight(self, run_id, batch_kind="DRAFT_PILOT", *, selected_product_ids=None):
        from .source_safety import FreeSourceSafetyService
        run = self.service.get(run_id)
        return FreeSourceSafetyService(self.db).preview_batch(
            str(run["store_id"]), batch_kind, selected_product_ids=selected_product_ids)

    def run_free_source_safety_check(self, run_id, batch_kind="DRAFT_PILOT", *,
                                     selected_product_ids=None, observations=None, confirmed=False, batch_id=None):
        """Persist an explicitly approved free capture batch; never calls a provider.

        Without browser observations this is only a local evidence review/queue
        preparation and cannot pass G2. An external observation must be supplied
        by the existing user-operated Browser Capture path.
        """
        if not confirmed:
            raise PermissionError("Explicit confirmation is required to run a release-batch source check")
        from .source_safety import FreeSourceSafetyService, SourceMonitorService, SourceSafetyService, free_capture_observation
        run = self.service.get(run_id); store_id = str(run["store_id"])
        safety = FreeSourceSafetyService(self.db)
        preview = safety.preview_batch(store_id, batch_kind, selected_product_ids=selected_product_ids)
        if not preview["target_product_ids"]:
            raise ValueError("No PRIMARY upload candidates are available for this source check")
        batch_id = batch_id or "FREE_SOURCE_" + hashlib.sha256(
            f"{run_id}:{batch_kind}:{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat()
        if observations is None:
            inspected = safety.inspect_local_batch(store_id, batch_kind, selected_product_ids=selected_product_ids)
            with connect(self.db) as con:
                con.execute("INSERT INTO source_safety_release_batches(batch_id,store_id,batch_kind,status,target_count,checked_count,created_at,updated_at,evidence_json) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,evidence_json=excluded.evidence_json",
                    (batch_id,store_id,batch_kind,"WAITING_FOR_INPUT",len(preview["target_product_ids"]),0,now,now,_json({"provider":"FREE_LOCAL_SOURCE_CHECK","manual_capture_required":True})))
                con.executemany("INSERT INTO source_safety_release_items(batch_id,product_id,status,availability,price_status,observed_at,reason) VALUES(?,?,?, ?,?,?,?) ON CONFLICT(batch_id,product_id) DO UPDATE SET status=excluded.status,availability=excluded.availability,price_status=excluded.price_status,observed_at=excluded.observed_at,reason=excluded.reason",
                    [(batch_id,item["product_id"],"WAITING_FOR_INPUT",item["availability"],item["price_status"],None,
                      "Fresh user-operated browser capture required" if item["manual_capture_required"] else "Local evidence is preview only") for item in inspected["items"]])
            self.service.update(run_id, {"SOURCE_SAFETY": {"status":"WAITING_FOR_INPUT","verified":False,
                "provider":"FREE_LOCAL_SOURCE_CHECK","batch_id":batch_id,"batch_kind":batch_kind,
                "counts":{"target":len(inspected["items"]),"checked":0},"manual_capture_required":True,
                "fingerprint_input":{"batch_id":batch_id,"target_ids":preview["target_product_ids"]}}})
            return {"status":"WAITING_FOR_INPUT","batch_id":batch_id,"preview":inspected,
                    "production_run":self.run(store_id,run_id=run_id)}
        normalized = {}
        for product_id in preview["target_product_ids"]:
            value = (observations or {}).get(product_id)
            if not isinstance(value, dict):
                normalized[product_id] = {"availability":"SOURCE_ERROR","availability_confidence":"UNKNOWN",
                    "evidence_kind":"MISSING_BROWSER_CAPTURE","evidence":{"reason":"capture missing"}}
                continue
            if "sourceAvailability" in value or "availabilityEvidence" in value:
                value = free_capture_observation(value, observed_at=value.get("_collectedAt"))
            evidence_text = json.dumps(value.get("evidence") or {}, ensure_ascii=False).casefold()
            if any(term in evidence_text for term in ("captcha", "robot check", "sign in to continue", "automated access")):
                normalized[product_id] = {"availability":"SOURCE_ERROR","availability_confidence":"UNKNOWN",
                    "evidence_kind":"HUMAN_ACTION_REQUIRED","evidence":{"challenge_detected":True}}
            else:
                explicit = (value.get("qualifies_for_sellability") is True and
                    value.get("evidence_kind") in {"JSON_LD_OFFER_AVAILABILITY", "VISIBLE_AVAILABILITY_TEXT"})
                normalized[product_id] = {**value,
                    "availability": str(value.get("availability") or "UNKNOWN").upper() if explicit else "UNKNOWN",
                    "availability_confidence": value.get("availability_confidence") or "UNKNOWN",
                    "evidence_kind": value.get("evidence_kind") if explicit else "NO_EXPLICIT_AVAILABILITY_EVIDENCE",
                    "observed_at":value.get("observed_at") or now}
        status = SourceMonitorService(self.db).run_due_checks(store_id, observations=normalized,
            limit=len(preview["target_product_ids"]), product_ids=preview["target_product_ids"])
        monitor = SourceMonitorService(self.db)
        monitor.safety.evaluate_many(store_id, preview["target_product_ids"])
        rows = []
        verified_count = 0
        freshness_policy = monitor.safety.settings(store_id)["freshness"]
        observed_now = datetime.now(timezone.utc)
        for product_id, observation in normalized.items():
            observed = datetime.fromisoformat(str(observation.get("observed_at") or now).replace("Z", "+00:00"))
            age_seconds = (observed_now - observed).total_seconds()
            freshness = "FRESH" if 0 <= age_seconds <= float(freshness_policy["pre_list_max_age_minutes"]) * 60 else "STALE_BLOCKED"
            avail = str(observation.get("availability") or "UNKNOWN").upper()
            price_ok = isinstance(observation.get("source_price"), (int,float)) and observation["source_price"] > 0
            good = avail == "IN_STOCK" and price_ok and freshness == "FRESH"
            verified_count += int(good)
            reason = ("HUMAN_ACTION_REQUIRED: challenge or source capture error; do not bypass."
                      if avail == "SOURCE_ERROR" else
                      None if good else "Availability, current price, or freshness needs review")
            rows.append((batch_id,product_id,"VERIFIED" if good else "REVIEW_REQUIRED",avail,
                         "OBSERVED" if price_ok else "MISSING_PRICE",observation.get("observed_at") or now,
                         observation.get("source_price") if price_ok else None,
                         reason))
        complete = verified_count == len(preview["target_product_ids"]) and status.get("failed_count",0) == 0
        with connect(self.db) as con:
            con.execute("INSERT INTO source_safety_release_batches(batch_id,store_id,batch_kind,status,target_count,checked_count,created_at,updated_at,evidence_json) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET status=excluded.status,checked_count=excluded.checked_count,updated_at=excluded.updated_at,evidence_json=excluded.evidence_json",
                (batch_id,store_id,batch_kind,"VERIFIED" if complete else "REVIEW_REQUIRED",len(rows),len(rows),now,now,
                 _json({"provider":"FREE_LOCAL_SOURCE_CHECK","verified_items":verified_count,"source_check_run_id":status["run_id"]})))
            con.executemany("INSERT INTO source_safety_release_items(batch_id,product_id,status,availability,price_status,observed_at,source_price,reason) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(batch_id,product_id) DO UPDATE SET status=excluded.status,availability=excluded.availability,price_status=excluded.price_status,observed_at=excluded.observed_at,source_price=excluded.source_price,reason=excluded.reason",rows)
        result = self.run(store_id,run_id=run_id)
        return {"status":"COMPLETE" if complete else "REVIEW_REQUIRED","batch_id":batch_id,
                "checked":len(rows),"verified_items":verified_count,"production_run":result}

    def prepare_free_browser_capture_batch(self, run_id, batch_kind="DRAFT_PILOT", *,
                                           selected_product_ids=None, confirmed=False):
        """Queue explicit ASINs in the existing user-operated Browser Capture flow."""
        if not confirmed:
            raise PermissionError("Explicit confirmation is required to prepare the local browser queue")
        from .capture.batch import BatchSourcingService
        from .source_safety import FreeSourceSafetyService
        run = self.service.get(run_id); store_id = str(run["store_id"])
        free = FreeSourceSafetyService(self.db)
        preview = free.preview_batch(store_id, batch_kind, selected_product_ids=selected_product_ids)
        if not preview["target_product_ids"]:
            raise ValueError("No eligible products are available for this browser source queue")
        prepared = self.run_free_source_safety_check(run_id, batch_kind,
            selected_product_ids=selected_product_ids, confirmed=True)
        release_batch_id = prepared["batch_id"]
        rows_by_id = {}
        ids = preview["target_product_ids"]
        marks = ",".join("?" for _ in ids)
        with connect(self.db) as con:
            rows_by_id = {int(row["id"]): dict(row) for row in con.execute(
                f"SELECT id,asin,title,source_url FROM products WHERE id IN ({marks})", ids)}
        from .db import utc_now
        batch_service = BatchSourcingService(self.db)
        browser_batch = batch_service.create(store_id, "FREE SOURCE SAFETY", len(ids),
            auto_import_master=False, target_mode="PRIMARY")
        capture_run_id = "BC_" + hashlib.sha256(f"{browser_batch['run_id']}:{release_batch_id}".encode()).hexdigest()[:20]
        now = utc_now()
        asins = []
        with connect(self.db) as con:
            con.execute("INSERT INTO browser_capture_runs(run_id,store_id,keyword,search_url,status,captured_at,candidates) VALUES(?,?,?,?,?,?,?)",
                (capture_run_id,store_id,"FREE SOURCE SAFETY","","DETAIL_QUEUED",now,len(ids)))
            for product_id in ids:
                row = rows_by_id[product_id]
                asin = str(row["asin"]).upper(); asins.append(asin)
                search_payload = json.dumps({"asin":asin,"title":row["title"],"url":row["source_url"],"source_safety_only":True},ensure_ascii=False)
                con.execute("INSERT INTO browser_capture_candidates(run_id,asin,search_payload_json,completeness_score,capture_status,created_at,updated_at) VALUES(?,?,?,0,'NEEDS_DETAIL',?,?)",
                    (capture_run_id,asin,search_payload,now,now))
            con.execute("UPDATE source_safety_release_batches SET evidence_json=? WHERE batch_id=?",
                (_json({"provider":"FREE_LOCAL_SOURCE_CHECK","browser_batch_run_id":browser_batch["run_id"],
                        "capture_run_id":capture_run_id,"master_ids":ids,"manual_browser_action_required":True}),release_batch_id))
        queued = batch_service.queue_existing_candidates(browser_batch["run_id"], asins)
        return {"status":"WAITING_FOR_INPUT","provider":"FREE_LOCAL_SOURCE_CHECK",
            "release_batch_id":release_batch_id,"browser_batch_run_id":browser_batch["run_id"],
            "capture_run_id":capture_run_id,"queued":queued.get("queued",0),
            "target_count":len(ids),"auto_import_master":False,
            "note":"Open the queued Amazon pages with the existing Browser Capture extension. No bypass or automatic Amazon execution was performed."}

    def apply_free_browser_capture_results(self, run_id, release_batch_id, *, confirmed=False):
        if not confirmed:
            raise PermissionError("Explicit confirmation is required to apply captured observations")
        from .source_safety import FreeSourceSafetyService
        run = self.service.get(run_id); store_id = str(run["store_id"])
        with connect(self.db) as con:
            batch = con.execute("SELECT * FROM source_safety_release_batches WHERE batch_id=? AND store_id=?",
                                (release_batch_id,store_id)).fetchone()
            if not batch:
                raise KeyError(release_batch_id)
            checkpoint = json.loads(batch["evidence_json"] or "{}")
            targets = con.execute("SELECT i.product_id,p.asin FROM source_safety_release_items i JOIN products p ON p.id=i.product_id WHERE i.batch_id=? ORDER BY i.product_id",
                                  (release_batch_id,)).fetchall()
            capture_run_id = checkpoint.get("capture_run_id")
            captured = con.execute("""SELECT c.asin,c.detail_payload_json FROM browser_capture_candidates c
                JOIN browser_capture_runs r ON r.run_id=c.run_id WHERE r.store_id=? AND c.run_id=? AND c.capture_status='DETAIL_COMPLETE'""",
                (store_id,capture_run_id)).fetchall() if capture_run_id else []
        by_asin = {str(row["asin"]).upper(): FreeSourceSafetyService._decode(row["detail_payload_json"]) for row in captured}
        expected = int(batch["target_count"])
        if len(by_asin) < expected:
            return {"status":"WAITING_FOR_INPUT","batch_id":release_batch_id,
                "captured":len(by_asin),"target_count":expected,
                "missing_capture_count":expected-len(by_asin),
                "human_action_required":True,
                "note":"Wait for each queued Browser Capture detail result. CAPTCHA/challenge items require manual handling; no result is inferred."}
        observations = {}
        for target in targets:
            payload = by_asin.get(str(target["asin"]).upper())
            if payload:
                observations[int(target["product_id"])] = payload
        return self.run_free_source_safety_check(run_id, batch["batch_kind"],
            selected_product_ids=[int(row["product_id"]) for row in targets],
            observations=observations, confirmed=True, batch_id=release_batch_id)

    def free_source_batch_control(self, run_id, action):
        action = str(action).upper()
        transitions = {"PAUSE":("PAUSED", {"WAITING_FOR_INPUT", "RUNNING"}),
                       "RESUME":("WAITING_FOR_INPUT", {"PAUSED"}),
                       "STOP":("STOPPED", {"WAITING_FOR_INPUT", "PAUSED", "RUNNING"}),
                       "RETRY_FAILED":("WAITING_FOR_INPUT", {"REVIEW_REQUIRED", "FAILED"})}
        if action not in transitions:
            raise ValueError("unsupported free source batch control")
        run = self.service.get(run_id)
        with connect(self.db) as con:
            batch = con.execute("SELECT * FROM source_safety_release_batches WHERE store_id=? ORDER BY created_at DESC LIMIT 1",
                                (str(run["store_id"]),)).fetchone()
            if not batch:
                raise ValueError("no persisted free source batch exists")
            new_status, accepted = transitions[action]
            if batch["status"] not in accepted:
                raise ValueError(f"cannot {action.lower()} batch in {batch['status']} state")
            con.execute("UPDATE source_safety_release_batches SET status=?,updated_at=? WHERE batch_id=?",
                        (new_status, datetime.now(timezone.utc).isoformat(), batch["batch_id"]))
            if action == "RETRY_FAILED":
                con.execute("UPDATE source_safety_release_items SET status='WAITING_FOR_INPUT' WHERE batch_id=? AND status='REVIEW_REQUIRED'",
                            (batch["batch_id"],))
        return {"run_id":run_id,"batch_id":batch["batch_id"],"status":new_status,
                "note":"This controls the persisted local capture queue; it does not launch browser automation or a paid provider."}

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
            rights = {r["product_id"]: dict(r) for r in con.execute("SELECT product_id,policy,reviewed_at,notes FROM product_media_rights WHERE store_id=?", (store_id,))}
            product_meta = {r["id"]: dict(r) for r in con.execute("SELECT id,asin,title,images_json FROM products")}
        missing = sum((rights.get(row.get("product_id"), {}).get("policy", row.get("media_policy")) not in {"SUPPLIER_AUTHORIZED", "MERCHANT_OWNED", "LICENSED"}
                       or not rights.get(row.get("product_id"), {}).get("reviewed_at")) for row in rows)
        unresolved = missing or not rows
        def images_for(product_id):
            try:
                value = json.loads(product_meta.get(product_id, {}).get("images_json") or "[]")
                return value if isinstance(value, list) else []
            except (json.JSONDecodeError, TypeError):
                return []
        return {"status": "WAITING_FOR_INPUT" if unresolved else "VERIFIED", "verified": not unresolved,
                "counts": {"total": len(rows), "rights_review_required": missing},
                "missing_inputs": (["Review product-image usage rights in bulk"] if missing else []) + (["No production candidates are available for exact-media review"] if not rows else []),
                "rights_auto_assigned": False, "reviewed_count": len(rows) - missing,
                "review_queue": [{"product_id": row.get("product_id"), "asin": product_meta.get(row.get("product_id"), {}).get("asin", row.get("asin")),
                                  "title": product_meta.get(row.get("product_id"), {}).get("title", row.get("title")),
                                  "policy": rights.get(row.get("product_id"), {}).get("policy", "NO_RIGHTS_CONFIRMED"),
                                  "reviewed_at": rights.get(row.get("product_id"), {}).get("reviewed_at"),
                                  "notes": rights.get(row.get("product_id"), {}).get("notes", ""),
                                  "image_count": len(images_for(row.get("product_id"))),
                                  "primary_image": (images_for(row.get("product_id")) or [None])[0]}
                                 for row in rows],
                "fingerprint_input": rights}

    def _pricing(self, store_id):
        from .source_safety import SourceSafetyService
        policy = SourceSafetyService(self.db).settings(store_id)["price_policy"]
        ready = bool(policy.get("enabled") and policy.get("currency") and policy.get("min_margin_amount") is not None
                     and policy.get("min_margin_percent") is not None and policy.get("unknown_fee_handling")
                     and policy.get("warning_source_price_change_percent") is not None
                     and not policy.get("auto_reprice_enabled"))
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

    def _shopify_read_client(self, store_id):
        from .shopify_collections import ShopifyGraphQLClient, get_connection, get_shopify_token
        connection = get_connection(store_id, db=self.db)
        token, _ = get_shopify_token(store_id,db=self.db)
        if not connection or not token:
            return None, connection, set(), "WAITING_FOR_CREDENTIALS"
        client = ShopifyGraphQLClient(connection["shop_domain"], token, connection["api_version"])
        try:
            data = client.execute("query ShopSourceGrantedScopes { currentAppInstallation { accessScopes { handle } } }")
            scopes = {row.get("handle") for row in ((data.get("currentAppInstallation") or {}).get("accessScopes") or [])}
            return client, connection, scopes, "VERIFIED"
        except Exception:
            # A scope list saved locally is not treated as proof of the grant.
            return client, connection, set(), "SCOPE_OR_API_UNAVAILABLE"

    def _pages_policies(self, store_id, run):
        adapter = self.collectors.get("remote_pages")
        if adapter:
            remote = dict(adapter(store_id) or {})
        else:
            client, connection, scopes, connection_state = self._shopify_read_client(store_id)
            if not client:
                return {"status": connection_state, "items": {}, "api_evidence": {}, "manual_evidence": {},
                        "missing_inputs": ["Shopify read-only credentials are required"], "fingerprint_input": {"connection": connection}}
            pages, policies = [], []
            page_state = policy_state = "API_NOT_EXPOSED"
            try:
                data = client.execute("query ShopSourcePages { pages(first: 100) { nodes { id title handle body publishedAt } } }")
                pages = (data.get("pages") or {}).get("nodes") or []
                page_state = "READ"
            except Exception as exc:
                page_state = "SCOPE_MISSING" if "access denied" in str(exc).casefold() or "scope" in str(exc).casefold() else "API_NOT_EXPOSED"
            if "read_legal_policies" in scopes:
                try:
                    data = client.execute("query ShopSourcePolicies { shop { shopPolicies { title body type url } } }")
                    policies = ((data.get("shop") or {}).get("shopPolicies") or [])
                    policy_state = "READ"
                except Exception as exc:
                    policy_state = "SCOPE_MISSING" if "access denied" in str(exc).casefold() else "API_NOT_EXPOSED"
            elif scopes:
                policy_state = "SCOPE_MISSING"
            remote = {"pages": pages, "policies": policies, "page_api_status": page_state,
                      "policy_api_status": policy_state, "shop_domain": (connection or {}).get("shop_domain"),
                      "api_version": (connection or {}).get("api_version"), "scopes": sorted(scopes)}
        targets = {"Contact": ("contact",), "About": ("about",), "Shipping": ("shipping",),
                   "Returns/Refund": ("return", "refund"), "Privacy": ("privacy",), "Terms": ("terms",)}
        all_remote = (remote.get("pages") or []) + (remote.get("policies") or [])
        with connect(self.db) as con:
            manual = con.execute("SELECT payload_json,confirmed_at FROM production_manual_evidence WHERE run_id=? AND gate_key='PAGES_POLICIES' AND evidence_kind='PAGES_POLICIES_SIGNOFF'",
                                 (run["run_id"],)).fetchone()
        manual_payload = json.loads(manual["payload_json"]) if manual else {}
        items = {}
        for label, needles in targets.items():
            found = [item for item in all_remote if any(
                needle in (str(item.get("title", "")) + " " + str(item.get("handle", "")) + " " + str(item.get("type", ""))).casefold()
                for needle in needles)]
            relevant_read = (remote.get("page_api_status") == "READ" if label in {"Contact", "About", "Shipping"} else
                             remote.get("policy_api_status") == "READ" if label in {"Privacy", "Terms"} else
                             "READ" in {remote.get("page_api_status"), remote.get("policy_api_status")})
            relevant_states = ({remote.get("page_api_status")} if label in {"Contact", "About", "Shipping"} else
                               {remote.get("policy_api_status")} if label in {"Privacy", "Terms"} else
                               {remote.get("page_api_status"), remote.get("policy_api_status")})
            if found:
                state = "PRESENT_VERIFIED" if manual_payload.get(label) is True else "PRESENT_NEEDS_REVIEW"
            elif manual_payload.get(label) is True:
                state = "PRESENT_VERIFIED"  # explicitly verified by the operator when API read is unavailable
            elif relevant_read:
                state = "MISSING"
            else:
                state = "SCOPE_MISSING" if "SCOPE_MISSING" in relevant_states else "API_NOT_EXPOSED"
            items[label] = {"status": state, "count": len(found), "content_review_required": bool(found)}
        # Content presence never certifies legal/business correctness.
        api_unavailable = all(remote.get(field) in {"API_NOT_EXPOSED", "SCOPE_MISSING"}
                              for field in ("page_api_status", "policy_api_status"))
        manually_verified = all(manual_payload.get(label) is True for label in targets) and all(
            item["status"] == "PRESENT_VERIFIED" for item in items.values())
        return {"status": "VERIFIED" if manually_verified else "WAITING_FOR_INPUT",
                "verified": manually_verified, "items": items,
                "remote_status": {"pages": remote.get("page_api_status", "READ"),
                                  "policies": remote.get("policy_api_status", "API_NOT_EXPOSED")},
                "manual_verification_required": True, "manual_evidence": {"confirmed": manually_verified,
                "confirmed_at": manual["confirmed_at"] if manual else None}, "api_unavailable": api_unavailable,
                "business_facts_invented": False,
                "missing_inputs": [] if manually_verified else ["Review each page/policy and supply missing business facts locally; remote write is disabled."],
                "fingerprint_input": remote}

    def _seo_mobile(self, store_id, run):
        adapter = self.collectors.get("remote_seo_mobile")
        remote = dict(adapter(store_id) or {}) if adapter else {}
        try: theme = self._theme_snapshot(store_id)
        except Exception: theme = {"status": "UNAVAILABLE"}
        rows = self._catalog_rows(store_id)
        issues = {"unverified_product_seo": sum("CONTENT_SEO_READY_UNVERIFIED" in row.get("reasons", []) for row in rows),
                  "duplicate_handle": 0,
                  "missing_alt": 0}
        with connect(self.db) as con:
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shopify_product_mappings'").fetchone()
            if exists:
                duplicate_handles = con.execute("""SELECT COUNT(*) FROM (SELECT lower(shopify_handle) h FROM shopify_product_mappings
                    WHERE store_id=? AND shopify_handle IS NOT NULL GROUP BY lower(shopify_handle) HAVING COUNT(*)>1)""", (store_id,)).fetchone()
                issues["duplicate_handle"] = int(duplicate_handles[0])
        theme_fingerprint = _hash({"theme": theme.get("theme"), "template": theme.get("template"), "files": theme.get("theme_files")})
        with connect(self.db) as con:
            signoff = con.execute("SELECT payload_json,confirmed_at,fingerprint FROM production_manual_evidence WHERE run_id=? AND gate_key='SEO_ACCESSIBILITY_MOBILE' AND evidence_kind='VISUAL_SIGNOFF'",
                                  (run["run_id"],)).fetchone()
        payload = json.loads(signoff["payload_json"]) if signoff else {}
        checklist = ("desktop", "mobile", "hero_crop", "category_cards", "menu", "footer", "readability")
        signed = bool(signoff and signoff["fingerprint"] == theme_fingerprint and all(payload.get(k) is True for k in checklist))
        critical = any(issues.values()) or bool(remote.get("critical_findings"))
        ready = signed and not critical
        return {"status": "VERIFIED" if ready else "WAITING_FOR_INPUT", "verified": ready,
                "local_checks": issues, "remote_checks": remote, "human_visual_signed_off": signed,
                "visual_signoff_fingerprint": theme_fingerprint,
                "missing_inputs": [] if ready else (["Resolve SEO/accessibility critical findings"] if critical else ["Desktop/mobile visual sign-off is required for the current theme."]),
                "wcag_automatically_certified": False, "wcag_claim": "NOT_ASSESSED_FULL_MANUAL_AUDIT_REQUIRED",
                "fingerprint_input": {"theme_fingerprint": theme_fingerprint, "local": issues, "remote": remote}}

    def _commerce(self, store_id, run):
        adapter = self.collectors.get("remote_commerce")
        if adapter:
            remote = dict(adapter(store_id) or {})
        else:
            client, connection, scopes, connection_state = self._shopify_read_client(store_id)
            remote = {"connection_state": connection_state, "shop_domain": (connection or {}).get("shop_domain"),
                      "api_version": (connection or {}).get("api_version"), "api_evidence": {},
                      "unsupported": ["US market status", "shipping configuration", "tax configuration", "payment readiness", "checkout readiness"],
                      "manual_verification_required": ["US market", "shipping", "tax", "payment", "checkout", "store password"]}
            if client:
                try:
                    data = client.execute("query ShopSourceCommerceBasics { shop { name currencyCode primaryDomain { host sslEnabled url } } }")
                    shop = data.get("shop") or {}; domain = shop.get("primaryDomain") or {}
                    remote["api_evidence"] = {"shop_name": shop.get("name"), "currency": shop.get("currencyCode"),
                                              "primary_domain": domain.get("host"), "ssl_enabled": domain.get("sslEnabled"),
                                              "domain_url": domain.get("url")}
                except Exception as exc:
                    remote["api_error_type"] = type(exc).__name__
        manual_items = remote.get("manual_verification_required") or []
        with connect(self.db) as con:
            row = con.execute("SELECT payload_json,confirmed_at FROM production_manual_evidence WHERE run_id=? AND gate_key='COMMERCE_READINESS' AND evidence_kind='COMMERCE_SIGNOFF'",
                              (run["run_id"],)).fetchone()
        payload = json.loads(row["payload_json"]) if row else {}
        confirmed = {key for key, value in payload.items() if value is True}
        unresolved = [item for item in manual_items if item not in confirmed]
        supported = remote.get("api_evidence") or {}
        ready = bool(supported) and not unresolved and not remote.get("api_error_type")
        return {"status": "VERIFIED" if ready else "WAITING_FOR_INPUT", "verified": ready,
                "api_evidence": supported, "manual_evidence": {"confirmed": sorted(confirmed), "confirmed_at": row["confirmed_at"] if row else None},
                "unsupported_read": remote.get("unsupported", []), "manual_verification_required": manual_items,
                "unresolved_manual_items": unresolved, "commerce_mutation_performed": False,
                "missing_inputs": [] if ready else ["Verify unsupported commerce settings and save manual confirmations."],
                "fingerprint_input": {"remote": remote, "manual": payload}}

    def _read_adapter(self, name, store_id):
        adapter = self.collectors.get("remote_" + name)
        evidence = dict(adapter(store_id) or {}) if adapter else {"status": "WAITING_FOR_INPUT"}
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
        try:
            return ShopifyThemeReader(db=self.db).discover(store_id)
        except Exception as exc:
            return {"status": "NOT_CONNECTED", "warning": f"Theme read-only evidence unavailable ({type(exc).__name__})"}

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
