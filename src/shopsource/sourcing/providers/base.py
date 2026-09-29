from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import DiscoveryPage, HydrationBatch, Recipe


class SourcingProvider(ABC):
    name = "unknown"

    @abstractmethod
    def discover(self, recipe: Recipe, page: int = 0) -> DiscoveryPage: ...

    @abstractmethod
    def hydrate(self, asins: list[str]) -> HydrationBatch: ...

    def estimate(self, recipes: list[Recipe], target: int) -> dict:
        finder_max = max(1, len(recipes))
        return {
            "finder_requests_min": 1,
            "finder_requests_max": finder_max,
            "product_requests_max": (target + 99) // 100,
            "estimated_token_min": target,
            "estimated_token_max": target + finder_max * 10,
        }
