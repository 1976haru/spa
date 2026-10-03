"""Local-only, versioned Shopify collection planning and preview.

This module plans collection rules from ShopSource data. It deliberately has no
Shopify client and never performs remote writes.
"""
from __future__ import annotations

import json
import html
import math
import re
import secrets
from collections import Counter, defaultdict
from pathlib import Path

from .db import connect, get_store, init_db, utc_now
from .paths import EXPORT_DIR
from .sourcing.planner import CategoryPlanner

PLANNER_VERSION = "3.2.0"
MAX_COLLECTIONS = {"small": 6, "normal": 10, "broad": 15}
SINGLE_TOPIC_RULES = {"seat", "console", "cup", "visor"}
GENERIC = {
    "a", "an", "and", "for", "with", "the", "to", "of", "in", "on", "by",
    "car", "cars", "auto", "automotive", "vehicle", "vehicles", "product", "products",
    "item", "items", "accessory", "accessories", "storage", "organizer", "organizers",
    "organization", "organizing", "collection", "best", "new", "portable", "premium",
    "quality", "travel", "home", "use", "gear", "supply", "supplies",
}
SUPPORTED_FIELDS = {"TITLE", "PRODUCT_TYPE", "TAG", "VENDOR", "PRICE", "METAFIELD"}
SUPPORTED_RELATIONS = {"EQUALS", "NOT_EQUALS", "CONTAINS", "NOT_CONTAINS", "STARTS_WITH",
                       "ENDS_WITH", "GREATER_THAN", "LESS_THAN"}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")[:80] or "collection"


def _words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value).casefold())


def _human_title(category_name: str) -> str:
    """Turn planner labels into customer-facing labels without store-specific rules."""
    words = str(category_name or "Products").strip().split()
    normalized = " ".join(words).casefold()
    if normalized.endswith("console storage"):
        return " ".join(words).title()
    replacements = {
        "storage": "Organizers", "organization": "Organizers", "organizing": "Organizers",
        "cup holder": "Cup Holders", "cup holders": "Cup Holders",
        "document": "Documents", "documents": "Documents",
    }
    for source, destination in replacements.items():
        if normalized.endswith(source):
            stem = " ".join(words)[:-len(source)].strip()
            return f"{stem} {destination}".strip().title()
    return " ".join(words).title()


def _category_phrases(name: str, keywords: list[dict]) -> list[str]:
    candidates: list[tuple[float, str]] = []
    name_words = _words(name)
    distinctive_name = [word for word in name_words if word not in GENERIC]
    if distinctive_name and not (len(distinctive_name) == 1 and distinctive_name[0] in SINGLE_TOPIC_RULES):
        # A distinctive category token is a useful title fallback (e.g. “Trunk”).
        candidates.append((2.0 + len(distinctive_name) * .1, " ".join(distinctive_name)))
    for row in sorted(keywords, key=lambda item: (-float(item.get("score") or 0), str(item["keyword"]).casefold())):
        words = _words(row["keyword"])
        start = next((index for index, word in enumerate(words) if word not in GENERIC), None)
        if start is None:
            continue
        # Preserve the useful product noun following a distinctive topic term.
        phrase = " ".join(words[start:start + 4])
        if len(_words(phrase)) == 1 and phrase.casefold() in GENERIC:
            continue
        score = 1.0 + float(row.get("score") or 0) + min(0.3, len(_words(phrase)) * .05)
        candidates.append((score, phrase))
    seen: set[str] = set()
    result = []
    for _score, phrase in sorted(candidates, key=lambda item: (-item[0], -len(item[1]), item[1])):
        phrase = " ".join(phrase.split())
        key = phrase.casefold()
        if key in seen or len(_words(phrase)) > 5:
            continue
        if len(_words(phrase)) == 1 and key in GENERIC:
            continue
        seen.add(key)
        result.append(phrase)
        if len(result) == 4:
            break
    return result


def _value(product: dict, field: str):
    return {
        "TITLE": product.get("title") or "",
        "PRODUCT_TYPE": product.get("category") or "",
        "TAG": product.get("tags") or [],
        "VENDOR": product.get("brand") or "",
        "PRICE": product.get("price"),
        "METAFIELD": product.get("metafields") or {},
    }[field]


