"""Small fixture-friendly HTML parsing helpers; live pages are parsed by the extension."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from .validation import reject_captcha

SEARCH_CARD_SELECTOR = '[data-component-type="s-search-result"][data-asin]'
PRODUCT_SELECTORS = {
    "title": ("#productTitle",), "brand": ("#bylineInfo",),
    "price": ("#corePrice_feature_div .a-offscreen", ".a-price .a-offscreen"),
    "rating": ("#acrPopover", ".a-icon-alt"), "reviewCount": ("#acrCustomerReviewText",),
}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []
    def handle_data(self, data):
        if data.strip(): self.parts.append(data.strip())


def _text(fragment: str) -> str:
    parser = _Text(); parser.feed(fragment); return " ".join(parser.parts)


def parse_search_html(html: str, search_url: str, page_number: int = 1) -> list[dict]:
    reject_captcha("", html[:10000])
    # The extension uses the centralized DOM selector; this parser is intentionally a safe fixture helper.
    cards = re.findall(r'<[^>]+data-component-type=["\']s-search-result["\'][^>]*data-asin=["\']([^"\']+)["\'][^>]*>(.*?)</(?:div|li)>', html, re.I | re.S)
    products = []
    for asin, card in cards:
        title_match = re.search(r'(?:a-size-base-plus|a-size-medium)[^>]*>(.*?)</span>', card, re.I | re.S)
        href = re.search(r'<a[^>]+href=["\']([^"\']*/dp/[^"\']+)', card, re.I)
        img = re.search(r'<img[^>]+src=["\']([^"\']+)', card, re.I)
        title = _text(title_match.group(1)) if title_match else ""
        products.append({"asin": asin.upper(), "title": title, "url": urljoin("https://www.amazon.com", href.group(1)) if href else None,
                         "price": None, "images": [img.group(1)] if img else [], "rating": None,
                         "reviewCount": None, "sponsored": None, "_sourceUrl": search_url,
                         "_listPage": page_number})
    return products


def parse_product_html(html: str, url: str) -> dict:
    reject_captcha("", html[:10000])
    json_ld = []
    for match in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
        try:
            value = json.loads(match)
            candidates = value if isinstance(value, list) else [value]
            json_ld.extend(item for item in candidates if isinstance(item, dict) and "Product" in str(item.get("@type", "")))
        except json.JSONDecodeError:
            continue
    if json_ld:
        item = json_ld[0]
        brand = item.get("brand")
        offers = item.get("offers") or {}
        asin_match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url, re.I)
        sku = item.get("sku") or item.get("productID")
        asin = asin_match.group(1).upper() if asin_match else (str(sku).upper() if isinstance(sku, str) and re.fullmatch(r"[A-Z0-9]{10}", sku, re.I) else None)
        return {"asin": asin, "url": url, "title": item.get("name"),
                "brand": brand.get("name") if isinstance(brand, dict) else brand,
                "price": offers.get("price") if isinstance(offers, dict) else None,
                "images": item.get("image") if isinstance(item.get("image"), list) else ([item["image"]] if item.get("image") else []),
                "rating": (item.get("aggregateRating") or {}).get("ratingValue") if isinstance(item.get("aggregateRating"), dict) else None,
                "reviewCount": (item.get("aggregateRating") or {}).get("reviewCount") if isinstance(item.get("aggregateRating"), dict) else None}
    title = re.search(r'<span[^>]+id=["\']productTitle["\'][^>]*>(.*?)</span>', html, re.I | re.S)
    asin_match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url, re.I)
    embedded = re.search(r"id=[\"']ASIN[\"'][^>]+value=[\"']([A-Z0-9]{10})", html, re.I)
    return {"asin": asin_match.group(1).upper() if asin_match else (embedded.group(1).upper() if embedded else None),
            "url": url, "title": _text(title.group(1)) if title else None,
            "brand": None, "price": None, "images": [], "rating": None, "reviewCount": None}
