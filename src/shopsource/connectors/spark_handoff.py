from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import ExportConnector


@dataclass(frozen=True)
class SparkHandoffCapability:
    status: str = "CONTRACT_PENDING"
    message: str = (
        "Spark Data Import file/folder contract has not been verified with a real sample. "
        "No Spark-compatible export is generated."
    )


class SparkHandoffConnector(ExportConnector):
    """Contract gate for the future official Spark UI handoff format."""

    capability = SparkHandoffCapability()

    def export(self, *args, **kwargs) -> Path:
        raise RuntimeError(
            f"Spark handoff unavailable: {self.capability.status}. {self.capability.message}"
        )
