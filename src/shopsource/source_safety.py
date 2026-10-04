"""Source availability, freshness, sellability and read-only inventory safety.

The module is intentionally provider-neutral.  Provider calls are injected;
none of the services starts network work or mutates Shopify on its own.
"""
from __future__ import annotations

import json
import math
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import connect
from .security import redact_value

AVAILABILITY = {"IN_STOCK", "OUT_OF_STOCK", "LIMITED", "PREORDER", "BACKORDER", "UNKNOWN", "SOURCE_ERROR"}
CONFIDENCE = {"HIGH", "MEDIUM", "LOW", "UNKNOWN"}
FRESHNESS = {"FRESH", "STALE_WARNING", "STALE_BLOCKED", "NEVER_VERIFIED"}
SELLABILITY = {"SELLABLE", "BLOCKED_SOURCE_OUT_OF_STOCK", "BLOCKED_SOURCE_UNKNOWN",
               "BLOCKED_SOURCE_STALE", "BLOCKED_MARGIN", "BLOCKED_SOURCE_ERROR",
               "NEEDS_PRICING_POLICY", "NEEDS_REVIEW"}

DEFAULT_FRESHNESS_POLICY = {
    "pre_list_max_age_minutes": 60, "active_monitor_interval_hours": 4,
    "price_warning_interval_hours": 1, "out_of_stock_recheck_hours": 12,
    "reserve_recheck_hours": 24, "stale_warning_hours": 8, "stale_block_hours": 24,
    "restock_confirmation_count": 2,
}
DEFAULT_PRICE_POLICY = {
    "enabled": False, "min_margin_amount": None, "min_margin_percent": None,
    "source_cost_buffer_fixed": 0, "source_cost_buffer_percent": 0,
    "additional_cost_buffer_fixed": 0, "additional_cost_buffer_percent": 0,
    "warning_source_price_change_percent": None, "block_on_margin_breach": True,
    "auto_reprice_enabled": False, "max_auto_price_change_percent": None, "currency": "USD",
}


