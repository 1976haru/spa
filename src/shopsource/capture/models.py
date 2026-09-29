from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CaptureResult:
    run_id: str
    candidates: int
    detailed: int
    status: str


COMPLETENESS_WEIGHTS = {
    "asin": 0, "title": 0, "url": 10, "price": 15, "images": 15,
    "brand": 10, "category": 10, "aboutThis": 10, "overview": 10,
    "rating": 5, "reviewCount": 5, "options": 5, "quantity": 5,
}


def completeness_score(payload: dict) -> int:
    if not str(payload.get("asin") or "").strip() or not str(payload.get("title") or "").strip():
        return 0
    score = 20  # required identity and title
    for field, weight in COMPLETENESS_WEIGHTS.items():
        if field in {"asin", "title"}:
            continue
        value = payload.get(field)
        present = bool(value) if isinstance(value, (list, dict, str)) else value is not None
        if present:
            score += round(weight * 0.8)
    return min(100, score)
