"""Approval-gated homepage category strategies and read-only collection reconciliation."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher

from .db import connect, init_db

MEMBERSHIP_PAGE_SIZE = 100
MEMBERSHIP_SAFE_CAP = 500
COMPATIBLE_RECALL = 0.80
COMPATIBLE_PRECISION = 0.70
COUNT_RATIO_MAX = 1.75
COUNT_RATIO_MIN = 0.57
COLLECTION_MEMBERSHIP_QUERY = """query ShopSourceCollectionMembership($id: ID!, $first: Int!, $after: String) {
  collection(id: $id) {
    id title handle productsCount { count precision }
    products(first: $first, after: $after) {
      nodes { id handle }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""


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
                        source_strategy_version=record["version"], strategy_status=record["status"],
                        proposed_handle=source.get("preferred_handle") or "")
            item["product_count"] = sum(_candidate_matches(product, item) for product in products)
            result.append(item)
        return result, record

    def publisher_plan(self, store_id: str, *, simulate_draft: bool = False) -> dict:
        record = self.latest(store_id, include_draft=True) if simulate_draft else self.effective(store_id)
        if not record or (record["status"] != "APPROVED" and not simulate_draft):
            raise RuntimeError("An approved category strategy is required")
        collections = []
        for item in record["strategy"]["items"]:
            evidence = item.get("evidence") or {}
            collections.append({
                "collection_key": item["collection_key"], "title": item["title"],
                "handle": item.get("preferred_handle") or _slug(item["title"]),
                "match_mode": item.get("match_mode", "ANY"), "conditions": item.get("conditions") or [],
                "estimated_product_count": evidence.get("matched_count", 0),
                "expected_product_count": evidence.get("matched_count"),
                "expected_product_ids": evidence.get("expected_product_ids") or [],
                "expected_product_handles": evidence.get("expected_product_handles") or [],
                "source_strategy_id": record["strategy_id"], "source_strategy_version": record["version"],
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
                    "expected_product_ids": sorted(identity for identity in identities if identity.startswith("gid://shopify/Product/")),
                    "expected_product_handles": sorted({str(product.get("shopify_handle") or "").casefold()
                                                        for product in matches if product.get("shopify_handle")}),
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

    @staticmethod
    def _expected(local: dict) -> tuple[int | None, set[str], set[str]]:
        evidence = local.get("evidence") if isinstance(local.get("evidence"), dict) else {}
        count = local.get("expected_product_count", local.get("estimated_product_count",
                    evidence.get("matched_count")))
        count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
        ids = {str(value) for value in (local.get("expected_product_ids") or
               evidence.get("expected_product_ids") or []) if str(value)}
        handles = {str(value).casefold() for value in (local.get("expected_product_handles") or
                   evidence.get("expected_product_handles") or []) if str(value)}
        return count, ids, handles

    @staticmethod
    def _content_evidence(local: dict, remote: dict) -> dict:
        expected_count, expected_ids, expected_handles = ExistingCollectionReconciliationService._expected(local)
        remote_count = remote.get("products_count")
        remote_count = remote_count if isinstance(remote_count, int) and not isinstance(remote_count, bool) else None
        count_ratio = (remote_count / expected_count) if remote_count is not None and expected_count else None
        complete = remote.get("membership_complete") is True or remote.get("membership_status") == "FULL"
        remote_ids = {str(value) for value in (remote.get("product_ids") or []) if str(value)}
        remote_handles = {str(value).casefold() for value in (remote.get("product_handles") or []) if str(value)}
        expected_set, remote_set, basis = set(), set(), None
        if complete and expected_ids and remote_ids:
            expected_set, remote_set, basis = expected_ids, remote_ids, "PRODUCT_ID"
        elif complete and expected_handles and remote_handles:
            expected_set, remote_set, basis = expected_handles, remote_handles, "PRODUCT_HANDLE"
        metrics = {"expected_count": expected_count, "remote_count": remote_count,
                   "count_ratio": round(count_ratio, 4) if count_ratio is not None else None,
                   "intersection_count": None, "precision": None, "recall": None, "jaccard": None,
                   "membership_basis": basis, "membership_complete": complete,
                   "membership_status": remote.get("membership_status") or ("FULL" if complete else "UNKNOWN")}
        if basis:
            intersection = len(expected_set & remote_set)
            union = len(expected_set | remote_set)
            precision = intersection / len(remote_set) if remote_set else 0.0
            recall = intersection / len(expected_set) if expected_set else 0.0
            jaccard = intersection / union if union else 0.0
            metrics.update(intersection_count=intersection, precision=round(precision, 4),
                           recall=round(recall, 4), jaccard=round(jaccard, 4))
            if recall >= COMPATIBLE_RECALL and precision >= COMPATIBLE_PRECISION and (
                    count_ratio is None or COUNT_RATIO_MIN <= count_ratio <= COUNT_RATIO_MAX):
                status = "VERIFIED_COMPATIBLE"
            elif recall >= COMPATIBLE_RECALL and precision < COMPATIBLE_PRECISION:
                status = "VERIFIED_OVERBROAD"
            elif precision >= COMPATIBLE_PRECISION and recall < COMPATIBLE_RECALL:
                status = "VERIFIED_UNDERCOVERED"
            else:
                status = "COUNT_MISMATCH"
        elif count_ratio is not None and (count_ratio > COUNT_RATIO_MAX or count_ratio < COUNT_RATIO_MIN):
            status = "COUNT_MISMATCH"
        else:
            status = "UNKNOWN"
        return {"content_status": status, "metrics": metrics}

    def fetch_membership(self, store_id: str, collection_id: str, *, cap: int = MEMBERSHIP_SAFE_CAP,
                         page_size: int = MEMBERSHIP_PAGE_SIZE, client=None) -> dict:
        """Fetch bounded Shopify collection membership using queries only."""
        if not re.fullmatch(r"gid://shopify/Collection/\d+", str(collection_id or "")):
            raise ValueError("A valid Shopify collection GID is required")
        cap = max(1, min(int(cap), MEMBERSHIP_SAFE_CAP))
        page_size = max(1, min(int(page_size), MEMBERSHIP_PAGE_SIZE, cap))
        if client is None:
            from .shopify_collections import ShopifyGraphQLClient, get_connection, get_shopify_token
            config = get_connection(str(store_id), db=self.db)
            token, _source = get_shopify_token(str(store_id), db=self.db)
            if not config or not token:
                raise RuntimeError("Shopify read connection is required")
            client = ShopifyGraphQLClient(config["shop_domain"], token, config["api_version"])
        nodes, after, identity, remote_count, precision = [], None, {}, None, None
        while len(nodes) < cap:
            first = min(page_size, cap - len(nodes))
            payload = client.execute(COLLECTION_MEMBERSHIP_QUERY,
                                     {"id": collection_id, "first": first, "after": after})
            collection = payload.get("collection") or {}
            if not collection or collection.get("id") != collection_id:
                raise RuntimeError("Shopify collection identity was not returned")
            identity = {"id": collection.get("id"), "title": collection.get("title"),
                        "handle": collection.get("handle")}
            products_count = collection.get("productsCount") or {}
            remote_count, precision = products_count.get("count"), products_count.get("precision")
            connection = collection.get("products") or {}
            nodes.extend(row for row in (connection.get("nodes") or []) if row.get("id") and row.get("handle"))
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if not after:
                break
        complete = isinstance(remote_count, int) and len(nodes) >= remote_count
        status = "FULL" if complete else "PARTIAL" if nodes else "UNKNOWN"
        return {**identity, "products_count": remote_count, "products_count_precision": precision,
                "product_ids": [row["id"] for row in nodes],
                "product_handles": [row["handle"] for row in nodes],
                "membership_status": status, "membership_complete": complete,
                "membership_fetched_count": len(nodes), "membership_cap": cap,
                "shopify_write_performed": False}

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
            identity_status, content_status, metrics, status = "NO_MATCH", "UNKNOWN", {}, "NO_MATCH"
        elif tied:
            identity_status, content_status, metrics, status = "AMBIGUOUS", "UNKNOWN", {}, "AMBIGUOUS"
        else:
            identity_status = ("EXACT_ID" if top["exact_id"] else "EXACT_HANDLE" if top["exact_handle"] else
                               "TITLE_MATCH" if top["normalized_title_match"] else "SIMILAR_HANDLE")
            content = self._content_evidence(local, top["remote"])
            content_status, metrics = content["content_status"], content["metrics"]
            if content_status != "VERIFIED_COMPATIBLE":
                status = "CONFLICT"
            elif top["exact_id"] or top["exact_handle"]:
                status = "EXACT_MATCH"
            elif top["score"] >= 80:
                status = "HIGH_CONFIDENCE_CANDIDATE"
            else:
                status = "CONFLICT"
        return {"store_id": str(store_id), "collection_key": key, "local": local,
                "status": status, "candidate": top, "candidates": candidates,
                "identity_status": identity_status, "content_status": content_status,
                "content_metrics": metrics,
                "user_confirmation_required": status in {"EXACT_MATCH", "HIGH_CONFIDENCE_CANDIDATE"},
                "remote_write_performed": False}

    def adopt(self, proposal: dict, *, confirmed: bool = False) -> dict:
        if confirmed is not True:
            raise PermissionError("Explicit confirmation is required to link an existing collection")
        if (proposal.get("status") not in {"EXACT_MATCH", "HIGH_CONFIDENCE_CANDIDATE"} or
                proposal.get("content_status") != "VERIFIED_COMPATIBLE" or
                proposal.get("identity_status") in {"AMBIGUOUS", "NO_MATCH", None}):
            raise ValueError("Adoption requires unambiguous identity and verified compatible content")
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
