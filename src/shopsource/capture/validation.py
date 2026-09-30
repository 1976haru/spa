from __future__ import annotations

import re
import math
from urllib.parse import urlparse

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
SENSITIVE_MARKERS = (
    "cookie", "authorization", "auth", "password", "secret", "credential", "apikey",
    "token", "sessionstorage", "localstorage", "waf", "shippingaddress", "accountid",
    "orderhistory", "userprofile",
)


def sensitive_paths(value, prefix="") -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            path = f"{prefix}.{key}" if prefix else str(key)
            if any(marker in normalized for marker in SENSITIVE_MARKERS):
                found.append(path)
            found.extend(sensitive_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(sensitive_paths(child, f"{prefix}[{index}]"))
    return found


def canonical_product_url(asin: str) -> str:
    normalized = str(asin or "").strip().upper()
    if not ASIN_RE.fullmatch(normalized):
        raise ValueError("ASIN을 확인할 수 없습니다.")
    return f"https://www.amazon.com/dp/{normalized}"


def validate_product(payload: dict, *, detail: bool = False, allow_missing_title: bool = False) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("상품 자료는 JSON object여야 합니다.")
    bad = sensitive_paths(payload)
    if bad:
        raise ValueError("민감정보로 보이는 필드가 있어 캡처를 거부했습니다.")
    asin = str(payload.get("asin") or "").strip().upper()
    title = str(payload.get("title") or "").strip()
    if not ASIN_RE.fullmatch(asin):
        raise ValueError("ASIN을 확인할 수 없습니다.")
    if not title and not allow_missing_title:
        raise ValueError("상품명이 없습니다.")
    if detail and not payload.get("url"):
        raise ValueError("상품 상세 URL이 없습니다.")
    for field in ("price", "rating", "reviewCount"):
        value = payload.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError(f"{field} 필드는 유효한 숫자 또는 null이어야 합니다.")
    for field in ("url", "_sourceUrl"):
        value = payload.get(field)
        if value is not None and not _amazon_url(value):
            raise ValueError("Amazon 상품 또는 검색 링크가 올바르지 않습니다.")
    images = payload.get("images")
    if images is not None and (not isinstance(images, list) or any(not _amazon_url(value, image=True) for value in images)):
        raise ValueError("Amazon 상품 이미지 주소가 올바르지 않습니다.")
    result = dict(payload)
    result["asin"] = asin
    if allow_missing_title:
        result["title"] = title
    return result


def _amazon_url(value: object, *, image: bool = False) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    valid_host = host == "amazon.com" or host.endswith(".amazon.com")
    if image:
        valid_host = valid_host or host.endswith(".media-amazon.com")
    return parsed.scheme == "https" and valid_host


def reject_captcha(title: str = "", body: str = "") -> None:
    text = f"{title} {body}".lower()
    if any(value in text for value in ("captcha", "robot check", "enter the characters you see")):
        raise ValueError("Amazon에서 확인 화면이 감지되었습니다. 브라우저에서 직접 확인한 뒤 다시 시도하세요.")
