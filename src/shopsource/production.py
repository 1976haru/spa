"""Strict, local-first production launch gates for the Cabin Tidy golden path.

This module records evidence and blockers; it never calls a provider or performs
Shopify/theme writes. Existing phase services remain the owners of their work.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .db import connect, init_db
from .paths import EXPORT_DIR
from .security import redact_value

GATES = (
    "ENVIRONMENT_STORE_IDENTITY", "SOURCING_QUALITY", "SOURCE_SAFETY",
    "PRODUCT_CONTENT", "PRODUCT_MEDIA", "PRICING_MARGIN", "COLLECTION_ARCHITECTURE",
    "COLLECTION_CATEGORY_MEDIA", "BRAND_HEADER_NAVIGATION", "HOMEPAGE",
    "PRODUCT_COLLECTION_TEMPLATES", "PAGES_POLICIES", "SEO_ACCESSIBILITY_MOBILE",
    "COMMERCE_READINESS", "CONTROLLED_LIVE_PILOT", "BATCH_EXPANSION",
    "FINAL_LAUNCH_READINESS",
)
GATE_STATES = {"NOT_STARTED", "RUNNING", "READY", "READY_WITH_WARNINGS", "REVIEW_REQUIRED",
               "WAITING_FOR_INPUT", "WAITING_FOR_CONFIRMATION", "BLOCKED", "VERIFIED"}
PRODUCTION_CLASSES = {"PRODUCTION_CANDIDATE", "RESERVE", "REVIEW_REQUIRED", "REJECT_FOR_STORE", "RESTRICTED"}
MEDIA_POLICIES = {"SUPPLIER_AUTHORIZED", "MERCHANT_OWNED", "LICENSED", "GENERATED_LIFESTYLE_ONLY",
                  "MANUAL_REVIEW_REQUIRED", "NO_RIGHTS_CONFIRMED"}
ALLOWED_PRODUCT_MEDIA = {"SUPPLIER_AUTHORIZED", "MERCHANT_OWNED", "LICENSED"}
ROLLOUT = (10, 50, 200)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value):
    return json.dumps(redact_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _install(db):
    init_db(db)
    with connect(db) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS production_runs(
          run_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,status TEXT NOT NULL,gate_index INTEGER NOT NULL DEFAULT 0,
          checkpoint_json TEXT NOT NULL DEFAULT '{}',summary_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_production_runs_store ON production_runs(store_id,updated_at DESC);
        CREATE TABLE IF NOT EXISTS production_gates(
          run_id TEXT NOT NULL,gate_key TEXT NOT NULL,position INTEGER NOT NULL,status TEXT NOT NULL,
          evidence_json TEXT NOT NULL DEFAULT '{}',blockers_json TEXT NOT NULL DEFAULT '[]',updated_at TEXT NOT NULL,
          PRIMARY KEY(run_id,gate_key));
        CREATE TABLE IF NOT EXISTS product_media_rights(
          store_id TEXT NOT NULL,product_id INTEGER NOT NULL,policy TEXT NOT NULL,reviewed_at TEXT,notes TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(store_id,product_id));
        CREATE TABLE IF NOT EXISTS production_rollouts(
          store_id TEXT PRIMARY KEY,batch_index INTEGER NOT NULL DEFAULT 0,last_verified_count INTEGER NOT NULL DEFAULT 0,
          critical_mismatches INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
        """)