def condition_matches(product: dict, condition: dict) -> bool:
    field, relation, expected = condition["field"], condition["relation"], condition["value"]
    if field not in SUPPORTED_FIELDS or relation not in SUPPORTED_RELATIONS:
        return False
    raw = _value(product, field)
    if field == "TAG":
        values = raw if isinstance(raw, list) else [raw]
        actual = [str(item).casefold() for item in values]
        target = str(expected).casefold()
        if relation == "CONTAINS": return any(target in item for item in actual)
        if relation == "NOT_CONTAINS": return all(target not in item for item in actual)
        if relation == "EQUALS": return target in actual
        if relation == "NOT_EQUALS": return target not in actual
        return any(_string_relation(item, relation, target) for item in actual)
    if field == "METAFIELD":
        actual = " ".join(str(item) for item in raw.values()).casefold()
        target = str(expected).casefold()
    elif field == "PRICE":
        try:
            actual_number, target_number = float(raw), float(expected)
        except (TypeError, ValueError):
            return False
        if relation == "GREATER_THAN": return actual_number > target_number
        if relation == "LESS_THAN": return actual_number < target_number
        if relation == "EQUALS": return actual_number == target_number
        if relation == "NOT_EQUALS": return actual_number != target_number
        actual, target = str(actual_number), str(target_number)
    else:
        actual, target = str(raw).casefold(), str(expected).casefold()
    return _string_relation(actual, relation, target)


def _string_relation(actual: str, relation: str, target: str) -> bool:
    if relation == "EQUALS": return actual == target
    if relation == "NOT_EQUALS": return actual != target
    if relation == "CONTAINS": return target in actual
    if relation == "NOT_CONTAINS": return target not in actual
    if relation == "STARTS_WITH": return actual.startswith(target)
    if relation == "ENDS_WITH": return actual.endswith(target)
    return False


