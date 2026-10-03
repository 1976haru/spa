"""Deterministic, local-only store sourcing plan generation (no execution)."""
from __future__ import annotations

import json
import math
import re
import secrets

from ..db import connect, get_store, init_db, utc_now
from ..intelligence.keyword_engine import KeywordEngine
from ..intelligence.text_features import normalize_keyword

PLANNER_VERSION = "3.0.0"
DEFAULTS = {"max_active_keywords_per_category": 15, "max_pages_per_keyword": 5,
            "stale_pages": 2, "max_unique_candidates_per_keyword": 300}
LIMITS = {"max_active_keywords_per_category": (3, 30), "max_pages_per_keyword": (1, 20),
          "stale_pages": (1, 10), "max_unique_candidates_per_keyword": (1, 5000)}

# Data-driven topic seeds; generic keyword/category grouping remains the fallback.
TOPIC_RULES = (
    ("trunk", "Trunk Storage", ("trunk", "cargo", "boot"), ("organizer", "storage", "cargo", "foldable", "container")),
    ("seat", "Seat Organization", ("seat", "backseat", "headrest", "gap"), ("organizer", "storage", "gap filler", "protector", "tray")),
    ("console", "Console Storage", ("console", "dashboard"), ("organizer", "storage", "tray", "holder", "insert")),
    ("cup", "Cup Holder", ("cup", "beverage"), ("cup holder", "expander", "insert", "coaster")),
    ("visor", "Visor & Documents", ("visor", "glove box", "document", "registration"), ("organizer", "holder", "document holder", "storage")),
    ("clean", "Trash & Cleanup", ("trash", "clean", "waste", "litter"), ("trash can", "cleanup kit", "organizer", "waste bin")),
    ("travel", "Travel Storage", ("travel", "road trip", "vehicle", "car"), ("travel organizer", "storage", "organizer", "accessories")),
)


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_keyword(value)).strip("-")[:80] or "general"


def _dedupe(values):
    output, seen = [], set()
    for value, source in values:
        normalized = normalize_keyword(value)
        if normalized and normalized not in seen:
            output.append((str(value).strip(), source)); seen.add(normalized)
    return output