def inspect_product(product: dict, *, safety: dict | None = None, media_policy: str | None = None,
                    pricing_policy: dict | None = None) -> dict:
    """Classify one MASTER item without deleting or mutating its source row."""
    p = dict(product or {})
    safety = safety or {}
    title = str(p.get("storefront_title") or p.get("title") or "").strip()
    source_url = p.get("source_url") or p.get("url")
    price = p.get("selling_price")
    restricted = bool(p.get("restricted") or p.get("classification") == "RESTRICTED")
    archived = bool(p.get("archived"))
    reasons = []
    if restricted: return {"classification": "RESTRICTED", "reasons": ["RESTRICTED"], "publishable": False}
    if archived: return {"classification": "REJECT_FOR_STORE", "reasons": ["ARCHIVED"], "publishable": False}
    letters = [c for c in title if c.isalpha()]
    all_caps = len(letters) >= 8 and all(c.isupper() for c in letters)
    if not title or len(title) < 8 or all_caps or re.search(r"\b(Amazon's Choice|Best Seller|#1 Best Seller|free shipping|limited time deal)\b", title, re.I):
        reasons.append("TITLE_QUALITY")
    if not source_url: reasons.append("MISSING_SOURCE_REFERENCE")
    if p.get("duplicate_asin") or p.get("identity_conflict"): reasons.append("DUPLICATE_OR_IDENTITY_CONFLICT")
    if p.get("variant_ambiguous"): reasons.append("VARIANT_REVIEW")
    if int(p.get("variant_count") or 1) > 1 and not p.get("multi_variant_supported", False): reasons.append("UNSUPPORTED_MULTI_VARIANT")
    if p.get("bundle_ambiguous"): reasons.append("BUNDLE_REVIEW")
    if p.get("fitment_risk") and not p.get("fitment_evidence"): reasons.append("FITMENT_EVIDENCE_REQUIRED")
    if not p.get("store_relevant", p.get("category_fit", False)): reasons.append("STORE_FIT_UNVERIFIED")
    if safety.get("availability") != "IN_STOCK": reasons.append("SOURCE_NOT_VERIFIED_IN_STOCK")
    freshness = safety.get("freshness_status")
    if not safety.get("snapshot_fresh") and freshness != "FRESH": reasons.append("SOURCE_NOT_FRESH")
    if safety.get("source_error") or safety.get("sellability_status") == "BLOCKED_SOURCE_ERROR": reasons.append("SOURCE_ERROR")
    if safety.get("sellability_status") != "SELLABLE": reasons.append("SELLABILITY_BLOCKED")
    try: price_ok = float(price) > 0
    except (TypeError, ValueError): price_ok = False
    if not price_ok: reasons.append("MISSING_SELLING_PRICE")
    policy = str(media_policy or p.get("media_policy") or "NO_RIGHTS_CONFIRMED").upper()
    if policy not in MEDIA_POLICIES: policy = "NO_RIGHTS_CONFIRMED"
    if policy not in ALLOWED_PRODUCT_MEDIA: reasons.append("MEDIA_RIGHTS_UNCONFIRMED")
    if not p.get("primary_image") and not p.get("images"): reasons.append("MISSING_EXACT_PRODUCT_MEDIA")
    if pricing_policy is None or not pricing_policy.get("enabled"):
        reasons.append("PRICING_POLICY_MISSING")
    elif safety.get("margin_status") not in {"SAFE", "PASS"}:
        reasons.append("MARGIN_NOT_VERIFIED")
    if any(x in reasons for x in ("STORE_FIT_UNVERIFIED", "SOURCE_NOT_VERIFIED_IN_STOCK", "SOURCE_NOT_FRESH", "SOURCE_ERROR", "SELLABILITY_BLOCKED", "MISSING_SELLING_PRICE", "PRICING_POLICY_MISSING", "MARGIN_NOT_VERIFIED", "MEDIA_RIGHTS_UNCONFIRMED")):
        classification = "REVIEW_REQUIRED"
    elif reasons:
        classification = "RESERVE" if reasons == ["TITLE_QUALITY"] else "REVIEW_REQUIRED"
    else:
        classification = "PRODUCTION_CANDIDATE"
    evidence_fields = ("description_supported", "features_supported", "variant_normalized", "seo_ready", "handle_stable")
    quality_score = round(sum(bool(p.get(k)) for k in evidence_fields) * 100 / len(evidence_fields))
    if any(not p.get(k) for k in evidence_fields):
        if classification == "PRODUCTION_CANDIDATE": classification = "REVIEW_REQUIRED"
        reasons.extend(f"CONTENT_{k.upper()}_UNVERIFIED" for k in evidence_fields if not p.get(k))
    return {"classification": classification, "reasons": reasons, "quality_score": quality_score, "media_policy": policy,
            "publishable": classification == "PRODUCTION_CANDIDATE"}


