"""Approval-gated homepage category strategies and read-only collection reconciliation."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher

from .db import connect, init_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")[:80]


def _words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _validate_strategy(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("Category strategy must contain an items list")
    if len(value["items"]) != 4:
        raise ValueError("An approvable homepage category strategy requires exactly four items")
    normalized, keys = [], set()
    for position, source in enumerate(value["items"], 1):
        if not isinstance(source, dict):
            raise ValueError("Each category strategy item must be an object")
        title = " ".join(str(source.get("title") or "").split())
        key = _slug(source.get("collection_key") or source.get("category_key") or title)
        conditions = source.get("conditions") or []
        signals = source.get("match_signals") or []
        if isinstance(signals, str):
            signals = [signals]
        if not title or not key or key in keys:
            raise ValueError("Strategy category titles and collection keys must be unique and nonempty")
        if not conditions and not signals:
            raise ValueError(f"{title} requires deterministic conditions or match signals")
        keys.add(key)
        normalized.append({
            "category_key": _slug(source.get("category_key") or key),
            "collection_key": key, "title": title,
            "match_mode": str(source.get("match_mode") or "ANY").upper(),
            "conditions": conditions, "match_signals": [str(item) for item in signals if str(item).strip()],
            "usefulness": float(source.get("usefulness") or (100 - position)),
            "priority": int(source.get("priority") or position),
            "preferred_handle": _slug(source.get("preferred_handle") or "") or None,
            "image_prompt": str(source.get("image_prompt") or ""),
            "notes": str(source.get("notes") or ""),
            "evidence": source.get("evidence") if isinstance(source.get("evidence"), dict) else {},
        })
    return {"items": normalized, "notes": str(value.get("notes") or "")}


class HomepageCategoryStrategyService:
    """Versioned, store-isolated strategy repository. Drafts never affect readiness."""

    def __init__(self, db=None):
        self.db = db
        init_db(db)
        with connect(db) as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS homepage_category_strategies (
              strategy_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, version INTEGER NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('DRAFT','APPROVED','SUPERSEDED')),
              source TEXT NOT NULL CHECK(source IN ('MANUAL','SUGGESTED','COLLECTION_PLAN')),
              strategy_json TEXT NOT NULL, created_at TEXT NOT NULL, approved_at TEXT,
              updated_at TEXT NOT NULL, UNIQUE(store_id,version)
            );
            CREATE INDEX IF NOT EXISTS idx_homepage_category_strategy_store
              ON homepage_category_strategies(store_id,status,version DESC);
            """)

    @staticmethod
    def _row(row):
        if not row:
            return None
        result = dict(row)
        result["strategy"] = json.loads(result.pop("strategy_json"))
        return result

    def latest(self, store_id: str, *, include_draft: bool = True):
        statuses = ("DRAFT", "APPROVED") if include_draft else ("APPROVED",)
        marks = ",".join("?" for _ in statuses)
        with connect(self.db) as con:
            row = con.execute(f"""SELECT * FROM homepage_category_strategies
              WHERE store_id=? AND status IN ({marks}) ORDER BY version DESC LIMIT 1""",
              (str(store_id), *statuses)).fetchone()
        return self._row(row)

    def effective(self, store_id: str):
        with connect(self.db) as con:
            row = con.execute("""SELECT * FROM homepage_category_strategies
              WHERE store_id=? AND status='APPROVED' ORDER BY version DESC LIMIT 1""",
              (str(store_id),)).fetchone()
        return self._row(row)

    def create_draft(self, store_id: str, strategy: dict, *, source: str = "MANUAL"):
        store_id, source = str(store_id).strip(), str(source).upper()
        if not store_id:
            raise ValueError("A store ID is required")
        if source not in {"MANUAL", "SUGGESTED", "COLLECTION_PLAN"}:
            raise ValueError("Unsupported category strategy source")
        value, now = _validate_strategy(strategy), _now()
        with connect(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            version = int(con.execute("SELECT COALESCE(MAX(version),0)+1 FROM homepage_category_strategies WHERE store_id=?",
                                      (store_id,)).fetchone()[0])
            strategy_id = "HCS_" + uuid.uuid4().hex
            con.execute("""INSERT INTO homepage_category_strategies
              (strategy_id,store_id,version,status,source,strategy_json,created_at,approved_at,updated_at)
              VALUES(?,?,?,'DRAFT',?,?,?,NULL,?)""",
              (strategy_id, store_id, version, source, json.dumps(value, ensure_ascii=False, sort_keys=True), now, now))
            row = con.execute("SELECT * FROM homepage_category_strategies WHERE strategy_id=?", (strategy_id,)).fetchone()
        return self._row(row)

    def approve(self, strategy_id: str, *, confirmed: bool = False):
        if confirmed is not True:
            raise PermissionError("Explicit confirmation is required to approve a category strategy")
        now = _now()
        with connect(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM homepage_category_strategies WHERE strategy_id=?", (strategy_id,)).fetchone()
            if not row:
                raise KeyError(strategy_id)
            if row["status"] == "APPROVED":
                return self._row(row)
            if row["status"] != "DRAFT":
                raise ValueError("Only a draft strategy can be approved")
            con.execute("UPDATE homepage_category_strategies SET status='SUPERSEDED',updated_at=? WHERE store_id=? AND status='APPROVED'",
                        (now, row["store_id"]))
            con.execute("UPDATE homepage_category_strategies SET status='APPROVED',approved_at=?,updated_at=? WHERE strategy_id=?",
                        (now, now, strategy_id))
            approved = con.execute("SELECT * FROM homepage_category_strategies WHERE strategy_id=?", (strategy_id,)).fetchone()
        return self._row(approved)

    def candidates(self, store_id: str, products: list[dict], *, simulate_draft: bool = False):
        record = self.latest(store_id, include_draft=True) if simulate_draft else self.effective(store_id)
        if not record or (record["status"] == "DRAFT" and not simulate_draft):
            return [], record
        from .category_shortcut_readiness import _candidate_matches
        result = []
        for source in record["strategy"]["items"]:
            item = dict(source)
            item.update(candidate_source="CATEGORY_STRATEGY", source_strategy_id=record["strategy_id"],
                        strategy_status=record["status"], proposed_handle=source.get("preferred_handle") or "")
            item["product_count"] = sum(_candidate_matches(product, item) for product in products)
            result.append(item)
        return result, record

    def publisher_plan(self, store_id: str, *, simulate_draft: bool = False) -> dict:
        record = self.latest(store_id, include_draft=True) if simulate_draft else self.effective(store_id)
        if not record or (record["status"] != "APPROVED" and not simulate_draft):
            raise RuntimeError("An approved category strategy is required")
        collections = []
        for item in record["strategy"]["items"]:
            collections.append({
                "collection_key": item["collection_key"], "title": item["title"],
                "handle": item.get("preferred_handle") or _slug(item["title"]),
                "match_mode": item.get("match_mode", "ANY"), "conditions": item.get("conditions") or [],
                "estimated_product_count": (item.get("evidence") or {}).get("matched_count", 0),
                "image_prompt": item.get("image_prompt", ""),
            })
        return {"plan_id": record["strategy_id"], "store_id": str(store_id),
                "strategy_status": record["status"], "collections": collections}

    def suggest_from_specs(self, store_id: str, specs: list[dict]):
        """Create an evidence-rich draft from caller-supplied store concepts; never approve it."""
        if len(specs) != 4:
            raise ValueError("Exactly four reviewed category concepts are required")
        from .category_shortcut_readiness import (
            _candidate_matches, _eligible_products, _json, category_image_prompt,
        )
        from .db import get_store
        with connect(self.db) as con:
            row = con.execute("SELECT candidates_json FROM homepage_featured_product_remote_cache WHERE store_id=?",
                              (str(store_id),)).fetchone()
        if not row:
            raise RuntimeError("A fresh read-only product cache is required")
        profile = get_store(str(store_id), self.db)
        products, _excluded = _eligible_products(_json(row[0], []), profile)
        matched_sets, prepared = [], []
        for position, source in enumerate(specs, 1):
            candidate = {**source, "collection_key": source.get("collection_key") or _slug(source.get("title")),
                         "category_key": source.get("category_key") or source.get("collection_key") or _slug(source.get("title")),
                         "conditions": source.get("conditions") or [], "match_signals": source.get("match_signals") or [],
                         "match_mode": source.get("match_mode") or "ANY"}
            matches = [product for product in products if _candidate_matches(product, candidate)]
            if not matches:
                raise ValueError(f"{source.get('title')}: no eligible cached products matched")
            identities = {str(product.get("shopify_product_id") or product.get("source_key")) for product in matches}
            matched_sets.append(identities)
            prepared.append({**candidate, "priority": position,
                "preferred_handle": source.get("preferred_handle") or _slug(source.get("title")),
                "image_prompt": source.get("image_prompt") or category_image_prompt(source.get("title"), profile),
                "notes": source.get("notes") or "Suggested from current eligible cached product evidence; review before approval.",
                "evidence": {"matched_count": len(matches),
                    "sample_titles": [str(product.get("title") or "")[:240] for product in matches[:10]],
                    "selection_reason": source.get("selection_reason") or "Nonzero verified products and explicit customer intent.",
                    "not_fallback_reason": "Customer-facing title and deterministic rules were explicitly supplied and evidenced."}})
        for index, item in enumerate(prepared):
            overlaps = {}
            for other_index, other in enumerate(prepared):
                if other_index == index:
                    continue
                denominator = max(1, min(len(matched_sets[index]), len(matched_sets[other_index])))
                overlaps[other["collection_key"]] = round(len(matched_sets[index] & matched_sets[other_index]) / denominator, 3)
            item["evidence"]["overlap"] = overlaps
        return self.create_draft(str(store_id), {"items": prepared,
            "notes": "SUGGESTED evidence snapshot; inactive until explicit user approval."}, source="SUGGESTED")


class ExistingCollectionReconciliationService:
    """Propose and explicitly adopt existing Shopify collections without remote writes."""

    def __init__(self, db=None):
        self.db = db
        init_db(db)
        from .shopify_collections import _install_schema
        _install_schema(db)
        with connect(db) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS shopify_collection_adoptions (
              adoption_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, collection_key TEXT NOT NULL,
              shopify_collection_id TEXT NOT NULL, handle TEXT NOT NULL, products_count INTEGER,
              products_count_precision TEXT, adoption_source TEXT NOT NULL, adopted_at TEXT NOT NULL,
              evidence_json TEXT NOT NULL)
            """)

    def propose(self, store_id: str, local: dict, remote_collections: list[dict]) -> dict:
        key = str(local.get("collection_key") or "")
        title = str(local.get("title") or "")
        handle = str(local.get("handle") or local.get("preferred_handle") or local.get("proposed_handle") or "")
        candidates = []
        for remote in remote_collections or []:
            if not remote.get("id") or not remote.get("handle"):
                continue
            exact_id = bool(local.get("shopify_collection_id") and local.get("shopify_collection_id") == remote.get("id"))
            exact_handle = bool(handle and handle.casefold() == str(remote.get("handle")).casefold())
            title_equal = bool(title and _words(title) == _words(remote.get("title")))
            similarity = SequenceMatcher(None, _slug(handle), _slug(remote.get("handle"))).ratio() if handle else 0.0
            count = remote.get("products_count")
            sane_count = isinstance(count, int) and count > 0
            score = 100 if exact_id else 95 if exact_handle else (82 if title_equal and sane_count else 65 if title_equal else round(similarity * 60))
            if exact_id or exact_handle or title_equal or similarity >= .65:
                candidates.append({"remote": remote, "score": score, "exact_id": exact_id,
                                   "exact_handle": exact_handle, "normalized_title_match": title_equal,
                                   "handle_similarity": round(similarity, 3), "product_count_sane": sane_count})
        candidates.sort(key=lambda item: (-item["score"], str(item["remote"].get("id"))))
        top = candidates[0] if candidates else None
        tied = bool(top and len(candidates) > 1 and candidates[1]["score"] == top["score"])
        if not top:
            status = "NO_MATCH"
        elif tied:
            status = "AMBIGUOUS"
        elif top["exact_id"] or top["exact_handle"]:
            status = "EXACT_MATCH"
        elif top["score"] >= 80:
            status = "HIGH_CONFIDENCE_CANDIDATE"
        else:
            status = "CONFLICT"
        return {"store_id": str(store_id), "collection_key": key, "local": local,
                "status": status, "candidate": top, "candidates": candidates,
                "user_confirmation_required": status in {"EXACT_MATCH", "HIGH_CONFIDENCE_CANDIDATE"},
                "remote_write_performed": False}

    def adopt(self, proposal: dict, *, confirmed: bool = False) -> dict:
        if confirmed is not True:
            raise PermissionError("Explicit confirmation is required to link an existing collection")
        if proposal.get("status") not in {"EXACT_MATCH", "HIGH_CONFIDENCE_CANDIDATE"}:
            raise ValueError("Only an unambiguous reconciliation proposal can be adopted")
        candidate = (proposal.get("candidate") or {}).get("remote") or {}
        store_id, key = str(proposal.get("store_id") or ""), str(proposal.get("collection_key") or "")
        if not store_id or not key or not candidate.get("id") or not candidate.get("handle"):
            raise ValueError("Incomplete reconciliation identity")
        now = _now()
        evidence = json.dumps(proposal, ensure_ascii=False, sort_keys=True, default=str)
        fingerprint = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
        adoption_id = "HCA_" + uuid.uuid4().hex
        with connect(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("""INSERT INTO shopify_collection_mappings
              (store_id,collection_key,handle,shopify_collection_id,last_synced_hash,last_synced_at,published_ids_json,image_url)
              VALUES(?,?,?,?,?,?,'[]',NULL)
              ON CONFLICT(store_id,collection_key) DO UPDATE SET handle=excluded.handle,
              shopify_collection_id=excluded.shopify_collection_id,last_synced_hash=excluded.last_synced_hash,
              last_synced_at=excluded.last_synced_at,published_ids_json='[]'""",
              (store_id, key, candidate["handle"], candidate["id"], fingerprint, now))
            con.execute("""INSERT INTO shopify_collection_adoptions
              (adoption_id,store_id,collection_key,shopify_collection_id,handle,products_count,
               products_count_precision,adoption_source,adopted_at,evidence_json)
              VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (adoption_id, store_id, key, candidate["id"], candidate["handle"],
               candidate.get("products_count"), candidate.get("products_count_precision"),
               "USER_CONFIRMED_EXISTING_COLLECTION", now, evidence))
        return {"adoption_id": adoption_id, "store_id": store_id, "collection_key": key,
                "shopify_collection_id": candidate["id"], "handle": candidate["handle"],
                "published_ids": [], "adoption_source": "USER_CONFIRMED_EXISTING_COLLECTION",
                "shopify_write_performed": False}
