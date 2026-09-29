from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable


class ProductSourceConnector(ABC):
    @abstractmethod
    def iter_products(self, source: Path) -> Iterable[tuple[dict, dict]]:
        """Yield (product_payload, occurrence_metadata)."""
        raise NotImplementedError


class ExportConnector(ABC):
    @abstractmethod
    def export(self, *args, **kwargs) -> object:
        raise NotImplementedError
