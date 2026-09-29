from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class KeepaClientAssessment:
    package: str = "akaszynski/keepa"
    product_finder_available: bool = True
    product_request_available: bool = True
    async_client_available: bool = True
    typed_query_parameters: bool = False
    direct_token_telemetry_contract: bool = False
    decision: str = "RETAIN_RAW_PROVIDER"
    reason: str = (
        "The client exposes finder/query helpers, but this project has not verified that its request "
        "configuration preserves our exact token telemetry, retry pacing and API key redaction contract."
    )

    def to_dict(self) -> dict:
        return asdict(self)


def assess_keepa_client() -> KeepaClientAssessment:
    """Dependency-free adapter spike record; does not instantiate a client or make requests."""
    return KeepaClientAssessment()
