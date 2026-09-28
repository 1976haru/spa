from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SparkCenterCapability:
    status: str
    message: str
    accepted_contracts: tuple[str, ...]


def capability() -> SparkCenterCapability:
    """Current integration gate.

    v0.1 intentionally performs no undocumented writes. Once Spark Center exposes or
    documents an accepted import/API/handoff contract, implement it behind this module.
    """
    return SparkCenterCapability(
        status="CONTRACT_PENDING",
        message=(
            "Spark Center external input contract has not been confirmed. "
            "Do not write to private app databases or undocumented endpoints."
        ),
        accepted_contracts=("official_api", "csv_json_import", "documented_handoff", "vendor_approved_method"),
    )


def export_to_spark_center(*args, **kwargs):
    cap = capability()
    raise RuntimeError(f"Spark Center connector unavailable: {cap.status}. {cap.message}")
