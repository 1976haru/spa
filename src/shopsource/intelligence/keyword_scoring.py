from __future__ import annotations

WEIGHTS = {
    "semantic_fit": 0.35,
    "candidate_yield": 0.20,
    "price_fit": 0.20,
    "quality_fit": 0.10,
    "novelty": 0.10,
}
RISK_PENALTY = 25.0


def lexical_semantic_fit(keyword: str, concept_terms: set[str]) -> float:
    words = set(keyword.lower().split())
    if not words or not concept_terms:
        return 0.0
    return len(words & concept_terms) / len(words | concept_terms)


def score_keyword(*, semantic_fit: float, candidate_yield: int, price_fit: float,
                  quality_fit: float, novelty: float, risk_rate: float,
                  max_yield: int = 100) -> float:
    yield_score = min(1.0, candidate_yield / max(1, max_yield))
    base = (
        WEIGHTS["semantic_fit"] * semantic_fit
        + WEIGHTS["candidate_yield"] * yield_score
        + WEIGHTS["price_fit"] * price_fit
        + WEIGHTS["quality_fit"] * quality_fit
        + WEIGHTS["novelty"] * novelty
    )
    return round(max(0.0, min(100.0, base * 100 - risk_rate * RISK_PENALTY)), 1)