def no_placeholder_audit(snapshot: dict) -> list[dict]:
    """Return deterministic critical findings for managed production content."""
    findings = []
    def add(entity, code, value=""):
        findings.append({"severity": "CRITICAL", "entity": str(entity), "code": code, "reason": str(value)[:240]})
    for i, link in enumerate(snapshot.get("links", []) or []):
        target = str(link.get("target") or link.get("url") or "").strip()
        if target in {"", "#"}: add(link.get("label", f"link:{i}"), "PLACEHOLDER_LINK", target or "blank")
        if link.get("wrong_target") or link.get("duplicate_wrong_target"): add(link.get("label", i), "WRONG_COLLECTION_TARGET")
    for i, shortcut in enumerate(snapshot.get("category_shortcuts", []) or []):
        target = str(shortcut.get("target") or "").strip()
        if not target or target == "#": add(shortcut.get("label", f"shortcut:{i}"), "PLACEHOLDER_LINK", target or "blank")
        if not shortcut.get("remote_collection_exists"): add(shortcut.get("label", i), "SHORTCUT_TARGET_MISSING")
        if shortcut.get("wrong_target") or shortcut.get("duplicate_wrong_target"): add(shortcut.get("label", i), "WRONG_COLLECTION_TARGET")
    def strings(value):
        if isinstance(value, str):
            yield value.casefold()
        elif isinstance(value, dict):
            for key, item in value.items():
                yield from strings(key)
                yield from strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value: yield from strings(item)
    phrases = ("todo", "tbd", "lorem ipsum", "example.com", "555-555", "your address")
    found_phrases = set()
    for text in strings(snapshot):
        found_phrases.update(phrase for phrase in phrases if phrase in text)
    for phrase in sorted(found_phrases): add("managed_content", "PLACEHOLDER_TEXT", phrase)
    for product in snapshot.get("products", []) or []:
        identity = product.get("asin") or product.get("product_id") or "unknown"
        safety = product.get("source_safety") or {}
        if safety.get("availability") == "UNKNOWN": add(identity, "UNKNOWN_SOURCE")
        if safety.get("freshness_status") in {"STALE_WARNING", "STALE_BLOCKED", "NEVER_VERIFIED"}: add(identity, "STALE_SOURCE")
        try: has_price = float(product.get("selling_price") or 0) > 0
        except (TypeError, ValueError): has_price = False
        if not has_price: add(identity, "MISSING_SELLING_PRICE")
        if not product.get("media_approved"): add(identity, "UNAPPROVED_PRODUCT_MEDIA")
    if snapshot.get("hero"):
        hero = snapshot["hero"]
        if not hero.get("approved_image"): add("homepage_hero", "UNAPPROVED_HERO_IMAGE")
        if not hero.get("cta_target") or hero.get("cta_target") == "#": add("homepage_hero", "HERO_WITHOUT_REAL_CTA")
    for item in snapshot.get("collections", []) or []:
        if not item.get("approved_image"): add(item.get("key", "collection"), "MISSING_COLLECTION_IMAGE")
        if not item.get("product_count", 0): add(item.get("key", "collection"), "EMPTY_COLLECTION")
    if snapshot.get("products") and not (snapshot.get("pricing_policy") or {}).get("enabled"):
        add("pricing", "MISSING_PRICING_POLICY")
    return sorted(findings, key=lambda x: (x["entity"], x["code"], x["reason"]))