class CollectionPlanner:
    """Create local collection design revisions from Store Profile, plans, and MASTER."""

    def __init__(self, db=None):
        self.db = db
        init_db(db)

    def create_plan(self, store_id: str, *, sourcing_plan_id: str | None = None,
                    settings: dict | None = None) -> dict:
        profile = get_store(store_id, self.db)
        catalog = self._catalog(store_id)
        source_plan = self._sourcing_plan(store_id, sourcing_plan_id)
        if source_plan is None:
            # A plan snapshot is local planning only; it does not start sourcing.
            target = min(50000, max(100, len(catalog), len(profile.get("sourcing_categories") or []) * 20))
            source_plan = CategoryPlanner(self.db).create_plan(store_id, target)
        options = {"desired_collection_count": None, "rule_strategy": "TITLE_FALLBACK",
                   "min_products": 3, "max_overlap_warning": .8, "language": "en",
                   "include_empty": False}
        options.update(settings or {})
        self._validate_settings(options)
        categories = self._source_categories(source_plan)
        desired = int(options["desired_collection_count"] or self._desired_count(len(catalog), len(categories)))
        desired = max(1, min(desired, int(MAX_COLLECTIONS["broad"]), len(categories) or 1))
        candidates = []
        skipped = []
        for category in categories:
            phrases = _category_phrases(category["category_name"], category["keywords"])
            if not phrases:
                skipped.append({"category": category["category_name"], "reason": "No safe distinctive condition could be derived."})
                continue
            conditions = self._conditions(store_id, category, phrases, options["rule_strategy"])
            matches = [product for product in catalog if any(condition_matches(product, condition) for condition in conditions if condition["field"] != "TAG" or not condition["value"].startswith("shopsource-"))]
            # Generated ShopSource tags are future-compatible suggestions. Preview uses the title fallback
            # conditions alongside them, so no product/tag writes are required to estimate current results.
            title_conditions = [condition for condition in conditions if condition["field"] == "TITLE"]
            if title_conditions:
                matches = [product for product in catalog if any(condition_matches(product, condition) for condition in title_conditions)]
            specific_asins = {product["asin"] for product in catalog if any(
                len(_words(condition["value"])) >= 2 and condition_matches(product, condition)
                for condition in title_conditions
            )}
            count = len(matches)
            if count == 0 and not options["include_empty"]:
                skipped.append({"category": category["category_name"], "reason": "ZERO_MATCHES"})
                continue
            if 0 < count < int(options["min_products"]) and not options["include_empty"]:
                skipped.append({"category": category["category_name"], "reason": f"BELOW_MIN_PRODUCTS:{count}"})
                continue
            title = _human_title(category["category_name"])
            collection_key = _slug(category["category_key"] or title)
            warning = self._condition_warnings(conditions, count, len(catalog))
            specificity = round(len(specific_asins.intersection(product["asin"] for product in matches)) / count, 3) if count else 0.0
            if count and specificity < .5:
                warning.append("LOW_TITLE_RULE_SPECIFICITY")
            status_breakdown = dict(Counter(str(product.get("final_status") or "UNCLASSIFIED") for product in matches))
            candidates.append({
                "collection_key": collection_key, "source_category_id": category["id"],
                "source_category": category["category_name"], "title": title,
                "description_html": self._description(profile, category["category_name"], title),
                "handle": _slug(f"{profile['store_name']} {title}"), "priority": int(category["priority"]),
                "enabled": True, "match_mode": "ANY", "rule_strategy": self._actual_strategy(conditions),
                "conditions": conditions, "estimated_product_count": count,
                "title_rule_specificity_estimate": specificity,
                "sample_products": [{"asin": product["asin"], "title": product["title"],
                                     "brand": product["brand"], "price": product["price"],
                                     "final_status": product.get("final_status") or "UNCLASSIFIED"}
                                    for product in matches[:5]],
                "store_status_breakdown": status_breakdown,
                "matched_asins": {product["asin"] for product in matches}, "warnings": warning,
                "image_prompt": self._image_prompt(profile, category["category_name"], title),
                "image_alt_text": f"{title} for {profile['store_name']} — clean, practical automotive organization.",
                "shopify_collection_id": None, "shopify_sync_status": "NOT_SYNCED",
            })
        # Prefer coverage and category importance; stable category priority breaks ties.
        candidates.sort(key=lambda row: (-row["estimated_product_count"], row["priority"], row["title"].casefold()))
        candidates = candidates[:desired]
        overlap_warnings = self._overlap_warnings(candidates, float(options["max_overlap_warning"]))
        by_key = {row["collection_key"]: row for row in candidates}
        for key, warning in overlap_warnings.items():
            by_key[key]["warnings"].extend(warning)
        matched = set().union(*(row["matched_asins"] for row in candidates)) if candidates else set()
        total = len(catalog)
        global_warnings = list(skipped)
        if total and len(matched) < total:
            global_warnings.append({"reason": "UNMATCHED_PRODUCTS", "count": total - len(matched),
                                    "percentage": round((total - len(matched)) * 100 / total, 1)})
        for row in candidates:
            if total and row["estimated_product_count"] > total * .7:
                row["warnings"].append("BROAD_COLLECTION_OVER_70_PERCENT")
        previous = self._latest_plan(store_id)
        diff = self._diff(previous, candidates)
        now, plan_id = utc_now(), "SCP_" + secrets.token_hex(10)
        with connect(self.db) as con:
            version = int(con.execute("SELECT COALESCE(MAX(version),0)+1 FROM store_collection_plans WHERE store_id=?", (store_id,)).fetchone()[0])
            con.execute("""INSERT INTO store_collection_plans
                (plan_id,store_id,sourcing_plan_id,version,planner_version,status,master_product_count,included_product_count,
                 unmatched_product_count,unmatched_percentage,settings_json,diff_json,created_at,updated_at)
                VALUES(?,?,?,?,?,'DRAFT',?,?,?,?,?,?,?,?)""",
                (plan_id, store_id, source_plan["plan_id"], version, PLANNER_VERSION,
                 total, len(matched), max(0, total-len(matched)), round((total-len(matched))*100/total, 1) if total else 0.0,
                 json.dumps(options, sort_keys=True), json.dumps(diff, ensure_ascii=False, sort_keys=True), now, now))
            for row in candidates:
                cursor = con.execute("""INSERT INTO store_collection_definitions
                    (plan_id,collection_key,source_category_id,title,description_html,handle,priority,enabled,match_mode,
                     rule_strategy,estimated_product_count,title_rule_specificity_estimate,sample_products_json,store_status_breakdown_json,warning_json,image_prompt,image_alt_text,
                     shopify_collection_id,shopify_sync_status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'NOT_SYNCED',?,?)""",
                    (plan_id, row["collection_key"], row["source_category_id"], row["title"], row["description_html"],
                     row["handle"], row["priority"], 1, row["match_mode"], row["rule_strategy"],
                     row["estimated_product_count"], row["title_rule_specificity_estimate"], json.dumps(row["sample_products"], ensure_ascii=False),
                     json.dumps(row["store_status_breakdown"], ensure_ascii=False),
                     json.dumps(row["warnings"], ensure_ascii=False), row["image_prompt"], row["image_alt_text"], now, now))
                row["id"] = cursor.lastrowid
                for priority, condition in enumerate(row["conditions"]):
                    con.execute("""INSERT INTO store_collection_conditions
                        (collection_definition_id,field,relation,value,group_operator,priority) VALUES(?,?,?,?,?,?)""",
                        (cursor.lastrowid, condition["field"], condition["relation"], condition["value"],
                         condition["group_operator"], priority))
        for row in candidates:
            row.pop("matched_asins", None)
        return {"plan_id": plan_id, "store_id": store_id, "store_name": profile["store_name"],
                "sourcing_plan_id": source_plan["plan_id"], "version": version, "planner_version": PLANNER_VERSION,
                "status": "DRAFT", "settings": options, "collections": candidates,
                "collection_count": len(candidates), "master_product_count": total,
                "included_product_count": len(matched), "unmatched_product_count": max(0, total-len(matched)),
                "unmatched_percentage": round((total-len(matched))*100/total, 1) if total else 0.0,
                "warnings": global_warnings, "diff": diff}

    def get_plan(self, plan_id: str) -> dict:
        with connect(self.db) as con:
            plan = con.execute("SELECT * FROM store_collection_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if not plan: raise KeyError(plan_id)
            result = dict(plan)
            result["settings"] = json.loads(result.pop("settings_json"))
            result["diff"] = json.loads(result.pop("diff_json"))
            result["collections"] = []
            for definition in con.execute("""SELECT d.*,c.category_name AS source_category
                FROM store_collection_definitions d LEFT JOIN store_sourcing_categories c ON c.id=d.source_category_id
                WHERE d.plan_id=? ORDER BY d.priority,d.id""", (plan_id,)):
                row = dict(definition)
                row["sample_products"] = json.loads(row.pop("sample_products_json"))
                row["store_status_breakdown"] = json.loads(row.pop("store_status_breakdown_json"))
                row["warnings"] = json.loads(row.pop("warning_json"))
                row["conditions"] = [dict(item) for item in con.execute("SELECT field,relation,value,group_operator,priority FROM store_collection_conditions WHERE collection_definition_id=? ORDER BY priority,id", (row["id"],))]
                result["collections"].append(row)
            result["collection_count"] = len(result["collections"])
            return result

    def export(self, plan_id: str, out_root: str | Path | None = None) -> dict:
        plan = self.get_plan(plan_id)
        root = Path(out_root) if out_root else EXPORT_DIR / "collection_plans"
        folder = root / _slug(plan["store_id"]) / plan_id
        folder.mkdir(parents=True, exist_ok=True)
        payload = {key: plan[key] for key in ("plan_id", "store_id", "sourcing_plan_id", "version", "planner_version", "status", "settings", "diff", "collections")}
        (folder / "collection_plan.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [f"# Collection plan — {plan['store_id']}", "", f"- Plan: {plan_id}", f"- Version: {plan['version']}",
                 f"- Source plan: {plan.get('sourcing_plan_id')}", f"- Planner: {plan['planner_version']}", ""]
        for row in plan["collections"]:
            lines += [f"## {row['priority']}. {row['title']}", "", row["description_html"], "",
                      f"- Handle suggestion: `{row['handle']}`", f"- Source category: {row.get('source_category') or row.get('source_category_id')}",
                      f"- Estimated products: {row['estimated_product_count']}", f"- Rule strategy: {row['rule_strategy']}",
                      f"- Title-rule specificity estimate: {row.get('title_rule_specificity_estimate', 0):.0%} (multi-word phrase heuristic)",
                      "- Conditions:"]
            lines.extend(f"  - {condition['field']} {condition['relation']} {condition['value']}" for condition in row["conditions"])
            lines += [f"- Image prompt: {row['image_prompt']}", f"- Image alt text: {row['image_alt_text']}", ""]
        (folder / "collection_plan.md").write_text("\n".join(lines), encoding="utf-8")
        return {"folder": str(folder), "json": str(folder / "collection_plan.json"), "markdown": str(folder / "collection_plan.md")}

    def _sourcing_plan(self, store_id: str, sourcing_plan_id: str | None) -> dict | None:
        from .sourcing.planner import CategoryPlanner
        with connect(self.db) as con:
            if sourcing_plan_id:
                row = con.execute("SELECT plan_id FROM store_sourcing_plans WHERE plan_id=? AND store_id=?", (sourcing_plan_id, store_id)).fetchone()
            else:
                row = con.execute("SELECT plan_id FROM store_sourcing_plans WHERE store_id=? ORDER BY version DESC LIMIT 1", (store_id,)).fetchone()
        return CategoryPlanner(self.db).get_plan(row["plan_id"]) if row else None

    @staticmethod
    def _source_categories(plan: dict) -> list[dict]:
        return [{**category, "keywords": [keyword for keyword in category["keywords"]
                                          if keyword["enabled"] and keyword.get("active_by_default")]}
                for category in plan["categories"] if category["enabled"]]

    def _catalog(self, store_id: str) -> list[dict]:
        with connect(self.db) as con:
            rows = con.execute("""SELECT p.asin,p.title,p.brand,p.price,p.category,p.tags_json,p.archived,
                d.final_status FROM products p LEFT JOIN store_product_decisions d
                ON d.product_id=p.id AND d.store_id=? WHERE p.archived=0 ORDER BY p.id""", (store_id,)).fetchall()
        catalog = []
        for row in rows:
            try: tags = json.loads(row["tags_json"] or "[]")
            except (json.JSONDecodeError, TypeError): tags = []
            catalog.append({**dict(row), "tags": tags if isinstance(tags, list) else [], "metafields": {}})
        return catalog

    @staticmethod
    def _desired_count(product_count: int, category_count: int) -> int:
        if product_count < 100: desired = min(6, max(4, category_count))
        elif product_count < 1000: desired = min(10, max(6, category_count))
        else: desired = min(15, max(8, category_count))
        return max(1, min(category_count or 1, desired))

    @staticmethod
    def _validate_settings(settings: dict) -> None:
        if settings["rule_strategy"] not in {"TITLE_FALLBACK", "TAG_PREFERRED", "MIXED"}:
            raise ValueError("rule_strategy must be TITLE_FALLBACK, TAG_PREFERRED, or MIXED.")
        settings["min_products"] = max(0, min(100000, int(settings["min_products"])))
        settings["max_overlap_warning"] = max(0.0, min(1.0, float(settings["max_overlap_warning"])))
        if settings["desired_collection_count"] is not None:
            settings["desired_collection_count"] = max(1, min(15, int(settings["desired_collection_count"])))

    @staticmethod
    def _conditions(store_id: str, category: dict, phrases: list[str], strategy: str) -> list[dict]:
        conditions = []
        if strategy in {"TAG_PREFERRED", "MIXED"}:
            conditions.append({"field": "TAG", "relation": "EQUALS",
                               "value": f"shopsource-{_slug(store_id)}-{_slug(category['category_key'])}",
                               "group_operator": "OR"})
        if strategy in {"TITLE_FALLBACK", "TAG_PREFERRED", "MIXED"}:
            for phrase in phrases:
                words = _words(phrase)
                if len(words) == 1 and (words[0] in GENERIC or len(words[0]) < 4):
                    continue
                conditions.append({"field": "TITLE", "relation": "CONTAINS", "value": phrase,
                                   "group_operator": "OR"})
        return conditions

    @staticmethod
    def _actual_strategy(conditions: list[dict]) -> str:
        fields = {row["field"] for row in conditions}
        if fields == {"TAG"}: return "TAG_PREFERRED"
        if "TAG" in fields and "TITLE" in fields: return "MIXED"
        return "TITLE_FALLBACK"

    @staticmethod
    def _condition_warnings(conditions: list[dict], count: int, total: int) -> list[str]:
        warnings = []
        if count == 0: warnings.append("ZERO_MATCHES")
        if total and count > total * .7: warnings.append("BROAD_COLLECTION_OVER_70_PERCENT")
        if any(len(_words(row["value"])) == 1 and row["value"].casefold() in GENERIC for row in conditions if row["field"] == "TITLE"):
            warnings.append("AMBIGUOUS_BROAD_RULE")
        return warnings

    @staticmethod
    def _overlap_warnings(collections: list[dict], threshold: float) -> dict[str, list[str]]:
        result: dict[str, list[str]] = defaultdict(list)
        for index, first in enumerate(collections):
            for second in collections[index + 1:]:
                a, b = first["matched_asins"], second["matched_asins"]
                if not a or not b: continue
                ratio = len(a & b) / min(len(a), len(b))
                if ratio >= threshold:
                    result[first["collection_key"]].append(f"EXTREME_OVERLAP:{second['title']}:{ratio:.2f}")
                    result[second["collection_key"]].append(f"EXTREME_OVERLAP:{first['title']}:{ratio:.2f}")
        return result

    @staticmethod
    def _description(profile: dict, category_name: str, title: str) -> str:
        store = html.escape(str(profile.get("store_name") or "our store"))
        concept = html.escape(str(profile.get("concept") or profile.get("category") or "everyday organization"))
        return (f"<p>Explore {html.escape(title.lower())} selected for {store}. Designed to make {html.escape(category_name.lower())} "
                f"simpler, these practical picks support {concept.lower()} with thoughtful fit and everyday function.</p>")

    @staticmethod
    def _image_prompt(profile: dict, category_name: str, title: str) -> str:
        identity = profile.get("visual_identity") or profile.get("brand_voice") or profile.get("design") or {}
        if isinstance(identity, dict):
            identity_text = ", ".join(str(value) for key, value in identity.items() if value and key not in {"logo", "logo_url"})
        else:
            identity_text = str(identity)
        identity_text = identity_text or str(profile.get("concept") or profile.get("category") or "clean, practical, modern")
        return (f"Photorealistic ecommerce lifestyle image for the {title} collection ({category_name}); "
                f"show a clear, realistic category subject in a bright, clean, premium setting, consistent with this store identity: {identity_text}. "
                "Square 1:1 composition, balanced negative space, natural soft light, crisp product detail; no text, no logo, no watermark.")

    def _latest_plan(self, store_id: str) -> dict | None:
        with connect(self.db) as con:
            row = con.execute("SELECT plan_id FROM store_collection_plans WHERE store_id=? ORDER BY version DESC LIMIT 1", (store_id,)).fetchone()
        return self.get_plan(row["plan_id"]) if row else None

    @staticmethod
    def _diff(previous: dict | None, candidates: list[dict]) -> dict:
        if not previous:
            return {"new": [row["collection_key"] for row in candidates], "changed": [], "unchanged": [], "removed_or_disabled": []}
        old = {row["collection_key"]: row for row in previous["collections"]}
        new = {row["collection_key"]: row for row in candidates}
        changed, unchanged = [], []
        fields = ("title", "description_html", "handle", "conditions")
        def normalized_conditions(rows):
            return sorted((row.get("field"), row.get("relation"), row.get("value"), row.get("group_operator", "OR")) for row in rows)
        for key in old.keys() & new.keys():
            changed_fields = [field for field in fields if field != "conditions" and old[key].get(field) != new[key].get(field)]
            if normalized_conditions(old[key].get("conditions", [])) != normalized_conditions(new[key].get("conditions", [])):
                changed_fields.append("conditions")
            if changed_fields: changed.append(key)
            else: unchanged.append(key)
        return {"new": sorted(new.keys() - old.keys()), "changed": sorted(changed),
                "unchanged": sorted(unchanged), "removed_or_disabled": sorted(old.keys() - new.keys())}
