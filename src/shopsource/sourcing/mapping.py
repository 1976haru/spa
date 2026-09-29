from __future__ import annotations

from datetime import datetime, timezone

AMAZON = 0
NEW = 1
SALES = 3
LISTPRICE = 4
COUNT_NEW = 11
BUY_BOX_SHIPPING = 18
KEEPA_TO_SPARK_CAPABILITY = "KEEPA_TO_SPARK_MAPPING_UNVERIFIED"
BROWSER_CAPTURE_TO_SPARK_CAPABILITY = "BROWSER_CAPTURE_TO_SPARK_MAPPING_UNVERIFIED"


def cents_to_dollars(value) -> float | None:
    if value is None:
        return None
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return None
    return None if amount < 0 else amount / 100.0


def _current(raw: dict, index: int):
    current = (raw.get("stats") or {}).get("current") or []
    return current[index] if index < len(current) else None


def keepa_price(raw: dict) -> float | None:
    for index in (BUY_BOX_SHIPPING, AMAZON, NEW):
        price = cents_to_dollars(_current(raw, index))
        if price is not None:
            return price
    return None


def keepa_images(raw: dict) -> list[str]:
    images = raw.get("images")
    if isinstance(images, list):
        return [str(item) for item in images if item]
    csv = raw.get("imagesCSV")
    if not csv:
        return []
    return [f"https://m.media-amazon.com/images/I/{token.strip()}" for token in str(csv).split(",") if token.strip()]


def keepa_to_canonical(raw: dict) -> dict:
    asin = str(raw.get("asin") or "").strip().upper()
    title = str(raw.get("title") or "").strip()
    rating_raw = raw.get("rating")
    if rating_raw is None:
        rating_raw = _current(raw, 16)
    rating = rating_raw / 10.0 if isinstance(rating_raw, (int, float)) and rating_raw > 5 else rating_raw
    reviews = raw.get("reviewCount")
    if reviews is None:
        reviews = _current(raw, 17)
    payload = {
        "asin": asin,
        "title": title,
        "brand": raw.get("brand"),
        "manufacturer": raw.get("manufacturer"),
        "price": keepa_price(raw),
        "url": f"https://www.amazon.com/dp/{asin}" if asin else None,
        "category": raw.get("categoryTree", [{}])[-1].get("name") if raw.get("categoryTree") else None,
        "tags": raw.get("features") or [],
        "overview": raw.get("features") or [],
        "aboutThis": raw.get("description") or [],
        "images": keepa_images(raw),
        "rating": rating,
        "reviewCount": reviews,
        "options": raw.get("variationCSV") or {},
        "quantity": raw.get("packageQuantity"),
        "_sourceUrl": "keepa:product",
        "_collectedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_sourceKind": "KEEPA",
    }
    return payload


def to_spark_product_payload(canonical: dict) -> dict:
    """Map canonical Keepa data to the observed Spark field surface; portal use is unverified."""
    keys = (
        "url", "asin", "title", "brand", "price", "options", "quantity", "tags",
        "category", "overview", "aboutThis", "images", "rating", "reviewCount",
        "_sourceUrl", "_listPage", "_collectedAt",
    )
    return {key: canonical.get(key) for key in keys}


def browser_capture_to_spark_payload(canonical: dict) -> dict:
    """Browser records already use the canonical Spark field names; never invent values."""
    keys = (
        "url", "asin", "title", "brand", "price", "options", "quantity", "tags",
        "category", "overview", "aboutThis", "images", "rating", "reviewCount",
        "_sourceUrl", "_listPage", "_collectedAt",
    )
    defaults = {"options": {}, "tags": [], "overview": [], "aboutThis": [], "images": []}
    return {key: (canonical.get(key) if canonical.get(key) is not None else defaults.get(key)) for key in keys}
