from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import SourcingProvider
from ..models import DiscoveryPage, HydrationBatch, Recipe, TokenTelemetry
from ..recipes import finder_query

BASE_URL = "https://api.keepa.com"


class KeepaError(RuntimeError):
    pass


def redact_key(text: str, key: str | None) -> str:
    return text.replace(key, "[REDACTED]") if key else text


def _telemetry(payload: dict) -> TokenTelemetry:
    return TokenTelemetry(
        tokens_left=payload.get("tokensLeft"),
        refill_in=payload.get("refillIn"),
        refill_rate=payload.get("refillRate"),
        tokens_consumed=int(payload.get("tokensConsumed") or 0),
        processing_time_ms=payload.get("processingTimeInMs"),
    )


class KeepaProvider(SourcingProvider):
    name = "keepa"

    def __init__(self, api_key: str | None = None, *, transport=None, sleep=time.sleep,
                 timeout: float = 30, max_retries: int = 3):
        self.api_key = api_key or os.environ.get("KEEPA_API_KEY")
        if not self.api_key:
            raise KeepaError(
                "Keepa API key is not configured. Set the KEEPA_API_KEY environment variable."
            )
        self.transport = transport or self._urlopen_transport
        self.sleep = sleep
        self.timeout = timeout
        self.max_retries = max_retries

    @staticmethod
    def _urlopen_transport(method: str, url: str, body: bytes | None, timeout: float) -> dict:
        request = urllib.request.Request(
            url, data=body, method=method,
            headers={"User-Agent": "ShopSourceStudio/0.2.3", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _request(self, method: str, endpoint: str, params: dict, payload: dict | None = None) -> dict:
        query = urllib.parse.urlencode({**params, "key": self.api_key})
        url = f"{BASE_URL}{endpoint}?{query}"
        body = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(self.max_retries + 1):
            try:
                return self.transport(method, url, body, self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    raise KeepaError(f"Keepa HTTP {exc.code}") from None
                error = exc
            except (urllib.error.URLError, TimeoutError) as exc:
                error = exc
            if attempt >= self.max_retries:
                message = redact_key(str(error), self.api_key)
                raise KeepaError(f"Keepa request failed after retries: {message}") from None
            self.sleep(min(2 ** attempt, 8))
        raise AssertionError("unreachable")

    def discover(self, recipe: Recipe, page: int = 0) -> DiscoveryPage:
        query = finder_query(recipe, page)
        response = self._request("POST", "/query", {"domain": 1}, query)
        asins = [str(value).upper() for value in response.get("asinList", [])]
        return DiscoveryPage(
            asins=asins,
            page=page,
            has_more=len(asins) >= recipe.per_page and (page + 1) * recipe.per_page < 10_000,
            telemetry=_telemetry(response),
        )

    def health(self) -> dict:
        response = self._request("GET", "/token", {"domain": 1})
        return {
            "ok": "tokensLeft" in response,
            "tokensLeft": response.get("tokensLeft"),
            "refillIn": response.get("refillIn"),
            "refillRate": response.get("refillRate"),
        }

    def hydrate(self, asins: list[str]) -> HydrationBatch:
        if not asins or len(asins) > 100:
            raise ValueError("Keepa Product Request batch must contain 1-100 ASINs")
        response = self._request("GET", "/product", {
            "domain": 1, "stats": 90, "history": 0, "asin": ",".join(asins),
        })
        return HydrationBatch(list(response.get("products") or []), _telemetry(response))
