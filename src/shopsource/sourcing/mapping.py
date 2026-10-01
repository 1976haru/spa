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

# Contract extracted from the installed Spark Desktop 1.0.3 main-process bundle
# and its bundled @crawlee/memory-storage dataset reader. Product fields do not
# participate in item enumeration; the selected directory's basename is used
# as an ID under Spark's own storage/datasets root.
SPARK_DESKTOP_LOADER_CONTRACT = {
    "app_version": "1.0.3",
    "selected_folder_handling": "use_basename_as_storage_id",
    "dataset_storage_relative_path": "storage/datasets/<storage_id>",
    "dataset_item_filename_pattern": "{index:09}.json",
    "first_item_index": 1,
    "item_json_read_predicate": "JSON.parse succeeds; no product keys or value types are checked",
    "required_product_fields": [],
    "quantity_required": False,
    "image_main_dimensions_required": False,
    "variation_display_labels_required": False,
    "external_folder_contents_imported": False,
    "count_without_metadata": "count regular files, then read sequential 9-digit item names",
    "count_with_metadata": "use __metadata__.json itemCount",
    "data_info_error_behavior": "caught; returns undefined",
    "get_data_error_behavior": "caught; returns empty items and total zero",
    "excluded_count": "unique ASIN union of deselected and filterDeselected",
    "included_count": "itemCount minus excluded count",
}


def spark_desktop_loader_contract() -> dict:
    """Return the read-only profile of the installed Spark Desktop loader."""
    return dict(SPARK_DESKTOP_LOADER_CONTRACT)


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
    """Adapt observed Browser Capture values to the Spark dataset shapes seen locally."""
    keys = (
        "url", "asin", "title", "brand", "price", "options", "quantity", "tags",
        "category", "overview", "aboutThis", "images", "rating", "reviewCount",
        "_sourceUrl", "_listPage", "_collectedAt",
    )
    search_metadata = canonical.get("_searchMetadata") if isinstance(canonical.get("_searchMetadata"), dict) else {}
    raw_images = canonical.get("images") or []
    images = []
    dimensions = canonical.get("_imageDimensions") if isinstance(canonical.get("_imageDimensions"), dict) else {}
    for value in raw_images:
        if isinstance(value, dict):
            image_url = value.get("hiRes") or value.get("large") or value.get("thumb")
            if not isinstance(image_url, str) or not image_url:
                continue
            image = {**value}
            image.setdefault("hiRes", image_url)
            image.setdefault("thumb", image_url)
            image.setdefault("large", image_url)
            image.setdefault("main", {image_url: dimensions.get(image_url, [])})
            image.setdefault("variant", "")
            image.setdefault("lowRes", None)
            image.setdefault("shoppableScene", None)
            images.append(image)
        elif isinstance(value, str) and value:
            # Preserve the URL in all URL slots. A missing size is represented by
            # an empty integer list; no dimensions are guessed from URL text.
            images.append({
                "hiRes": value,
                "thumb": value,
                "large": value,
                "main": {value: dimensions.get(value, [])},
                "variant": "",
                "lowRes": None,
                "shoppableScene": None,
            })
    raw_options = canonical.get("options")
    if isinstance(raw_options, dict) and "selectedVariations" in raw_options and "variationDisplayLabels" in raw_options:
        options = {
            "selectedVariations": raw_options.get("selectedVariations") if isinstance(raw_options.get("selectedVariations"), dict) else {},
            "variationDisplayLabels": raw_options.get("variationDisplayLabels") if isinstance(raw_options.get("variationDisplayLabels"), dict) else {},
        }
    else:
        options = {"selectedVariations": {}, "variationDisplayLabels": {}}
    collected_at = canonical.get("_collectedAt")
    if collected_at is None:
        collected_at = search_metadata.get("_collectedAt")
    if collected_at is None:
        raise ValueError("Browser Capture is missing an observed collection timestamp")
    source_url = search_metadata.get("_sourceUrl") or canonical.get("_sourceUrl")
    list_page = search_metadata.get("_listPage")
    if list_page is None:
        list_page = canonical.get("_listPage")
    mapped = {key: canonical.get(key) for key in keys}
    mapped.update({
        "images": images,
        "options": options,
        "_sourceUrl": source_url,
        "_listPage": list_page,
        "_collectedAt": normalize_spark_collected_at(collected_at),
    })
    return mapped


def normalize_spark_collected_at(value) -> int:
    """Return the observed Spark epoch-milliseconds timestamp shape without inventing time."""
    if isinstance(value, bool):
        raise ValueError("Invalid Browser Capture timestamp")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid Browser Capture timestamp")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise ValueError("Invalid Browser Capture timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("Browser Capture timestamp must include a timezone")
    return int(parsed.timestamp() * 1000)


def observed_spark_schema_issues(payload: dict) -> list[str]:
    """Return no inferred field-shape errors: Spark's reader only JSON.parse()s.

    ShopSource separately requires product objects with ASIN/title for its own
    export integrity, but the inspected Spark reader has no item-field schema
    predicate. Malformed JSON is rejected by the reader's JSON.parse call.
    """
    return []
