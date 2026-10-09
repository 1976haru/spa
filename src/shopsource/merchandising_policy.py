"""Versioned, approval-gated store merchandising policy storage.

This local-only service owns policy drafts and approvals. It has no Shopify
client and never changes product or store JSON records.
"""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

from .db import connect, get_store, init_db

PURPOSE_CATEGORY_SHORTCUTS = "CATEGORY_SHORTCUTS"
MAX_POLICY_LIST_ITEMS = 30
MAX_POLICY_TEXT_LENGTH = 120
MAX_POLICY_JSON_BYTES = 16_000
_POLICY_LIST_FIELDS = (
    "exclude_keywords", "exclude_phrases", "exclude_title_patterns",
    "exclude_decision_statuses",
)
_UNSAFE_REGEX = re.compile(r"\\[1-9]|\(\?|\)[+*{]|\([^)]*[+*{][^)]*\)[+*{]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def validate_policy(policy: dict) -> dict:
    if not isinstance(policy, dict):
        raise ValueError("Policy must be an object")
    normalized = {}
    for field in _POLICY_LIST_FIELDS:
        raw = policy.get(field, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple, set)):
            raise ValueError(f"{field} must be a list of strings")
        if len(raw) > MAX_POLICY_LIST_ITEMS:
            raise ValueError(f"{field} exceeds the maximum of {MAX_POLICY_LIST_ITEMS} entries")
        seen, values = set(), []
        for value in raw:
            if not isinstance(value, str):
                raise ValueError(f"{field} entries must be strings")
            text = _normalize_text(value)
            if not text:
                continue
            if len(text) > MAX_POLICY_TEXT_LENGTH:
                raise ValueError(f"{field} entries may not exceed {MAX_POLICY_TEXT_LENGTH} characters")
            key = text.casefold()
            if key in seen:
                continue
            if field == "exclude_title_patterns":
                if _UNSAFE_REGEX.search(text):
                    raise ValueError("Title pattern uses a disallowed regex construct")
                quantifiers, escaped = 0, False
                for character in text:
                    if escaped:
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character in "*+?{":
                        quantifiers += 1
                if quantifiers > 8:
                    raise ValueError("Title pattern has too many repetition operators")
                try:
                    re.compile(text, re.IGNORECASE)
                except re.error as exc:
                    raise ValueError("Title pattern is invalid") from exc
            seen.add(key)
            values.append(text.upper() if field == "exclude_decision_statuses" else text)
        normalized[field] = values
    notes = _normalize_text(policy.get("notes", ""))
    if len(notes) > 2_000:
        raise ValueError("notes may not exceed 2000 characters")
    normalized["notes"] = notes
    if len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")) > MAX_POLICY_JSON_BYTES:
        raise ValueError("Policy is too large")
    return normalized


def match_policy_exclusion(product: dict, policy: dict) -> str | None:
    """Return deterministic reason; keywords use case-folded token/phrase containment."""
    title = _normalize_text(product.get("title", "")).casefold()
    tags = product.get("tags") or []
    if not isinstance(tags, (list, tuple, set)):
        tags = [tags]
    searchable = _normalize_text(" ".join(str(product.get(key) or "") for key in (
        "title", "product_type", "category", "category_key", "collection_key",
    )) + " " + " ".join(map(str, tags))).casefold()
    for keyword in policy.get("exclude_keywords", []):
        needle = _normalize_text(keyword).casefold()
        if needle and needle in searchable:
            return f"MERCH_POLICY_KEYWORD:{keyword}"
    for phrase in policy.get("exclude_phrases", []):
        needle = _normalize_text(phrase).casefold()
        if needle and needle in searchable:
            return f"MERCH_POLICY_PHRASE:{phrase}"
    for index, pattern in enumerate(policy.get("exclude_title_patterns", []), start=1):
        if re.search(pattern, title, re.IGNORECASE):
            return f"MERCH_POLICY_PATTERN:{index}"
    statuses = {str(status).strip().upper() for status in policy.get("exclude_decision_statuses", [])}
    for field in ("final_status", "decision_status", "risk_status"):
        status = str(product.get(field) or "").strip().upper()
        if status and status in statuses:
            return f"MERCH_POLICY_DECISION_STATUS:{status}"
    return None


