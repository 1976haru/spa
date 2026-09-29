from __future__ import annotations

import hashlib
import json

from .models import Recipe

CABIN_TIDY_KEYWORDS = (
    "trunk organizer",
    "car seat organizer",
    "backseat organizer",
    "center console organizer",
    "car trash can",
    "headrest hook",
    "car cup holder",
    "car gap filler",
    "car storage organizer",
    "vehicle organizer",
)


def dollars_to_cents(value: float) -> int:
    return int(round(value * 100))


def rating_to_keepa(value: float) -> int:
    return int(round(value * 10))


def recipes_for_profile(profile: dict) -> list[Recipe]:
    config = profile.get("sourcing") or {}
    configured = config.get("recipes")
    if configured:
        keywords = [item["keyword"] if isinstance(item, dict) else str(item) for item in configured]
    elif profile.get("store_id") == "001":
        keywords = list(CABIN_TIDY_KEYWORDS)
    else:
        keywords = list(profile.get("include_keywords") or [])
    return [
        Recipe(
            recipe_id=f"{profile['store_id']}-{index:02}",
            keyword=keyword,
            price_min=float(config.get("price_min", 30)),
            price_max=float(config.get("price_max", 120)),
            min_rating=float(config.get("min_rating", 4.0)),
            min_reviews=int(config.get("min_reviews", 30)),
            min_images=int(config.get("min_images", 2)),
            single_variation=bool(config.get("single_variation", True)),
            exclude_adult=bool(config.get("exclude_adult", True)),
            exclude_hazmat=bool(config.get("exclude_hazmat", True)),
        )
        for index, keyword in enumerate(keywords, 1)
    ]


def finder_query(recipe: Recipe, page: int = 0) -> dict:
    if page < 0 or page * recipe.per_page >= 10_000:
        raise ValueError("Keepa Product Finder supports at most 10,000 results per query")
    return {
        "title": recipe.keyword,
        "productType": 0,
        "singleVariation": recipe.single_variation,
        "isAdultProduct": not recipe.exclude_adult,
        "isHazMat": not recipe.exclude_hazmat,
        "hasReviews": True,
        "current_NEW_gte": dollars_to_cents(recipe.price_min),
        "current_NEW_lte": dollars_to_cents(recipe.price_max),
        "current_RATING_gte": rating_to_keepa(recipe.min_rating),
        "current_COUNT_REVIEWS_gte": recipe.min_reviews,
        "imageCount_gte": recipe.min_images,
        "sort": [["current_SALES", "asc"]],
        "perPage": recipe.per_page,
        "page": page,
    }


def query_hash(query: dict) -> str:
    raw = json.dumps(query, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:20]
