from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Classification:
    fit_score: float
    price_status: str
    risk_status: str
    auto_status: str
    reasons: list[str]


def product_text(row) -> str:
    parts = [row["title"] or "", row["brand"] or "", row["category"] or ""]
    for key in ("tags_json", "overview_json", "about_json"):
        try:
            value = json.loads(row[key] or "[]")
        except Exception:
            value = []
        if isinstance(value, list):
            parts.extend(str(x) for x in value)
        elif isinstance(value, dict):
            parts.extend(f"{k} {v}" for k, v in value.items())
    return " ".join(parts).lower()


def price_bucket(price: float | None, profile: dict) -> tuple[str, str]:
    if price is None:
        return "REVIEW", "price_missing"
    bands = profile.get("price_bands") or []
    for band in bands:
        lo = band.get("min")
        hi = band.get("max")
        if lo is not None and price < float(lo):
            continue
        if hi is not None and price >= float(hi):
            continue
        return str(band.get("status", "REVIEW")).upper(), f"price_band:{band.get('name','unnamed')}"
    return "REVIEW", "price_outside_defined_bands"


def risk_check(text: str, profile: dict) -> tuple[str, list[str]]:
    rules = profile.get("risk_rules") or []
    hits: list[str] = []
    highest = "SAFE"
    order = {"SAFE": 0, "REVIEW": 1, "RESTRICTED": 2}
    for rule in rules:
        terms = [str(x).lower() for x in rule.get("terms", [])]
        matched = [t for t in terms if t and t in text]
        if matched:
            status = str(rule.get("status", "REVIEW")).upper()
            if order.get(status, 1) > order.get(highest, 0):
                highest = status
            hits.append(f"{rule.get('code','risk')}:{','.join(matched[:4])}")
    return highest, hits


def fit_score(text: str, profile: dict) -> tuple[float, list[str]]:
    include = [str(x).lower() for x in profile.get("include_keywords", [])]
    exclude = [str(x).lower() for x in profile.get("exclude_keywords", [])]
    include_hits = [x for x in include if x and x in text]
    exclude_hits = [x for x in exclude if x and x in text]

    # Explainable score, not an AI truth score. Manual override always wins later.
    if include:
        score = min(100.0, 25.0 + 75.0 * (len(include_hits) / max(1, min(len(include), 8))))
    else:
        score = 50.0
    score -= min(60.0, len(exclude_hits) * 20.0)
    score = max(0.0, min(100.0, score))
    reasons = []
    if include_hits:
        reasons.append("include:" + ",".join(include_hits[:8]))
    if exclude_hits:
        reasons.append("exclude:" + ",".join(exclude_hits[:8]))
    return round(score, 1), reasons


def classify(row, profile: dict) -> Classification:
    text = product_text(row)
    p_status, p_reason = price_bucket(row["price"], profile)
    r_status, r_reasons = risk_check(text, profile)
    score, f_reasons = fit_score(text, profile)

    reasons = [p_reason] + f_reasons + r_reasons
    min_fit = float(profile.get("minimum_fit_score", 40))

    if r_status == "RESTRICTED":
        auto = "RESTRICTED"
    elif r_status == "REVIEW":
        auto = "REVIEW"
    elif score < min_fit:
        auto = "REVIEW"
        reasons.append(f"fit_below:{min_fit:g}")
    else:
        auto = p_status

    return Classification(
        fit_score=score,
        price_status=p_status,
        risk_status=r_status,
        auto_status=auto,
        reasons=reasons,
    )
