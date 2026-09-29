from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class KeywordRecommendation:
    keyword: str
    source: str
    semantic_fit: float
    candidate_yield: int
    price_fit: float
    quality_fit: float
    risk_rate: float
    novelty: float
    score: float
    reason: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    status: str = "SUGGESTED"

    def to_dict(self) -> dict:
        return asdict(self)