def _now(): return datetime.now(timezone.utc).isoformat()
def _dt(value): return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_product_snapshots (
 id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, asin TEXT NOT NULL,
 source_platform TEXT NOT NULL, source_kind TEXT NOT NULL, source_price REAL,
 source_currency TEXT, availability TEXT NOT NULL, availability_confidence TEXT NOT NULL,
 observed_at TEXT NOT NULL, provider_updated_at TEXT, evidence_kind TEXT NOT NULL,
 evidence_json TEXT NOT NULL DEFAULT '{}', source_url TEXT, check_run_id TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_source_snapshots_product_observed ON source_product_snapshots(product_id,observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_snapshots_asin_observed ON source_product_snapshots(asin,observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_snapshots_availability_observed ON source_product_snapshots(availability,observed_at DESC);
CREATE TABLE IF NOT EXISTS source_monitoring_state (
 product_id INTEGER PRIMARY KEY, last_checked_at TEXT, last_success_at TEXT, last_snapshot_id INTEGER,
 latest_availability TEXT NOT NULL DEFAULT 'UNKNOWN', latest_source_price REAL,
 freshness_status TEXT NOT NULL DEFAULT 'NEVER_VERIFIED', consecutive_errors INTEGER NOT NULL DEFAULT 0,
 consecutive_in_stock INTEGER NOT NULL DEFAULT 0, next_check_at TEXT, volatility_score REAL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_safety_store_settings (
 store_id TEXT PRIMARY KEY, freshness_json TEXT NOT NULL, price_policy_json TEXT NOT NULL,
 inventory_control_mode TEXT NOT NULL DEFAULT 'UNMANAGED', enforcement_mode TEXT NOT NULL DEFAULT 'PREVIEW_ONLY',
 auto_pause_on_source_oos INTEGER NOT NULL DEFAULT 0, auto_restore_on_restock INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS store_product_sellability (
 store_id TEXT NOT NULL, product_id INTEGER NOT NULL, snapshot_id INTEGER,
 source_availability TEXT NOT NULL, freshness_status TEXT NOT NULL, current_source_price REAL,
 source_price_at_first_seen REAL, source_price_at_listing REAL, previous_source_price REAL,
 selling_price REAL, price_change_amount REAL, price_change_percent REAL, effective_source_cost REAL,
 estimated_margin_amount REAL, estimated_margin_percent REAL, margin_status TEXT NOT NULL,
 sellability_status TEXT NOT NULL, reasons_json TEXT NOT NULL DEFAULT '[]', evaluated_at TEXT NOT NULL,
 expires_at TEXT, paused_by_shopsource INTEGER NOT NULL DEFAULT 0, pause_reason TEXT,
 pause_snapshot_id INTEGER, previous_shopify_status TEXT, paused_at TEXT,
 PRIMARY KEY(store_id,product_id));
CREATE INDEX IF NOT EXISTS idx_sellability_store_status ON store_product_sellability(store_id,sellability_status);
CREATE TABLE IF NOT EXISTS source_check_runs (
 run_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, mode TEXT NOT NULL, provider TEXT NOT NULL,
 requested_count INTEGER NOT NULL, checked_count INTEGER NOT NULL DEFAULT 0,
 in_stock_count INTEGER NOT NULL DEFAULT 0, out_of_stock_count INTEGER NOT NULL DEFAULT 0,
 unknown_count INTEGER NOT NULL DEFAULT 0, changed_price_count INTEGER NOT NULL DEFAULT 0,
 failed_count INTEGER NOT NULL DEFAULT 0, token_estimate INTEGER NOT NULL DEFAULT 0,
 tokens_consumed INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, checkpoint_json TEXT NOT NULL DEFAULT '{}',
 started_at TEXT, finished_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS source_check_items (
 run_id TEXT NOT NULL, product_id INTEGER NOT NULL, priority INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT, PRIMARY KEY(run_id,product_id));
CREATE TABLE IF NOT EXISTS shopify_inventory_snapshots (
 id INTEGER PRIMARY KEY AUTOINCREMENT, store_id TEXT NOT NULL, product_id INTEGER NOT NULL,
 shopify_product_id TEXT, shopify_variant_id TEXT, inventory_item_id TEXT, product_status TEXT,
 available_for_sale INTEGER, inventory_quantity INTEGER, inventory_policy TEXT,
 inventory_tracked INTEGER, available_quantity_by_location_json TEXT NOT NULL DEFAULT '{}',
 publication_state TEXT, observed_at TEXT NOT NULL);
"""


def ensure_source_safety_schema(db=None):
    with connect(db) as con:
        con.executescript(SCHEMA)


def browser_source_observation(*, jsonld_availability=None, visible_text=None, search_hint=False):
    mapping = {"instock": "IN_STOCK", "outofstock": "OUT_OF_STOCK",
               "limitedavailability": "LIMITED", "preorder": "PREORDER", "backorder": "BACKORDER"}
    if jsonld_availability:
        key = str(jsonld_availability).rstrip("/").split("/")[-1].casefold()
        if key in mapping:
            return {"availability": mapping[key], "availability_confidence": "HIGH",
                    "evidence_kind": "JSON_LD_OFFER_AVAILABILITY", "evidence": {"value": str(jsonld_availability)},
                    "qualifies_for_sellability": not search_hint}
    text = " ".join(str(visible_text or "").casefold().split())[:160]
    if text:
        if any(x in text for x in ("currently unavailable", "out of stock")): availability = "OUT_OF_STOCK"
        elif "in stock" in text: availability = "IN_STOCK"
        elif "pre-order" in text or "preorder" in text: availability = "PREORDER"
        elif "backorder" in text: availability = "BACKORDER"
        elif "limited" in text: availability = "LIMITED"
        else: availability = "UNKNOWN"
        if availability != "UNKNOWN":
            return {"availability": availability, "availability_confidence": "MEDIUM",
                    "evidence_kind": "SEARCH_AVAILABILITY_HINT" if search_hint else "VISIBLE_AVAILABILITY_TEXT",
                    "evidence": {"normalized": text}, "qualifies_for_sellability": False if search_hint else True}
    return {"availability": "UNKNOWN", "availability_confidence": "UNKNOWN",
            "evidence_kind": "NO_AVAILABILITY_EVIDENCE", "evidence": {}, "qualifies_for_sellability": False}


def keepa_source_observation(raw: dict | None, *, provider_error=None):
    if provider_error:
        return {"availability": "SOURCE_ERROR", "availability_confidence": "UNKNOWN",
                "source_price": None, "evidence_kind": "PROVIDER_ERROR", "evidence": {"message": "provider request failed"}}
    raw = raw or {}
    # Adapter input uses normalized documented Keepa evidence, never guesses from one absent price type.
    current_new = raw.get("current_new_price")
    offer_count = raw.get("current_new_offer_count")
    explicit_no_offer = raw.get("documented_no_new_offer") is True
    if isinstance(current_new, (int, float)) and current_new > 0 and (offer_count is None or offer_count > 0):
        availability, confidence = "IN_STOCK", "MEDIUM"
    elif explicit_no_offer and offer_count == 0:
        availability, confidence = "OUT_OF_STOCK", "MEDIUM"
    else:
        availability, confidence = "UNKNOWN", "UNKNOWN"
    return {"availability": availability, "availability_confidence": confidence,
            "source_price": current_new if isinstance(current_new, (int, float)) and current_new > 0 else None,
            "source_currency": raw.get("currency", "USD"), "provider_updated_at": raw.get("provider_updated_at"),
            "evidence_kind": "KEEPA_CURRENT_NEW_OFFER", "evidence": {"offer_count": offer_count,
                                                                       "explicit_no_offer": explicit_no_offer}}


class SourceSafetyService:
    def __init__(self, db=None): self.db = db; ensure_source_safety_schema(db)

    def settings(self, store_id):
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM source_safety_store_settings WHERE store_id=?", (store_id,)).fetchone()
        if not row:
            return {"freshness": dict(DEFAULT_FRESHNESS_POLICY), "price_policy": dict(DEFAULT_PRICE_POLICY),
                    "inventory_control_mode": "UNMANAGED", "enforcement_mode": "PREVIEW_ONLY"}
        return {"freshness": {**DEFAULT_FRESHNESS_POLICY, **json.loads(row["freshness_json"])},
                "price_policy": {**DEFAULT_PRICE_POLICY, **json.loads(row["price_policy_json"])},
                "inventory_control_mode": row["inventory_control_mode"], "enforcement_mode": row["enforcement_mode"]}

    def save_settings(self, store_id, *, freshness=None, price_policy=None, inventory_control_mode="UNMANAGED", enforcement_mode="PREVIEW_ONLY"):
        if inventory_control_mode not in {"UNMANAGED", "ALERT_ONLY", "SHOP_SOURCE_MANAGED"}: raise ValueError("invalid inventory mode")
        current = self.settings(store_id)
        freshness = {**current["freshness"], **(freshness or {})}; price = {**current["price_policy"], **(price_policy or {})}
        price["auto_reprice_enabled"] = bool(price.get("auto_reprice_enabled", False))
        with connect(self.db) as con:
            con.execute("""INSERT INTO source_safety_store_settings(store_id,freshness_json,price_policy_json,inventory_control_mode,enforcement_mode,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET freshness_json=excluded.freshness_json,
                price_policy_json=excluded.price_policy_json,inventory_control_mode=excluded.inventory_control_mode,
                enforcement_mode=excluded.enforcement_mode,updated_at=excluded.updated_at""",
                (store_id,json.dumps(freshness),json.dumps(price),inventory_control_mode,enforcement_mode,_now()))

    def record_snapshot(self, product_id, asin, observation, *, source_platform="AMAZON", source_kind="MANUAL", check_run_id=None, source_url=None):
        availability = str(observation.get("availability") or "UNKNOWN").upper()
        confidence = str(observation.get("availability_confidence") or "UNKNOWN").upper()
        if availability not in AVAILABILITY or confidence not in CONFIDENCE: raise ValueError("invalid source observation")
        observed = observation.get("observed_at") or _now()
        evidence = redact_value(observation.get("evidence") or {})
        encoded = json.dumps(evidence, ensure_ascii=False)
        if len(encoded) > 4096 or "<html" in encoded.casefold(): raise ValueError("compact evidence only; page HTML is forbidden")
        price = observation.get("source_price")
        with connect(self.db) as con:
            previous = con.execute("SELECT * FROM source_product_snapshots WHERE product_id=? AND availability!='SOURCE_ERROR' ORDER BY observed_at DESC,id DESC LIMIT 1", (product_id,)).fetchone()
            cur = con.execute("""INSERT INTO source_product_snapshots(product_id,asin,source_platform,source_kind,source_price,source_currency,
                availability,availability_confidence,observed_at,provider_updated_at,evidence_kind,evidence_json,source_url,check_run_id,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (product_id,asin,source_platform,source_kind,price,
                observation.get("source_currency") or "USD",availability,confidence,observed,observation.get("provider_updated_at"),
                observation.get("evidence_kind") or "UNKNOWN",encoded,source_url,check_run_id,_now()))
            snapshot_id = cur.lastrowid
            state = con.execute("SELECT * FROM source_monitoring_state WHERE product_id=?", (product_id,)).fetchone()
            errors = (int(state["consecutive_errors"]) + 1) if availability == "SOURCE_ERROR" and state else (1 if availability == "SOURCE_ERROR" else 0)
            consecutive = ((int(state["consecutive_in_stock"]) if state else 0) + 1) if availability == "IN_STOCK" else 0
            successful = availability != "SOURCE_ERROR"
            con.execute("""INSERT INTO source_monitoring_state(product_id,last_checked_at,last_success_at,last_snapshot_id,latest_availability,
                latest_source_price,freshness_status,consecutive_errors,consecutive_in_stock,next_check_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET last_checked_at=excluded.last_checked_at,
                last_success_at=CASE WHEN ? THEN excluded.last_success_at ELSE source_monitoring_state.last_success_at END,
                last_snapshot_id=excluded.last_snapshot_id,latest_availability=CASE WHEN ? THEN excluded.latest_availability ELSE source_monitoring_state.latest_availability END,
                latest_source_price=CASE WHEN ? THEN excluded.latest_source_price ELSE source_monitoring_state.latest_source_price END,
                freshness_status=excluded.freshness_status,consecutive_errors=excluded.consecutive_errors,
                consecutive_in_stock=CASE WHEN ? THEN excluded.consecutive_in_stock ELSE source_monitoring_state.consecutive_in_stock END,updated_at=excluded.updated_at""",
                (product_id,observed,observed if successful else None,snapshot_id,availability,price,"FRESH" if successful else "NEVER_VERIFIED",
                 errors,consecutive,None,_now(),successful,successful,successful,successful))
        prev_price = previous["source_price"] if previous else None
        delta = price - prev_price if price is not None and prev_price is not None else None
        pct = delta / prev_price * 100 if delta is not None and prev_price else None
        return {"snapshot_id": snapshot_id, "previous_source_price": prev_price, "current_source_price": price,
                "price_change_amount": delta, "price_change_percent": pct}

    def freshness(self, observed_at, store_id, *, pre_list=False, now=None):
        if not observed_at: return "NEVER_VERIFIED"
        now = now or datetime.now(timezone.utc); age = now - _dt(observed_at)
        policy = self.settings(store_id)["freshness"]
        if pre_list and age > timedelta(minutes=policy["pre_list_max_age_minutes"]): return "STALE_BLOCKED"
        if age > timedelta(hours=policy["stale_block_hours"]): return "STALE_BLOCKED"
        if age > timedelta(hours=policy["stale_warning_hours"]): return "STALE_WARNING"
        return "FRESH"

    def evaluate(self, store_id, product_id, *, selling_price=None, pre_list=False):
        with connect(self.db) as con:
            snapshots = con.execute("SELECT * FROM source_product_snapshots WHERE product_id=? AND availability!='SOURCE_ERROR' ORDER BY observed_at DESC,id DESC LIMIT 2", (product_id,)).fetchall()
            latest_any = con.execute("SELECT * FROM source_product_snapshots WHERE product_id=? ORDER BY observed_at DESC,id DESC LIMIT 1", (product_id,)).fetchone()
        latest = snapshots[0] if snapshots else None; previous = snapshots[1] if len(snapshots)>1 else None
        availability = latest["availability"] if latest else "UNKNOWN"
        freshness = self.freshness(latest["observed_at"] if latest else None, store_id, pre_list=pre_list)
        reasons=[]
        if latest_any and latest_any["availability"] == "SOURCE_ERROR" and not latest: status="BLOCKED_SOURCE_ERROR"; reasons.append("SOURCE_ERROR")
        elif availability == "OUT_OF_STOCK": status="BLOCKED_SOURCE_OUT_OF_STOCK"; reasons.append("SOURCE_OUT_OF_STOCK")
        elif availability != "IN_STOCK": status="BLOCKED_SOURCE_UNKNOWN"; reasons.append("SOURCE_UNKNOWN")
        elif freshness != "FRESH": status="BLOCKED_SOURCE_STALE"; reasons.append("SOURCE_STALE")
        else: status="SELLABLE"
        price_policy = self.settings(store_id)["price_policy"]
        current = latest["source_price"] if latest else None; currency = latest["source_currency"] if latest else None
        previous_price = previous["source_price"] if previous else None
        delta = current-previous_price if current is not None and previous_price is not None else None
        delta_pct = delta/previous_price*100 if delta is not None and previous_price else None
        effective=margin=margin_pct=None; margin_status="NOT_CONFIGURED"
        configured = price_policy.get("enabled") and (price_policy.get("min_margin_amount") is not None or price_policy.get("min_margin_percent") is not None)
        if status == "SELLABLE" and currency and currency != price_policy.get("currency", "USD"):
            status="NEEDS_REVIEW"; reasons.append("CURRENCY_MISMATCH"); margin_status="CURRENCY_MISMATCH"
        elif status == "SELLABLE" and not configured:
            status="NEEDS_PRICING_POLICY"; reasons.append("NEEDS_PRICING_POLICY")
        elif status == "SELLABLE" and configured:
            if current is None or selling_price is None: status="NEEDS_REVIEW"; reasons.append("MISSING_PRICE"); margin_status="UNKNOWN"
            else:
                effective=current+float(price_policy.get("source_cost_buffer_fixed") or 0)+current*float(price_policy.get("source_cost_buffer_percent") or 0)/100
                effective+=float(price_policy.get("additional_cost_buffer_fixed") or 0)+current*float(price_policy.get("additional_cost_buffer_percent") or 0)/100
                margin=float(selling_price)-effective; margin_pct=margin/float(selling_price)*100 if selling_price else None
                passed=(price_policy.get("min_margin_amount") is None or margin >= float(price_policy["min_margin_amount"])) and (price_policy.get("min_margin_percent") is None or margin_pct >= float(price_policy["min_margin_percent"]))
                margin_status="PASS" if passed else "BLOCKED"
                if not passed and price_policy.get("block_on_margin_breach",True): status="BLOCKED_MARGIN"; reasons.append("MARGIN_BLOCKED")
        result={"store_id":store_id,"product_id":product_id,"snapshot_id":latest["id"] if latest else None,
                "source_availability":availability,"freshness_status":freshness,"current_source_price":current,
                "previous_source_price":previous_price,"selling_price":selling_price,"price_change_amount":delta,
                "price_change_percent":delta_pct,"effective_source_cost":effective,"estimated_margin_amount":margin,
                "estimated_margin_percent":margin_pct,"margin_status":margin_status,"sellability_status":status,"reasons":reasons}
        with connect(self.db) as con:
            first=con.execute("SELECT source_price FROM source_product_snapshots WHERE product_id=? AND source_price IS NOT NULL ORDER BY observed_at,id LIMIT 1",(product_id,)).fetchone()
            con.execute("""INSERT INTO store_product_sellability(store_id,product_id,snapshot_id,source_availability,freshness_status,current_source_price,
                source_price_at_first_seen,previous_source_price,selling_price,price_change_amount,price_change_percent,effective_source_cost,
                estimated_margin_amount,estimated_margin_percent,margin_status,sellability_status,reasons_json,evaluated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(store_id,product_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,
                source_availability=excluded.source_availability,freshness_status=excluded.freshness_status,current_source_price=excluded.current_source_price,
                previous_source_price=excluded.previous_source_price,selling_price=excluded.selling_price,price_change_amount=excluded.price_change_amount,
                price_change_percent=excluded.price_change_percent,effective_source_cost=excluded.effective_source_cost,
                estimated_margin_amount=excluded.estimated_margin_amount,estimated_margin_percent=excluded.estimated_margin_percent,
                margin_status=excluded.margin_status,sellability_status=excluded.sellability_status,reasons_json=excluded.reasons_json,evaluated_at=excluded.evaluated_at""",
                (store_id,product_id,result["snapshot_id"],availability,freshness,current,first[0] if first else None,previous_price,selling_price,delta,delta_pct,effective,margin,margin_pct,margin_status,status,json.dumps(reasons),_now()))
        return result

    def evaluate_many(self, store_id, product_ids):
        """Recompute sellability for a batch with bounded indexed reads/writes."""
        ids = sorted({int(value) for value in product_ids})
        if not ids: return []
        settings = self.settings(store_id); policy = settings["price_policy"]; freshness_policy = settings["freshness"]
        snapshots = {}; latest_anys = {}; product_data = {}; first_prices = {}
        # Stay below SQLite's variable limit while avoiding one lookup per product.
        with connect(self.db) as con:
            for start in range(0, len(ids), 500):
                group = ids[start:start + 500]; marks = ",".join("?" for _ in group)
                query = f"""SELECT * FROM (SELECT s.*,ROW_NUMBER() OVER(PARTITION BY product_id ORDER BY observed_at DESC,id DESC) rank
                    FROM source_product_snapshots s WHERE product_id IN ({marks}) AND availability!='SOURCE_ERROR') WHERE rank<=2 ORDER BY product_id,rank"""
                for row in con.execute(query, group): snapshots.setdefault(row["product_id"], []).append(dict(row))
                query = f"""SELECT * FROM (SELECT s.*,ROW_NUMBER() OVER(PARTITION BY product_id ORDER BY observed_at DESC,id DESC) rank
                    FROM source_product_snapshots s WHERE product_id IN ({marks})) WHERE rank=1"""
                latest_anys.update({row["product_id"]: dict(row) for row in con.execute(query, group)})
                for row in con.execute(f"SELECT id,raw_json FROM products WHERE id IN ({marks})", group): product_data[row["id"]] = row["raw_json"]
                query = f"""SELECT * FROM (SELECT product_id,source_price,ROW_NUMBER() OVER(PARTITION BY product_id ORDER BY observed_at,id) rank
                    FROM source_product_snapshots WHERE product_id IN ({marks}) AND source_price IS NOT NULL) WHERE rank=1"""
                first_prices.update({row["product_id"]: row["source_price"] for row in con.execute(query, group)})
        now_dt = datetime.now(timezone.utc); now = _now(); writes = []; results = []
        for product_id in ids:
            history = snapshots.get(product_id, [])
            latest_any = latest_anys.get(product_id)
            successful = history[0] if history else None
            previous = history[1] if len(history) > 1 else None
            availability = successful["availability"] if successful else "UNKNOWN"
            observed = _dt(successful["observed_at"]) if successful else None
            if observed is None: freshness = "NEVER_VERIFIED"
            elif now_dt-observed > timedelta(hours=float(freshness_policy["stale_block_hours"])): freshness = "STALE_BLOCKED"
            elif now_dt-observed > timedelta(hours=float(freshness_policy["stale_warning_hours"])): freshness = "STALE_WARNING"
            else: freshness = "FRESH"
            reasons=[]
            if latest_any and latest_any["availability"] == "SOURCE_ERROR" and not successful: status="BLOCKED_SOURCE_ERROR"; reasons.append("SOURCE_ERROR")
            elif availability == "OUT_OF_STOCK": status="BLOCKED_SOURCE_OUT_OF_STOCK"; reasons.append("SOURCE_OUT_OF_STOCK")
            elif availability != "IN_STOCK": status="BLOCKED_SOURCE_UNKNOWN"; reasons.append("SOURCE_UNKNOWN")
            elif freshness != "FRESH": status="BLOCKED_SOURCE_STALE"; reasons.append("SOURCE_STALE")
            else: status="SELLABLE"
            try:
                raw = json.loads(product_data.get(product_id) or "{}")
                if not isinstance(raw, dict): raw = {}
                selling = raw.get("shopify_selling_price", raw.get("store_selling_price"))
                if selling is not None: selling=float(selling)
            except (TypeError, ValueError, json.JSONDecodeError): selling=None
            current = successful.get("source_price") if successful else None
            currency = successful.get("source_currency") if successful else None
            previous_price = previous.get("source_price") if previous else None
            delta = current-previous_price if current is not None and previous_price is not None else None
            delta_pct = delta/previous_price*100 if delta is not None and previous_price else None
            effective=margin=margin_pct=None; margin_status="NOT_CONFIGURED"
            configured=policy.get("enabled") and (policy.get("min_margin_amount") is not None or policy.get("min_margin_percent") is not None)
            if status=="SELLABLE" and currency and currency != policy.get("currency", "USD"):
                status="NEEDS_REVIEW"; reasons.append("CURRENCY_MISMATCH"); margin_status="CURRENCY_MISMATCH"
            elif status=="SELLABLE" and not configured:
                status="NEEDS_PRICING_POLICY"; reasons.append("NEEDS_PRICING_POLICY")
            elif status=="SELLABLE":
                if current is None or selling is None: status="NEEDS_REVIEW"; reasons.append("MISSING_PRICE"); margin_status="UNKNOWN"
                else:
                    effective=current+float(policy.get("source_cost_buffer_fixed") or 0)+current*float(policy.get("source_cost_buffer_percent") or 0)/100
                    effective+=float(policy.get("additional_cost_buffer_fixed") or 0)+current*float(policy.get("additional_cost_buffer_percent") or 0)/100
                    margin=selling-effective; margin_pct=margin/selling*100 if selling else None
                    passed=(policy.get("min_margin_amount") is None or margin>=float(policy["min_margin_amount"])) and (policy.get("min_margin_percent") is None or margin_pct>=float(policy["min_margin_percent"]))
                    margin_status="PASS" if passed else "BLOCKED"
                    if not passed and policy.get("block_on_margin_breach",True): status="BLOCKED_MARGIN"; reasons.append("MARGIN_BLOCKED")
            result={"store_id":store_id,"product_id":product_id,"snapshot_id":successful["id"] if successful else None,
                    "source_availability":availability,"freshness_status":freshness,"current_source_price":current,
                    "previous_source_price":previous_price,"selling_price":selling,"price_change_amount":delta,
                    "price_change_percent":delta_pct,"effective_source_cost":effective,"estimated_margin_amount":margin,
                    "estimated_margin_percent":margin_pct,"margin_status":margin_status,"sellability_status":status,"reasons":reasons}
            results.append(result)
            writes.append((store_id,product_id,result["snapshot_id"],availability,freshness,current,first_prices.get(product_id),previous_price,selling,delta,delta_pct,effective,margin,margin_pct,margin_status,status,json.dumps(reasons),now))
        with connect(self.db) as con:
            con.executemany("""INSERT INTO store_product_sellability(store_id,product_id,snapshot_id,source_availability,freshness_status,current_source_price,
                source_price_at_first_seen,previous_source_price,selling_price,price_change_amount,price_change_percent,effective_source_cost,
                estimated_margin_amount,estimated_margin_percent,margin_status,sellability_status,reasons_json,evaluated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(store_id,product_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,
                source_availability=excluded.source_availability,freshness_status=excluded.freshness_status,current_source_price=excluded.current_source_price,
                source_price_at_first_seen=excluded.source_price_at_first_seen,previous_source_price=excluded.previous_source_price,selling_price=excluded.selling_price,
                price_change_amount=excluded.price_change_amount,price_change_percent=excluded.price_change_percent,effective_source_cost=excluded.effective_source_cost,
                estimated_margin_amount=excluded.estimated_margin_amount,estimated_margin_percent=excluded.estimated_margin_percent,
                margin_status=excluded.margin_status,sellability_status=excluded.sellability_status,reasons_json=excluded.reasons_json,evaluated_at=excluded.evaluated_at""", writes)
        return results

    def snapshot_fingerprint(self, store_id, product_ids):
        if not product_ids: return []
        marks=",".join("?" for _ in product_ids)
        with connect(self.db) as con:
            rows=con.execute(f"""SELECT s.product_id,s.id,s.availability,s.observed_at,s.source_price FROM source_product_snapshots s
                JOIN (SELECT product_id,MAX(id) id FROM source_product_snapshots WHERE product_id IN ({marks}) GROUP BY product_id) x ON x.id=s.id
                ORDER BY s.product_id""", tuple(product_ids)).fetchall()
        return [tuple(row) for row in rows]

    def bulk_latest_snapshots(self, *, limit=None):
        """One indexed latest-per-product query for audit/stress summary paths."""
        sql="""SELECT s.product_id,s.id,s.availability,s.source_price,s.source_currency,s.observed_at
            FROM source_product_snapshots s JOIN
            (SELECT product_id,MAX(id) id FROM source_product_snapshots GROUP BY product_id) latest ON latest.id=s.id
            ORDER BY s.product_id"""
        params=()
        if limit is not None: sql+=" LIMIT ?"; params=(int(limit),)
        with connect(self.db) as con: return [dict(row) for row in con.execute(sql,params)]

    def live_safe_filter(self, store_id, rows):
        """Filter a proposed Spark/Shopify handoff without deleting MASTER rows."""
        allowed, excluded = [], {}
        mapping = {"BLOCKED_SOURCE_OUT_OF_STOCK":"SOURCE_OUT_OF_STOCK",
                   "BLOCKED_SOURCE_UNKNOWN":"SOURCE_UNKNOWN", "BLOCKED_SOURCE_STALE":"SOURCE_STALE",
                   "BLOCKED_SOURCE_ERROR":"SOURCE_ERROR", "BLOCKED_MARGIN":"MARGIN_BLOCKED",
                   "NEEDS_PRICING_POLICY":"NEEDS_PRICING_POLICY", "NEEDS_REVIEW":"SOURCE_UNKNOWN"}
        with connect(self.db) as con:
            states={r["product_id"]:dict(r) for r in con.execute(
                "SELECT * FROM store_product_sellability WHERE store_id=?",(store_id,))}
        for row in rows:
            status=str(row.get("final_status") or "").upper()
            if row.get("missing_product"): reason="MISSING_PRODUCT"
            elif row.get("missing_decision"): reason="MISSING_DECISION"
            elif row.get("archived") or status=="ARCHIVED": reason="ARCHIVED"
            elif status=="RESTRICTED": reason="RESTRICTED"
            elif row.get("product_id") not in states: reason="MISSING_SNAPSHOT"
            elif states[row["product_id"]]["sellability_status"]!="SELLABLE": reason=mapping.get(states[row["product_id"]]["sellability_status"],"SOURCE_UNKNOWN")
            else: allowed.append(row); continue
            excluded[reason]=excluded.get(reason,0)+1
        return {"items":allowed,"counts":{"included":len(allowed),"excluded":sum(excluded.values())},
                "source_exclusion_counts":excluded,"package_version":"LIVE_SAFE_V2"}

    def beginner_summary(self, store_id):
        with connect(self.db) as con:
            total=con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            latest=con.execute("SELECT COUNT(*) FROM source_monitoring_state WHERE freshness_status='FRESH'").fetchone()[0]
            availability={r[0]:r[1] for r in con.execute("SELECT latest_availability,COUNT(*) FROM source_monitoring_state GROUP BY latest_availability")}
            states={r[0]:r[1] for r in con.execute("SELECT sellability_status,COUNT(*) FROM store_product_sellability WHERE store_id=? GROUP BY sellability_status",(store_id,))}
            changed=con.execute("SELECT COUNT(*) FROM store_product_sellability WHERE store_id=? AND price_change_amount IS NOT NULL AND price_change_amount!=0",(store_id,)).fetchone()[0]
        blocked=sum(v for k,v in states.items() if k.startswith("BLOCKED_"))
        attention=sum(v for k,v in states.items() if k in {"NEEDS_PRICING_POLICY","NEEDS_REVIEW"})
        return {"targets":total,"fresh":latest,"out_of_stock":availability.get("OUT_OF_STOCK",0),
                "attention":attention,"price_changed":changed,"blocked":blocked,
                "sellable":states.get("SELLABLE",0),"recheck":availability.get("UNKNOWN",0)+availability.get("SOURCE_ERROR",0)}

    def result_page(self, store_id, *, page=0, page_size=50, search="", filter_key="ALL"):
        size=max(1,min(50,int(page_size))); clauses=["s.store_id=?"]; params=[store_id]
        if search.strip(): clauses.append("(p.asin LIKE ? OR p.title LIKE ?)"); term=f"%{search.strip()}%"; params.extend((term,term))
        filters={"SELLABLE":"s.sellability_status='SELLABLE'","OOS":"s.source_availability='OUT_OF_STOCK'",
                 "ATTENTION":"s.sellability_status IN ('NEEDS_PRICING_POLICY','NEEDS_REVIEW','BLOCKED_SOURCE_UNKNOWN')",
                 "PRICE":"s.price_change_amount IS NOT NULL AND s.price_change_amount!=0","BLOCKED":"s.sellability_status LIKE 'BLOCKED_%'",
                 "RESTOCK":"m.consecutive_in_stock>=2 AND s.source_availability='IN_STOCK'"}
        if filter_key in filters: clauses.append(filters[filter_key])
        where=" AND ".join(clauses)
        base=""" FROM store_product_sellability s JOIN products p ON p.id=s.product_id
            LEFT JOIN source_monitoring_state m ON m.product_id=s.product_id"""
        with connect(self.db) as con:
            total=con.execute("SELECT COUNT(*)"+base+" WHERE "+where,params).fetchone()[0]
            rows=[dict(r) for r in con.execute("""SELECT p.title,p.asin,s.source_availability,s.current_source_price,
                s.freshness_status,s.sellability_status,s.price_change_amount,s.reasons_json,m.last_checked_at,m.consecutive_in_stock"""+
                base+" WHERE "+where+" ORDER BY p.title,p.asin LIMIT ? OFFSET ?",[*params,size,max(0,int(page))*size])]
        return {"rows":rows,"total":total,"page":max(0,int(page)),"page_size":size}


class SoldOutDiagnosticService:
    """Pure read-only classification; it never infers source OOS from Shopify."""
    @staticmethod
    def diagnose(source: dict, shopify: dict):
        availability=source.get("availability","UNKNOWN"); freshness=source.get("freshness_status","NEVER_VERIFIED")
        if source.get("margin_blocked"): return "MARGIN_BLOCKED"
        if freshness in {"STALE_WARNING","STALE_BLOCKED","NEVER_VERIFIED"}: return "SOURCE_STALE"
        if shopify.get("inventory_tracked") is False: return "SHOPIFY_INVENTORY_NOT_TRACKED"
        if str(shopify.get("inventory_policy") or "").upper() == "CONTINUE": return "SHOPIFY_CONTINUE_SELLING_WHEN_OOS"
        if str(shopify.get("product_status") or "ACTIVE").upper() != "ACTIVE" or shopify.get("publication_state") is False:
            return "SHOPIFY_NOT_PUBLISHED_OR_NOT_AVAILABLE"
        available=bool(shopify.get("available_for_sale")); quantity=shopify.get("inventory_quantity")
        if availability=="IN_STOCK":
            if not available and quantity == 0: return "SHOPIFY_ZERO_AVAILABLE_QUANTITY"
            return "SOURCE_IN_STOCK_SHOPIFY_AVAILABLE" if available else "SOURCE_IN_STOCK_SHOPIFY_SOLD_OUT"
        if availability=="OUT_OF_STOCK": return "SOURCE_OUT_OF_STOCK_SHOPIFY_AVAILABLE" if available else "SOURCE_OUT_OF_STOCK_SHOPIFY_SOLD_OUT"
        if availability in {"UNKNOWN","SOURCE_ERROR"}: return "SOURCE_UNKNOWN_SHOPIFY_AVAILABLE" if available else "SOURCE_UNKNOWN_SHOPIFY_SOLD_OUT"
        return "UNKNOWN"

    def inspect(self, pairs):
        return [{**row, "diagnosis": self.diagnose(row.get("source",{}),row.get("shopify",{}))} for row in pairs]


class SourceMonitorService:
    def __init__(self, db=None, provider=None, reports_root="exports/source_safety_reports"):
        self.db=db; self.provider=provider; self.safety=SourceSafetyService(db); self.reports_root=Path(reports_root)

    def preview_due_checks(self, store_id, *, limit=100, offset=0):
        with connect(self.db) as con:
            mapped = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shopify_product_mappings'").fetchone()
            if mapped:
                rows=con.execute("""SELECT p.id product_id,p.asin,
                    CASE WHEN m.shopify_product_id IS NOT NULL THEN 1 WHEN sm.latest_availability='OUT_OF_STOCK' THEN 3 ELSE 4 END priority,
                    sm.freshness_status FROM products p LEFT JOIN source_monitoring_state sm ON sm.product_id=p.id
                    LEFT JOIN shopify_product_mappings m ON m.store_id=? AND m.master_product_id=p.id
                    WHERE sm.next_check_at IS NULL OR sm.next_check_at<=? ORDER BY priority,p.id LIMIT ? OFFSET ?""",(store_id,_now(),limit,offset)).fetchall()
            else:
                rows=con.execute("""SELECT p.id product_id,p.asin,
                    CASE WHEN sm.latest_availability='OUT_OF_STOCK' THEN 3 ELSE 4 END priority,sm.freshness_status
                    FROM products p LEFT JOIN source_monitoring_state sm ON sm.product_id=p.id
                    WHERE sm.next_check_at IS NULL OR sm.next_check_at<=? ORDER BY priority,p.id LIMIT ? OFFSET ?""",(_now(),limit,offset)).fetchall()
        return {"store_id":store_id,"items":[dict(r) for r in rows],"preview_only":True,"batch_size":100,
                "estimated_batches":math.ceil(len(rows)/100),"estimated_tokens":len(rows),"offset":offset,
                "estimated_cost_usd":None,"cost_estimate_available":False,
                "cost_estimate_note":"Provider-specific cost is unavailable until a provider adapter supplies a quote."}

    def run_due_checks(self, store_id, *, observations=None, max_retries=3, limit=100, offset=0):
        preview=self.preview_due_checks(store_id,limit=limit,offset=offset); run_id="SOURCE_"+secrets.token_hex(8); now=_now()
        with connect(self.db) as con:
            con.execute("INSERT INTO source_check_runs(run_id,store_id,mode,provider,requested_count,token_estimate,status,checkpoint_json,started_at) VALUES(?,?,?,?,?,?,?,'{}',?)",
                        (run_id,store_id,"MANUAL_AUDIT","INJECTED" if observations is not None else "CONFIGURED",len(preview["items"]),preview["estimated_tokens"],"RUNNING",now))
            con.executemany("INSERT INTO source_check_items(run_id,product_id,priority,status) VALUES(?,?,?,'PENDING')",[(run_id,x["product_id"],x["priority"]) for x in preview["items"]])
        if observations is None:
            return self.status(run_id)  # explicit caller/provider execution required; no hidden network.
        self._process(run_id, observations, max_retries=max_retries)
        return self.status(run_id)

    def _process(self, run_id, observations, *, max_retries=3, failed_only=False):
        with connect(self.db) as con:
            run=con.execute("SELECT * FROM source_check_runs WHERE run_id=?",(run_id,)).fetchone()
            query="SELECT i.*,p.asin FROM source_check_items i JOIN products p ON p.id=i.product_id WHERE i.run_id=?"
            if failed_only: query+=" AND i.status='FAILED'"
            items=con.execute(query+" ORDER BY i.priority,i.product_id",(run_id,)).fetchall()
        freshness_settings=self.safety.settings(run["store_id"])["freshness"]
        for item in items:
            try:
                obs=observations.get(item["product_id"])
                if isinstance(obs, Exception): raise obs
                if not obs: raise RuntimeError("missing observation")
                self.safety.record_snapshot(item["product_id"],item["asin"],obs,check_run_id=run_id)
                availability=str(obs.get("availability") or "UNKNOWN").upper()
                interval_hours=(freshness_settings["out_of_stock_recheck_hours"] if availability=="OUT_OF_STOCK" else
                                freshness_settings["reserve_recheck_hours"] if availability in {"LIMITED","PREORDER","BACKORDER"} else
                                freshness_settings["active_monitor_interval_hours"] if availability=="IN_STOCK" else 0.0)
                due=(datetime.now(timezone.utc)+timedelta(hours=float(interval_hours))).isoformat()
                with connect(self.db) as con:
                    con.execute("UPDATE source_monitoring_state SET next_check_at=? WHERE product_id=?",(due,item["product_id"]))
                    con.execute("UPDATE source_check_items SET status='COMPLETE',attempts=attempts+1,error=NULL WHERE run_id=? AND product_id=?",(run_id,item["product_id"]))
            except Exception as exc:
                attempts=int(item["attempts"])+1
                with connect(self.db) as con: con.execute("UPDATE source_check_items SET status=?,attempts=?,error=? WHERE run_id=? AND product_id=?",("FAILED",attempts,str(exc)[:300],run_id,item["product_id"]))
        with connect(self.db) as con:
            counts={r[0]:r[1] for r in con.execute("SELECT status,COUNT(*) FROM source_check_items WHERE run_id=? GROUP BY status",(run_id,))}
            con.execute("UPDATE source_check_runs SET checked_count=?,failed_count=?,status=?,checkpoint_json=?,finished_at=? WHERE run_id=?",
                        (counts.get("COMPLETE",0),counts.get("FAILED",0),"FAILED" if counts.get("FAILED") else "COMPLETE",json.dumps({"counts":counts}),_now(),run_id))

    def retry_failed(self, run_id, *, observations, max_retries=3): self._process(run_id,observations,max_retries=max_retries,failed_only=True); return self.status(run_id)
    def status(self, run_id):
        with connect(self.db) as con: row=con.execute("SELECT * FROM source_check_runs WHERE run_id=?",(run_id,)).fetchone()
        if not row: raise KeyError(run_id)
        return dict(row)

    def build_actions(self, store_id):
        settings=self.safety.settings(store_id); required=settings["freshness"]["restock_confirmation_count"]
        with connect(self.db) as con:
            rows=con.execute("""SELECT s.*,m.consecutive_in_stock FROM store_product_sellability s
                LEFT JOIN source_monitoring_state m ON m.product_id=s.product_id WHERE s.store_id=? ORDER BY s.product_id""",(store_id,)).fetchall()
        actions=[]
        for row in rows:
            if row["sellability_status"]=="BLOCKED_SOURCE_OUT_OF_STOCK": action="PAUSE_LISTING"
            elif row["sellability_status"]=="BLOCKED_SOURCE_STALE": action="PAUSE_LISTING"
            elif row["source_availability"]=="IN_STOCK" and int(row["consecutive_in_stock"] or 0)>=required and row["freshness_status"]=="FRESH" and row["margin_status"]!="BLOCKED": action="RESTORE_ELIGIBLE"
            elif row["sellability_status"] in {"BLOCKED_SOURCE_UNKNOWN","BLOCKED_SOURCE_ERROR"}: action="RECHECK_REQUIRED"
            elif row["sellability_status"] in {"BLOCKED_MARGIN","NEEDS_REVIEW"}: action="MANUAL_REVIEW"
            elif row["price_change_percent"]: action="WARN_PRICE_CHANGE"
            else: action="NO_ACTION"
            if action=="RESTORE_ELIGIBLE" and not row["paused_by_shopsource"]: action="MANUAL_REVIEW"
            actions.append({"product_id":row["product_id"],"action":action,"live_mutation":False})
        return actions

    def generate_report(self, store_id, run_id):
        run=self.status(run_id); root=self.reports_root/str(store_id)/run_id; root.mkdir(parents=True,exist_ok=True)
        actions=self.build_actions(store_id)
        with connect(self.db) as con:
            availability=[dict(r) for r in con.execute("""SELECT product_id,source_availability,freshness_status
                FROM store_product_sellability WHERE store_id=? ORDER BY product_id""",(store_id,))]
            prices=[dict(r) for r in con.execute("""SELECT product_id,previous_source_price,current_source_price,price_change_amount,price_change_percent
                FROM store_product_sellability WHERE store_id=? AND price_change_amount IS NOT NULL ORDER BY product_id""",(store_id,))]
            sellability=[dict(r) for r in con.execute("SELECT product_id,sellability_status,reasons_json FROM store_product_sellability WHERE store_id=? ORDER BY product_id",(store_id,))]
        payloads={"source_check_summary.json":run,"availability_changes.json":availability,"price_changes.json":prices,
                  "sellability_changes.json":sellability,"sold_out_diagnostics.json":[],"action_plan.json":actions}
        for name,value in payloads.items(): (root/name).write_text(json.dumps(redact_value(value),ensure_ascii=False,indent=2),encoding="utf-8")
        (root/"summary.md").write_text(f"# Source Safety\n\n- checked: {run['checked_count']}\n- failed: {run['failed_count']}\n- live mutations: 0\n",encoding="utf-8")
        return {"report_dir":str(root),"files":[*payloads,"summary.md"]}