class StoreMerchandisingPolicyService:
    """Versioned local policy repository. Drafts have no effect until approved."""

    def __init__(self, db=None):
        self.db = db
        init_db(db)
        with connect(db) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS store_merchandising_policies (
                policy_id TEXT PRIMARY KEY,
                store_id TEXT NOT NULL,
                purpose TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                status TEXT NOT NULL CHECK(status IN ('DRAFT','APPROVED','SUPERSEDED')),
                policy_json TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('MANUAL','IMPORTED_PROFILE','SUGGESTED')),
                created_at TEXT NOT NULL,
                approved_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(store_id,purpose,version)
            )""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_merch_policy_store_purpose ON store_merchandising_policies(store_id,purpose,status,version DESC)")

    @staticmethod
    def _row(row) -> dict | None:
        if not row:
            return None
        result = dict(row)
        result["policy"] = json.loads(result.pop("policy_json"))
        return result

    def latest(self, store_id: str, purpose: str = PURPOSE_CATEGORY_SHORTCUTS,
               include_draft: bool = False) -> dict | None:
        statuses = ("DRAFT", "APPROVED") if include_draft else ("APPROVED",)
        marks = ",".join("?" for _ in statuses)
        with connect(self.db) as con:
            row = con.execute(f"""SELECT * FROM store_merchandising_policies
                WHERE store_id=? AND purpose=? AND status IN ({marks})
                ORDER BY version DESC,created_at DESC LIMIT 1""",
                (str(store_id), purpose, *statuses)).fetchone()
        return self._row(row)

    def effective_policy(self, store_id: str, purpose: str = PURPOSE_CATEGORY_SHORTCUTS) -> dict | None:
        with connect(self.db) as con:
            row = con.execute("""SELECT * FROM store_merchandising_policies
                WHERE store_id=? AND purpose=? AND status='APPROVED'
                ORDER BY version DESC,created_at DESC LIMIT 1""",
                (str(store_id), purpose)).fetchone()
        return self._row(row)

    def create_draft(self, store_id: str, purpose: str, policy: dict,
                     source: str = "MANUAL") -> dict:
        store_id, purpose = str(store_id).strip(), _normalize_text(purpose).upper()
        if not store_id or store_id.casefold() in {"none", "null", "unknown"}:
            raise ValueError("A real store ID is required")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", purpose):
            raise ValueError("Purpose must be a valid named policy scope")
        source = str(source).upper()
        if source not in {"MANUAL", "IMPORTED_PROFILE", "SUGGESTED"}:
            raise ValueError("Unsupported policy source")
        normalized = validate_policy(policy)
        now = _now()
        policy_id = "MP_" + uuid.uuid4().hex
        with connect(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT COALESCE(MAX(version),0)+1 FROM store_merchandising_policies WHERE store_id=? AND purpose=?",
                              (store_id, purpose)).fetchone()
            version = int(row[0])
            con.execute("""INSERT INTO store_merchandising_policies
                (policy_id,store_id,purpose,version,status,policy_json,source,created_at,approved_at,updated_at)
                VALUES(?,?,?,?,'DRAFT',?,?,?,NULL,?)""",
                (policy_id, store_id, purpose, version,
                 json.dumps(normalized, ensure_ascii=False, sort_keys=True), source, now, now))
            saved = con.execute("SELECT * FROM store_merchandising_policies WHERE policy_id=?", (policy_id,)).fetchone()
        return self._row(saved)

    def approve(self, policy_id: str, confirmed: bool = False) -> dict:
        if confirmed is not True:
            raise PermissionError("Explicit confirmation is required to approve a policy")
        now = _now()
        with connect(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            draft = con.execute("SELECT * FROM store_merchandising_policies WHERE policy_id=?", (policy_id,)).fetchone()
            if not draft:
                raise KeyError("Policy draft not found")
            if draft["status"] == "APPROVED":
                return self._row(draft)
            if draft["status"] != "DRAFT":
                raise ValueError("Only a draft policy can be approved")
            con.execute("""UPDATE store_merchandising_policies SET status='SUPERSEDED',updated_at=?
                WHERE store_id=? AND purpose=? AND status='APPROVED'""",
                (now, draft["store_id"], draft["purpose"]))
            con.execute("UPDATE store_merchandising_policies SET status='APPROVED',approved_at=?,updated_at=? WHERE policy_id=?",
                        (now, now, policy_id))
            approved = con.execute("SELECT * FROM store_merchandising_policies WHERE policy_id=?", (policy_id,)).fetchone()
        return self._row(approved)

    def supersede(self, policy_id: str, confirmed: bool = False) -> dict:
        if confirmed is not True:
            raise PermissionError("Explicit confirmation is required to supersede a policy")
        with connect(self.db) as con:
            cursor = con.execute("UPDATE store_merchandising_policies SET status='SUPERSEDED',updated_at=? WHERE policy_id=? AND status='APPROVED'",
                                 (_now(), policy_id))
            if cursor.rowcount != 1:
                raise ValueError("Only an active approved policy can be superseded")
            row = con.execute("SELECT * FROM store_merchandising_policies WHERE policy_id=?", (policy_id,)).fetchone()
        return self._row(row)

    def suggest_draft(self, store_id: str, purpose: str = PURPOSE_CATEGORY_SHORTCUTS) -> dict:
        """Prefill only explicit Store Profile rules; insufficient evidence means an empty draft."""
        try:
            profile = get_store(str(store_id), self.db)
        except KeyError:
            profile = {}
        def strings(value):
            if isinstance(value, str):
                return [value]
            return [item for item in value if isinstance(item, str)] if isinstance(value, (list, tuple, set)) else []
        policy = {
            "exclude_keywords": [*strings(profile.get("exclude_keywords")),
                                 *strings(profile.get("category_shortcut_exclude_keywords"))],
            "exclude_phrases": strings(profile.get("merchandising_exclusions")),
            "exclude_title_patterns": [], "exclude_decision_statuses": [],
            "notes": "Suggested only from explicit Store Profile exclusions; review before approval.",
        }
        for rule in profile.get("risk_rules", []) or []:
            if isinstance(rule, dict) and str(rule.get("status") or "").upper() in {"REVIEW", "RESTRICTED"}:
                policy["exclude_keywords"].extend(strings(rule.get("terms")))
                policy["exclude_decision_statuses"].append(str(rule["status"]).upper())
        return self.create_draft(store_id, purpose, policy, source="SUGGESTED")

    def preview(self, store_id: str, policy: dict, purpose: str = PURPOSE_CATEGORY_SHORTCUTS,
                sample_limit: int = 8) -> dict:
        """Evaluate a draft against cached product evidence only; performs no remote reads."""
        from .category_shortcut_readiness import _eligible_products, _json
        normalized = validate_policy(policy)
        with connect(self.db) as con:
            row = con.execute("SELECT candidates_json FROM homepage_featured_product_remote_cache WHERE store_id=?",
                              (str(store_id),)).fetchone()
        products = _json(row[0], []) if row else []
        try:
            profile = get_store(str(store_id), self.db)
        except KeyError:
            profile = {}
        baseline, _ = _eligible_products(products, profile)
        excluded = []
        for product in baseline:
            reason = match_policy_exclusion(product, normalized)
            if reason:
                excluded.append({"title": _normalize_text(product.get("title"))[:240], "reason": reason})
        return {"store_id": str(store_id), "purpose": purpose,
                "eligible_product_count": len(baseline), "excluded_count": len(excluded),
                "sample_excluded": excluded[:max(0, min(int(sample_limit), 20))],
                "exclusion_reason_counts": {reason: sum(item["reason"] == reason for item in excluded)
                    for reason in sorted({item["reason"] for item in excluded})}}
