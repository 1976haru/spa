from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Recipe:
    recipe_id: str
    keyword: str
    price_min: float = 30.0
    price_max: float = 120.0
    min_rating: float = 4.0
    min_reviews: int = 30
    min_images: int = 2
    single_variation: bool = True
    exclude_adult: bool = True
    exclude_hazmat: bool = True
    per_page: int = 100

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TokenTelemetry:
    tokens_left: int | None = None
    refill_in: int | None = None
    refill_rate: int | None = None
    tokens_consumed: int = 0
    processing_time_ms: int | None = None


@dataclass
class DiscoveryPage:
    asins: list[str]
    page: int
    has_more: bool
    telemetry: TokenTelemetry = field(default_factory=TokenTelemetry)


@dataclass
class HydrationBatch:
    products: list[dict]
    telemetry: TokenTelemetry = field(default_factory=TokenTelemetry)