def _gate_result(key: str, evidence: dict | None) -> dict:
    """Unknown evidence is never treated as success; explicit blockers dominate."""
    evidence = dict(evidence or {})
    blockers = list(evidence.get("blockers") or [])
    missing = list(evidence.get("missing_inputs") or [])
    review = list(evidence.get("review_required") or [])
    if evidence.get("status") in {"BLOCKED", "WAITING_FOR_INPUT", "WAITING_FOR_CONFIRMATION", "REVIEW_REQUIRED"}:
        status = evidence["status"]
    elif blockers: status = "BLOCKED"
    elif missing: status = "WAITING_FOR_INPUT"
    elif review: status = "REVIEW_REQUIRED"
    elif evidence.get("verified") is True: status = "VERIFIED"
    elif evidence.get("ready") is True: status = "READY"
    else:
        status = "REVIEW_REQUIRED"
        review.append("검증 가능한 근거가 없습니다.")
    return {"gate_key": key, "status": status, "evidence": redact_value(evidence),
            "blockers": blockers + missing + review}


class ProductionGoldenPathService:
    """Persistent gate/evidence service; no outbound clients are constructed."""
    def __init__(self, *, db=None, export_dir=None):
        self.db = db
        self.export_dir = Path(export_dir) if export_dir else EXPORT_DIR / "production_runs"
        _install(db)

    def start(self, store_id: str, *, expected_store_name: str = "Cabin Tidy") -> dict:
        run_id = "PROD_" + secrets.token_hex(8)
        now = _now()
        with connect(self.db) as con:
            con.execute("INSERT INTO production_runs(run_id,store_id,status,created_at,updated_at) VALUES(?,?,?, ?,?)",
                        (run_id, str(store_id), "RUNNING", now, now))
            con.executemany("INSERT INTO production_gates(run_id,gate_key,position,status,updated_at) VALUES(?,?,?,'NOT_STARTED',?)",
                            [(run_id, key, i, now) for i, key in enumerate(GATES)])
        if str(store_id) != "001" or expected_store_name.casefold() != "cabin tidy":
            self.update(run_id, {"ENVIRONMENT_STORE_IDENTITY": {"status": "BLOCKED",
                "blockers": ["이 Golden Path는 Store 001 | Cabin Tidy 전용입니다."]}})
        else:
            self.update(run_id, {"ENVIRONMENT_STORE_IDENTITY": {"status": "REVIEW_REQUIRED",
                "review_required": ["로컬 Store ID만으로 Shopify 연결/도메인/권한/테마를 확인할 수 없습니다."]}})
        # Reuse the Phase 4 StoreBuild plan/checkpoint rather than reimplementing
        # the detailed stages. PREVIEW mode only persists the local stage plan.
        from .store_build import StoreBuildOrchestrator
        build = StoreBuildOrchestrator(db=self.db).preview(str(store_id), options={
            "auto_sourcing": False, "product_sync": False, "collection_design": True,
            "collection_images": False, "paid_image_opt_in": False, "collection_sync": False,
            "homepage_plan": True, "publish_status": "DRAFT", "media_mode": "MANUAL_MEDIA",
            "brand_automation": False, "navigation_automation": False, "navigation_sync": False,
            "homepage_sync": False,
        }, mode="PREVIEW")
        with connect(self.db) as con:
            con.execute("UPDATE production_runs SET checkpoint_json=?,updated_at=? WHERE run_id=?",
                        (_json({"store_build_run_id": build["run_id"], "store_build_mode": "PREVIEW"}), _now(), run_id))
        return self.get(run_id)

    def get(self, run_id: str) -> dict:
        with connect(self.db) as con:
            run = con.execute("SELECT * FROM production_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run: raise KeyError(run_id)
            gates = [dict(r) for r in con.execute("SELECT * FROM production_gates WHERE run_id=? ORDER BY position", (run_id,))]
        result = dict(run)
        result["checkpoint"] = json.loads(result.pop("checkpoint_json") or "{}")
        result["summary"] = json.loads(result.pop("summary_json") or "{}")
        result["gates"] = [{**g, "evidence": json.loads(g.pop("evidence_json") or "{}"),
                            "blockers": json.loads(g.pop("blockers_json") or "[]")} for g in gates]
        return result

    def update(self, run_id: str, evidence_by_gate: dict[str, dict]) -> dict:
        run = self.get(run_id)
        unknown = set(evidence_by_gate) - set(GATES)
        if unknown: raise ValueError(f"Unknown production gate(s): {sorted(unknown)}")
        now = _now()
        with connect(self.db) as con:
            existing = {r["gate_key"]: r["status"] for r in con.execute(
                "SELECT gate_key,status FROM production_gates WHERE run_id=? ORDER BY position", (run_id,))}
            for key in sorted(evidence_by_gate, key=GATES.index):
                evidence = evidence_by_gate[key]
                item = _gate_result(key, evidence)
                position = GATES.index(key)
                if key == "FINAL_LAUNCH_READINESS" and item["status"] in {"READY", "READY_WITH_WARNINGS"}:
                    item["status"] = "REVIEW_REQUIRED"
                    item["blockers"].append("최종 출시 gate는 VERIFIED 증거가 있어야 통과합니다.")
                prior_unresolved = [gate for gate in GATES[:position]
                                    if existing.get(gate, "NOT_STARTED") not in {"READY", "READY_WITH_WARNINGS", "VERIFIED"}]
                if prior_unresolved and key == "FINAL_LAUNCH_READINESS":
                    item["status"] = "BLOCKED"
                    item["blockers"].append("최종 출시 선행 gate 미완료: " + ", ".join(prior_unresolved))
                if prior_unresolved and item["status"] in {"READY", "READY_WITH_WARNINGS", "VERIFIED"}:
                    item["status"] = "BLOCKED"
                    item["blockers"].append("선행 gate 미완료: " + ", ".join(prior_unresolved))
                con.execute("UPDATE production_gates SET status=?,evidence_json=?,blockers_json=?,updated_at=? WHERE run_id=? AND gate_key=?",
                            (item["status"], _json(item["evidence"]), _json(item["blockers"]), now, run_id, key))
                existing[key] = item["status"]
            rows = list(con.execute("SELECT gate_key,status,blockers_json FROM production_gates WHERE run_id=? ORDER BY position", (run_id,)))
            counts = {state: sum(r["status"] == state for r in rows) for state in GATE_STATES}
            blockers = [{"gate": r["gate_key"], "reason": reason} for r in rows
                        if r["status"] in {"BLOCKED", "WAITING_FOR_INPUT", "WAITING_FOR_CONFIRMATION", "REVIEW_REQUIRED"}
                        for reason in json.loads(r["blockers_json"])]
            blockers.extend({"gate": r["gate_key"], "reason": "아직 검증되지 않았습니다."}
                            for r in rows if r["status"] == "NOT_STARTED")
            complete = len(rows) and all(r["status"] in {"READY", "READY_WITH_WARNINGS", "VERIFIED"} for r in rows)
            warning_count = sum(r["status"] == "READY_WITH_WARNINGS" for r in rows)
            status = ("READY_WITH_WARNINGS" if warning_count else "READY_TO_LAUNCH") if complete and not blockers else "NOT_READY"
            summary = {"status": status, "gate_counts": counts, "blocker_count": len(blockers),
                       "blockers": blockers, "completion_percent": round(sum(r["status"] in {"READY", "READY_WITH_WARNINGS", "VERIFIED"} for r in rows) * 100 / max(1, len(rows)))}
            gate_index = 0
            for row in rows:
                if row["status"] not in {"READY", "READY_WITH_WARNINGS", "VERIFIED"}: break
                gate_index += 1
            con.execute("UPDATE production_runs SET status=?,gate_index=?,summary_json=?,updated_at=? WHERE run_id=?",
                        (status, gate_index, _json(summary), now, run_id))
        return self.get(run_id)

    def evaluate_catalog(self, products: list[dict]) -> list[dict]:
        """Batch-friendly pure catalog audit; keeps one output row per input."""
        return [{"product_id": p.get("id", p.get("product_id")), "asin": p.get("asin"),
                 **inspect_product(p, safety=p.get("source_safety"), media_policy=p.get("media_policy"),
                                   pricing_policy=p.get("pricing_policy"))} for p in products]

    def audit_master(self, store_id: str) -> list[dict]:
        """Read MASTER + store decisions + latest source evidence in bounded queries."""
        from .source_safety import SourceSafetyService
        SourceSafetyService(self.db)  # idempotent additive schema install only
        with connect(self.db) as con:
            products = [dict(r) for r in con.execute("""
                SELECT p.id,p.asin,p.title,p.brand,p.price,p.currency,p.category,p.tags_json,p.overview_json,
                       p.about_json,p.images_json,p.options_json,p.source_url,p.url,p.archived,
                       d.final_status,d.manual_override,d.reasons_json AS decision_reasons
                FROM products p LEFT JOIN store_product_decisions d ON d.product_id=p.id AND d.store_id=?
                ORDER BY p.id
            """, (str(store_id),))]
            product_ids = [p["id"] for p in products]
            snapshots = {}
            if product_ids:
                # Fetch latest snapshot per product with a single indexed window query.
                placeholders = ",".join("?" for _ in product_ids)
                for row in con.execute(f"""SELECT product_id,availability,availability_confidence,source_price,source_currency,
                    observed_at FROM (SELECT product_id,availability,availability_confidence,source_price,source_currency,observed_at,
                    ROW_NUMBER() OVER(PARTITION BY product_id ORDER BY observed_at DESC,id DESC) AS rank
                    FROM source_product_snapshots WHERE product_id IN ({placeholders})) WHERE rank=1""", product_ids):
                    snapshots[row["product_id"]] = dict(row)
        output = []
        for product in products:
            for field in ("tags_json", "overview_json", "about_json", "images_json", "options_json", "decision_reasons"):
                try: product[field.removesuffix("_json")] = json.loads(product.pop(field) or ("{}" if field == "options_json" else "[]"))
                except (json.JSONDecodeError, TypeError): product[field.removesuffix("_json")] = []
            snapshot = snapshots.get(product["id"])
            status = str(product.get("final_status") or "").upper()
            # Manual PRIMARY never overrides source or policy gates.
            product["store_relevant"] = status in {"PRIMARY", "RESERVE", "REVIEW"}
            product["restricted"] = status == "RESTRICTED"
            product["source_safety"] = {"availability": snapshot.get("availability") if snapshot else "UNKNOWN",
                                         "freshness_status": "REVIEW_REQUIRED", "sellability_status": "UNKNOWN",
                                         "margin_status": "UNKNOWN", "snapshot_id_present": bool(snapshot)}
            product["source_safety"]["snapshot_fresh"] = False
            output.append({"product_id": product["id"], "asin": product["asin"], **inspect_product(product, safety=product["source_safety"])})
        return output

    def set_media_rights(self, store_id: str, product_id: int, policy: str, *, reviewed=False, notes="") -> dict:
        policy = str(policy).upper()
        if policy not in MEDIA_POLICIES: raise ValueError("Unknown product media policy")
        now = _now() if reviewed else None
        with connect(self.db) as con:
            con.execute("INSERT INTO product_media_rights(store_id,product_id,policy,reviewed_at,notes) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(store_id,product_id) DO UPDATE SET policy=excluded.policy,reviewed_at=excluded.reviewed_at,notes=excluded.notes",
                        (str(store_id), int(product_id), policy, now, str(notes)[:500]))
        return {"store_id": str(store_id), "product_id": int(product_id), "policy": policy,
                "reviewed": bool(reviewed), "active_allowed": policy in ALLOWED_PRODUCT_MEDIA and bool(reviewed)}

    def pilot_preview(self, audited_products: list[dict], *, limit=10) -> dict:
        """Select only fully qualified products; always DRAFT and never writes."""
        if int(limit) < 1 or int(limit) > 10: raise ValueError("Production pilot is capped at 10 products")
        candidates = [dict(row) for row in audited_products if row.get("classification") == "PRODUCTION_CANDIDATE" and row.get("publishable") is True]
        selected = candidates[:int(limit)]
        return {"status": "READY_FOR_EXPLICIT_CONFIRMATION" if selected else "BLOCKED",
                "requested_limit": int(limit), "max_products": 10, "selected_count": len(selected),
                "publish_status": "DRAFT", "items": [{**row, "publish_status": "DRAFT"} for row in selected],
                "preview_fingerprint": _fingerprint(selected), "write_performed": False,
                "blockers": [] if selected else ["production candidate 없음"]}

    def verify_pilot_preview(self, preview: dict, *, current_fingerprint: str, identity_rows: list[dict]) -> dict:
        if preview.get("publish_status") != "DRAFT" or preview.get("selected_count", 0) > 10:
            return {"status": "BLOCKED", "reason": "INVALID_PILOT_PREVIEW"}
        if preview.get("preview_fingerprint") != current_fingerprint:
            return {"status": "BLOCKED", "reason": "PREVIEW_STALE"}
        if len(identity_rows) != preview.get("selected_count") or any(not row.get("identity_match") or row.get("duplicate") for row in identity_rows):
            return {"status": "BLOCKED", "reason": "IDENTITY_MISMATCH_OR_DUPLICATE"}
        return {"status": "READY_FOR_EXPLICIT_CONFIRMATION", "verified_count": len(identity_rows),
                "publish_status": "DRAFT", "write_performed": False}

    def assess(self, run_id: str, evidence: dict) -> dict:
        """Apply production blockers to phase service evidence and persist report."""
        findings = no_placeholder_audit(evidence)
        mapped = dict(evidence.get("gates") or {})
        if findings:
            mapped["FINAL_LAUNCH_READINESS"] = {"blockers": [f"{x['entity']}: {x['code']}" for x in findings]}
        result = self.update(run_id, mapped)
        self.write_report(run_id, evidence, findings)
        return result

    def rollout(self, store_id: str, *, verified=True, critical_mismatch=False, explicit_confirmed=False) -> dict:
        """Enforce 10→50→200 DRAFT batches; never changes ACTIVE or calls Shopify."""
        if critical_mismatch:
            target = self._record_rollout(store_id, mismatch=True)
            return {"status": "BLOCKED", "batch_size": 0, "next_batch": target, "reason": "CRITICAL_MISMATCH"}
        current = self._get_rollout(store_id)
        if current["critical_mismatches"]:
            return {"status": "BLOCKED", "batch_size": 0, "reason": "CRITICAL_MISMATCH"}
        index = current["batch_index"]
        if index >= len(ROLLOUT): return {"status": "REVIEW_REQUIRED", "batch_size": 0, "next_batch": None, "reason": "LARGER_BATCH_REQUIRES_REVIEW"}
        if not explicit_confirmed: return {"status": "WAITING_FOR_CONFIRMATION", "batch_size": ROLLOUT[index], "publish_status": "DRAFT", "write_performed": False}
        if not verified: return {"status": "BLOCKED", "batch_size": 0, "reason": "PREVIOUS_BATCH_NOT_VERIFIED"}
        return {"status": "READY_FOR_EXPLICIT_WRITE", "batch_size": ROLLOUT[index], "publish_status": "DRAFT", "write_performed": False,
                "next_batch": None}

    def record_batch_verification(self, store_id: str, *, expected_count: int, verified_count: int,
                                  critical_mismatch=False) -> dict:
        """Advance rollout checkpoint only after matching remote reread evidence."""
        current = self._get_rollout(store_id)
        index = current["batch_index"]
        if current["critical_mismatches"] or index >= len(ROLLOUT):
            return {"status": "BLOCKED", "reason": "ROLLOUT_STOPPED_OR_COMPLETE"}
        if critical_mismatch or int(expected_count) != ROLLOUT[index] or int(verified_count) != int(expected_count):
            self._record_rollout(store_id, mismatch=True)
            return {"status": "BLOCKED", "reason": "CRITICAL_MISMATCH", "next_batch": None}
        saved = self._record_rollout(store_id, verified_count=int(verified_count))
        next_size = ROLLOUT[saved["batch_index"]] if saved["batch_index"] < len(ROLLOUT) else None
        return {"status": "VERIFIED", "verified_count": int(verified_count), "next_batch": next_size}

    def active_publication_gate(self, *, blockers: list[str], explicit_confirmed=False) -> dict:
        if blockers: return {"status": "BLOCKED", "reasons": list(blockers), "publish_status": "DRAFT", "write_performed": False}
        if not explicit_confirmed: return {"status": "WAITING_FOR_CONFIRMATION", "publish_status": "DRAFT", "write_performed": False}
        return {"status": "READY_FOR_EXPLICIT_PUBLICATION", "publish_status": "ACTIVE", "write_performed": False}

    def _get_rollout(self, store_id):
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM production_rollouts WHERE store_id=?", (str(store_id),)).fetchone()
        return dict(row) if row else {"store_id": str(store_id), "batch_index": 0, "last_verified_count": 0, "critical_mismatches": 0}

    def _record_rollout(self, store_id, *, verified_count=0, mismatch=False):
        state = self._get_rollout(store_id)
        now = _now()
        with connect(self.db) as con:
            con.execute("INSERT INTO production_rollouts(store_id,batch_index,last_verified_count,critical_mismatches,updated_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(store_id) DO UPDATE SET batch_index=excluded.batch_index,last_verified_count=excluded.last_verified_count,critical_mismatches=excluded.critical_mismatches,updated_at=excluded.updated_at",
                        (str(store_id), state["batch_index"] if mismatch else min(state["batch_index"] + 1, len(ROLLOUT)),
                         state["last_verified_count"] if mismatch else int(verified_count), state["critical_mismatches"] + int(mismatch), now))
        return self._get_rollout(store_id)

    def write_report(self, run_id: str, evidence: dict, findings: list[dict] | None = None) -> Path:
        run = self.get(run_id)
        target = self.export_dir / str(run["store_id"]) / run_id
        target.mkdir(parents=True, exist_ok=True)
        findings = findings if findings is not None else no_placeholder_audit(evidence)
        report = {"run_id": run_id, "store_id": run["store_id"], "status": run["status"],
                  "summary": run["summary"], "gates": run["gates"], "blockers": findings,
                  "provider_calls": 0, "shopify_writes": 0, "theme_writes": 0}
        files = {
            "production_summary.json": report, "blockers.json": findings,
            "source_safety_summary.json": evidence.get("source_safety", {}),
            "asset_readiness.json": evidence.get("asset_readiness", {}),
            "homepage_readiness.json": evidence.get("homepage", {}),
            "seo_accessibility.json": evidence.get("seo_accessibility", {}),
            "commerce_readiness.json": evidence.get("commerce", {}),
            "shopify_reconciliation.json": evidence.get("shopify_reconciliation", {}),
        }
        for name, value in files.items(): (target / name).write_text(_json(value), encoding="utf-8")
        csv_sets = {
            "sourcing_audit.csv": evidence.get("sourcing_audit", []), "product_quality.csv": evidence.get("product_quality", []),
            "media_rights.csv": evidence.get("media_rights", []), "pricing_margin.csv": evidence.get("pricing_margin", []),
            "collection_matrix.csv": evidence.get("collection_matrix", []), "navigation_link_audit.csv": evidence.get("navigation_link_audit", []),
        }
        for name, rows in csv_sets.items():
            rows = list(rows or [])
            with (target / name).open("w", encoding="utf-8-sig", newline="") as stream:
                keys = sorted({k for row in rows for k in row}) or ["status"]
                writer = csv.DictWriter(stream, fieldnames=keys, extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)
        (target / "final_launch_report.md").write_text(
            "# Cabin Tidy 실전 스토어 최종 점검\n\n"
            f"- 판정: {run['status']}\n- 완료 gate: {run['summary'].get('completion_percent', 0)}%\n"
            f"- 차단/확인 항목: {len(findings)}\n- Shopify 변경: 없음\n- 실제 provider 실행: 없음\n\n"
            + "\n".join(f"- [{item['severity']}] {item['entity']}: {item['code']} — 다음 작업을 확인하세요." for item in findings),
            encoding="utf-8")
        return target