class CategoryPlanner:
    """Build and persist versioned plan snapshots using only local profile/DB evidence."""

    def __init__(self, db=None):
        self.db = db
        init_db(db)

    def create_plan(self, store_id: str, total_candidate_target: int, *, mode="balanced",
                    detail_ratio: float = 1.0, advanced: dict | None = None) -> dict:
        target = int(total_candidate_target)
        if not 1 <= target <= 50000:
            raise ValueError("Target must be between 1 and 50,000.")
        if mode not in {"fast", "balanced", "deep"}:
            raise ValueError("Mode must be fast, balanced, or deep.")
        ratio = float(detail_ratio)
        if not 0 < ratio <= 1:
            raise ValueError("Detail ratio must be greater than 0 and at most 1.")
        settings = dict(DEFAULTS)
        if mode == "fast":
            settings.update(max_active_keywords_per_category=8, max_pages_per_keyword=3,
                            stale_pages=1, max_unique_candidates_per_keyword=200)
        elif mode == "deep":
            settings.update(max_active_keywords_per_category=20, max_pages_per_keyword=10,
                            stale_pages=3, max_unique_candidates_per_keyword=500)
        settings.update(advanced or {})
        for name, limits in LIMITS.items():
            settings[name] = int(settings[name])
            if not limits[0] <= settings[name] <= limits[1]:
                raise ValueError(f"{name} must be between {limits[0]} and {limits[1]}.")
        profile = get_store(store_id, self.db)
        categories = self._categories(profile, store_id)
        if target < len(categories):
            categories = categories[:target]
        self._allocate_quotas(categories, target)
        now, plan_id = utc_now(), "SAP_" + secrets.token_hex(10)
        with connect(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            version = int(con.execute("SELECT COALESCE(MAX(version),0)+1 FROM store_sourcing_plans WHERE store_id=?", (store_id,)).fetchone()[0])
            con.execute("""INSERT INTO store_sourcing_plans
                (plan_id,store_id,version,name,total_candidate_target,detail_target,detail_ratio,mode,status,planner_version,settings_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,'DRAFT',?,?,?,?)""",
                (plan_id, store_id, version, f"{profile.get('store_name', store_id)} Auto Sourcing v{version}",
                 target, int(math.ceil(target * ratio)), ratio, mode, PLANNER_VERSION,
                 json.dumps(settings, sort_keys=True), now, now))
            for priority, category in enumerate(categories, 1):
                cursor = con.execute("""INSERT INTO store_sourcing_categories
                    (plan_id,category_key,category_name,weight,quota,priority,enabled,source,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,1,?,?,?)""",
                        (plan_id, category["key"], category["name"], category["weight"], category["quota"], priority,
                     category["source"], now, now))
                for keyword in category["keywords"]:
                    con.execute("""INSERT INTO store_sourcing_keywords
                        (category_id,keyword,source,score,enabled,historical_yield,duplicate_rate,price_fit,quality_fit,risk_rate,pages_used,last_run_at,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (cursor.lastrowid, keyword["keyword"], keyword["source"], keyword["score"],
                         int(keyword["score"] >= 0.35 and keyword["risk_rate"] < 0.5
                             and normalize_keyword(keyword["keyword"]) not in {normalize_keyword(x) for x in profile.get("exclude_keywords", [])}),
                         keyword["yield"], keyword["duplicate_rate"],
                         keyword["price_fit"], keyword["quality_fit"], keyword["risk_rate"], keyword["pages_used"],
                         keyword["last_run_at"], now, now))
        return self.get_plan(plan_id)

    def _categories(self, profile: dict, store_id: str) -> list[dict]:
        configured = profile.get("sourcing_categories") or (profile.get("sourcing") or {}).get("categories")
        groups = []
        if configured:
            for item in configured:
                if isinstance(item, str): item = {"name": item}
                name = str(item.get("name") or item.get("category_name") or "").strip()
                if name: groups.append({"name": name, "weight": float(item.get("weight", 1)), "source": "PROFILE"})
        else:
            context = " ".join([str(profile.get("category", "")), str(profile.get("concept", "")),
                                " ".join(map(str, profile.get("include_keywords") or []))]).casefold()
            for key, name, matches, _terms in TOPIC_RULES:
                if any(term in context for term in matches):
                    groups.append({"name": name, "weight": 1.0 + (0.3 if any(t in context for t in matches[:1]) else 0), "source": "LOCAL_RULE"})
            if not groups:
                broad = str(profile.get("category") or profile.get("concept") or profile.get("store_name") or "General Products").strip()
                groups = [{"name": broad, "weight": 1.0, "source": "PROFILE_FALLBACK"}]
        seen = set(); unique = []
        for group in groups:
            key = _key(group["name"])
            base, suffix = key, 2
            while key in seen:
                key = f"{base}-{suffix}"; suffix += 1
            seen.add(key); unique.append({**group, "key": key})
        if not unique:
            unique = [{"name": "General Products", "key": "general-products", "weight": 1.0, "source": "FALLBACK"}]

        recommendations = KeywordEngine(self.db).recommend(store_id, limit=300)
        history = self._history(store_id)
        profile_keywords = []
        for item in (profile.get("sourcing") or {}).get("recipes", []):
            profile_keywords.append((item.get("keyword", "") if isinstance(item, dict) else str(item), "PROFILE_RECIPE"))
        profile_keywords.extend((str(x), "INCLUDE") for x in profile.get("include_keywords", []))
        excluded = {normalize_keyword(x) for x in profile.get("exclude_keywords", [])}
        risk_terms = [normalize_keyword(term) for rule in profile.get("risk_rules", []) for term in rule.get("terms", [])]
        for group in unique:
            seeds = []
            # Profile's own terms remain eligible in broad/fallback and are matched to topical groups.
            for value, source in profile_keywords:
                if (group["source"] in {"PROFILE", "PROFILE_FALLBACK", "FALLBACK"} or group is unique[0]
                        or any(t in normalize_keyword(value) for t in group["name"].casefold().split())):
                    seeds.append((value, source))
            for rec in recommendations:
                value = rec["keyword"]
                if group["source"] in {"PROFILE", "PROFILE_FALLBACK", "FALLBACK"} or any(t in normalize_keyword(value) for t in group["name"].casefold().split()):
                    seeds.append((value, rec.get("source", "KEYWORD_ENGINE")))
            matching_rules = [rule for rule in TOPIC_RULES if rule[1] == group["name"]]
            if matching_rules:
                _, _, _matches, terms = matching_rules[0]
                roots = profile.get("category") or profile.get("concept") or ""
                seeds.extend((f"{roots} {term}".strip(), "LOCAL_TEMPLATE") for term in terms)
                seeds.extend((f"{term} {product}".strip(), "LOCAL_TEMPLATE") for term in terms for product in ("organizer", "storage", "holder"))
                modifiers = ("compact", "large", "foldable", "portable", "adjustable", "waterproof",
                             "heavy duty", "with pockets", "for SUV", "for truck", "for family", "with lid")
                seeds.extend((f"{roots} {term} {modifier}".strip(), "LOCAL_TEMPLATE")
                             for term in terms for modifier in modifiers)
            base_terms = ("organizer", "storage", "accessories", "holder", "travel", "container", "rack", "basket", "tray", "bin", "bag", "box")
            modifiers = ("compact", "large", "foldable", "portable", "adjustable", "waterproof",
                         "heavy duty", "with pockets", "for home", "for travel", "with lid", "space saving")
            seeds.extend((f"{group['name']} {term} {modifier}", "LOCAL_TEMPLATE")
                         for term in base_terms for modifier in modifiers)
            scored = []
            for keyword, source in _dedupe(seeds):
                normalized = normalize_keyword(keyword)
                if normalized in excluded or any(term and term in normalized for term in risk_terms):
                    # Keep risk-related suggestions in the pool but disabled at low priority.
                    risk = 1.0
                else:
                    risk = 0.0
                hist = history.get(normalized, {})
                evidence = next((x for x in recommendations if normalize_keyword(x["keyword"]) == normalized), {})
                semantic = float(evidence.get("semantic_fit", 0.45))
                y = float(hist.get("yield", 0))
                yield_fit = min(1.0, math.log1p(y) / math.log(51)) if y else float(evidence.get("candidate_yield", 0)) / 50
                duplicate = float(hist.get("duplicate_rate", 0))
                price = float(hist.get("price_fit", evidence.get("price_fit", 0.5)))
                quality = float(hist.get("quality_fit", evidence.get("quality_fit", 0.5)))
                risk = max(risk, float(hist.get("risk_rate", evidence.get("risk_rate", 0))))
                freshness = 0.35 if hist.get("last_run_at") else 0.7
                score = max(0.0, min(1.0, .32*semantic + .20*yield_fit + .12*(1-duplicate) + .12*price + .12*quality + .08*(1-risk) + .04*freshness))
                scored.append({"keyword": keyword, "source": source, "score": round(score, 4), "yield": y or None,
                    "duplicate_rate": duplicate if y else None, "price_fit": price if y else None,
                    "quality_fit": quality if y else None, "risk_rate": risk,
                    "pages_used": int(hist.get("pages_used", 0)), "last_run_at": hist.get("last_run_at")})
            scored.sort(key=lambda x: (-x["score"], normalize_keyword(x["keyword"])))
            group["keywords"] = scored
        return unique

    @staticmethod
    def _allocate_quotas(categories: list[dict], target: int) -> None:
        weights = [max(0.05, float(item["weight"])) for item in categories]
        minimum = 1 if target >= len(categories) else 0
        remaining = target - minimum * len(categories)
        shares = [remaining * weight / sum(weights) for weight in weights]
        base = [math.floor(value) + minimum for value in shares]
        for index in sorted(range(len(shares)), key=lambda i: (-(shares[i] % 1), i))[:target - sum(base)]:
            base[index] += 1
        for item, quota in zip(categories, base): item["quota"] = quota

    def _history(self, store_id: str) -> dict:
        with connect(self.db) as con:
            rows = con.execute("""SELECT keyword,candidate_yield,price_fit,quality_fit,risk_rate,master_duplicate_rate,checked_at
                FROM keyword_validation_results WHERE store_id=? ORDER BY id""", (store_id,)).fetchall()
        result = {}
        for row in rows:
            result[normalize_keyword(row["keyword"])] = {"yield": row["candidate_yield"], "price_fit": row["price_fit"],
                "quality_fit": row["quality_fit"], "risk_rate": row["risk_rate"], "duplicate_rate": row["master_duplicate_rate"],
                "pages_used": 0, "last_run_at": row["checked_at"]}
        return result

    def get_plan(self, plan_id: str) -> dict:
        with connect(self.db) as con:
            plan = con.execute("SELECT * FROM store_sourcing_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if not plan: raise KeyError(plan_id)
            result = dict(plan); result["settings"] = json.loads(result.pop("settings_json"))
            result["categories"] = []
            for row in con.execute("SELECT * FROM store_sourcing_categories WHERE plan_id=? ORDER BY priority", (plan_id,)):
                category = dict(row)
                keywords = [dict(keyword) for keyword in con.execute("SELECT * FROM store_sourcing_keywords WHERE category_id=? ORDER BY score DESC,keyword COLLATE NOCASE", (row["id"],))]
                active = result["settings"]["max_active_keywords_per_category"]
                for index, keyword in enumerate(keywords): keyword["active_by_default"] = bool(keyword["enabled"] and index < active)
                category["keywords"] = keywords
                result["categories"].append(category)
        result["total_quota"] = sum(c["quota"] for c in result["categories"])
        result["keyword_pool_total"] = sum(len(c["keywords"]) for c in result["categories"])
        result["active_keyword_count"] = sum(k["active_by_default"] for c in result["categories"] for k in c["keywords"])
        return result
