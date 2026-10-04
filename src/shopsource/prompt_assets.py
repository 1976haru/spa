"""Safe, copy-ready image prompts for ShopSource-managed store assets."""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

from .paths import EXPORT_DIR


DEFAULT_CABIN_CONTEXT = {
    "store_name": "Cabin Tidy",
    "brand_style": "clean, practical, organized, trustworthy, modern, premium but approachable",
    "market": "US",
    "audience": "US drivers, commuters, families, and road-trip users",
    "main_category": "car organization, car storage, and travel organization",
    "colors": "navy, beige/sand, white, and charcoal",
    "visual_direction": "clutter-free practical organization, realistic automotive lifestyle, bright but premium ecommerce photography, clean composition",
    "avoid": "copyrighted logos, watermarks, fake UI, fake reviews, star ratings, guarantees, badges, exaggerated or impossible product claims, embedded text",
}
NEGATIVE = "low quality, cluttered, unrealistic proportions, too dark, too many unrelated objects, visible brand logos, watermark, text, fake badges or reviews, distorted car interior, cartoon style, misleading product claims"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-") or "asset"


def _safe_context(store: dict | None, brand: dict | None) -> dict:
    result = dict(DEFAULT_CABIN_CONTEXT)
    for source in (store or {}, (brand or {}).get("profile", brand or {})):
        aliases = {"brand_name": "store_name", "personality": "brand_style", "target_country": "market",
                   "primary_category": "main_category", "colors": "colors", "brand_keywords": "brand_style"}
        for key, value in source.items():
            dest = aliases.get(key, key)
            if dest in result and isinstance(value, (str, int, float)) and str(value).strip():
                result[dest] = str(value).strip()
    # Public exports contain only recognized descriptive fields, never arbitrary profile secrets.
    return result


class PromptAssetService:
    def build(self, *, store: dict | None = None, brand: dict | None = None,
              collection_plan: dict | None = None, homepage_plan: dict | None = None,
              navigation: dict | None = None) -> dict:
        context = _safe_context(store, brand)
        name, style = context["store_name"], context["brand_style"]
        category = context["main_category"]
        common = (f'Create a clean ecommerce lifestyle image for "{name}". Brand style: {style}. '
                  f"Audience: {context['audience']}. Visual direction: {context['visual_direction']}. "
                  f"Palette: {context['colors']}. Use realistic commercial photography and natural clean daylight.")
        records: list[dict[str, Any]] = []

        def add(title, kind, use, prompt, size, ratio, filename, alt, *, variants=(), overlay=None, cta=None, notes=""):
            records.append({"title": title, "asset_type": kind, "recommended_use": use, "prompt_main": prompt,
                "prompt_variant_1": variants[0] if len(variants) > 0 else prompt + " Use a closer composition with one clear focal subject.",
                "prompt_variant_2": variants[1] if len(variants) > 1 else prompt + " Use a wider environment view while keeping the main subject clear.",
                "negative_prompt": NEGATIVE, "suggested_size": size, "suggested_aspect_ratio": ratio,
                "file_naming_rule": filename, "alt_text_suggestion": alt, "overlay_text_suggestion": overlay,
                "CTA_suggestion": cta, "notes_for_manual_generation": notes or "Paste the prompt into your chosen image tool. Save the result as PNG, JPEG, or WebP, then upload it for inspection.",
                "notes_for_local_generator": "Prompt-only fallback is always available. Local generation is used only when the bridge reports a configured generator.",
                "notes_for_shopify_placement": "Review the image and its mobile crop before approval. Only approved assets enter an apply preview."})

        hero = (f"{common} Show a neatly organized SUV, sedan, or family vehicle trunk/interior in a real commuting or road-trip context. "
                "Practical storage products appear naturally in use without excessive product repetition. Leave clear negative space on one side for Shopify theme headline and CTA overlay; keep key subject mobile-center-safe. "
                "Text-free image only: no words, logo, watermark, rating, guarantee, or badge. Premium but approachable, bright natural daylight, uncluttered, wide Shopify homepage hero composition.")
        add("Homepage Hero Banner", "HERO_BANNER", "홈페이지 메인 배너", hero, "1920 x 1080", "16:9",
            "<store>-hero-<campaign>-v01.png", f"Organized vehicle interior with practical {category} from {name}",
            variants=(hero.replace("SUV, sedan, or family vehicle trunk/interior", "SUV trunk during a calm family road-trip preparation"),
                      hero.replace("Leave clear negative space on one side", "Leave clean negative space on the left side")),
            overlay=(homepage_plan or {}).get("hero", {}).get("headline") or "Thoughtful storage for the road ahead",
            cta=(homepage_plan or {}).get("hero", {}).get("cta_label") or "Shop collections",
            notes="HERO_IMAGE_NO_TEXT is the default. Add headline and CTA separately in the Shopify theme. For a user-supplied precomposed banner, mark MANUAL_ASSET and review mobile cropping/accessibility.")
        add("Hero mobile crop guide", "HERO_MOBILE_CROP_GUIDE", "Hero desktop/mobile 자르기 확인 안내",
            "Keep the main storage product and essential vehicle context inside the center 60% of the canvas. Leave headline-safe space near an outer edge on desktop, but do not place important details at the far edge. Check narrow mobile crop separately.",
            "Use approved hero source", "responsive crop guide", "<store>-hero-crop-notes.txt",
            "Mobile-safe crop of an organized vehicle storage hero")
        add("Hero alt text", "HERO_ALT_TEXT", "Hero 이미지 대체 텍스트 제안",
            f"Write one concise, factual alt-text sentence describing only visible content in the approved {category} lifestyle image. Do not include keywords, claims, CTA text, or information that is not visible.",
            "Text only", "N/A", "<store>-hero-alt.txt", f"Organized car storage scene for {name}",
            notes="After selecting an image, edit the suggestion so it matches the actual image exactly.")
        for collection in (collection_plan or {}).get("collections", []):
            if not collection.get("enabled", True):
                continue
            title = str(collection.get("title") or collection.get("collection_key") or "Collection")
            key = str(collection.get("collection_key") or _slug(title))
            focus = title.casefold()
            coll_prompt = (f"{common} Create a square collection image whose single obvious subject is {title}. "
                           f"Show a realistic vehicle storage scene where {focus} is clearly in use; keep surrounding objects minimal and relevant. "
                           "Consistent framing, daylight, contrast, and visual tone across sibling collection images. No embedded text, logo, or watermark.")
            add(f"Collection · {title}", "COLLECTION_IMAGE", "Shopify collection 대표 이미지", coll_prompt, "1200 x 1200", "1:1",
                f"<store>-collection-{_slug(key)}-v01.png", f"{title} storage solution in a clean vehicle interior",
                variants=(coll_prompt + " Use a close product-in-use view.", coll_prompt + " Use a slightly wider vehicle-context view."),
                notes="Generate one distinct image for this actual collection. Avoid reusing an unrelated category image.")
            add(f"Category Card · {title}", "CATEGORY_SHORTCUT", "홈페이지 카테고리 바로가기 카드", coll_prompt,
                "1200 x 1200", "1:1 (optional 4:5 crop)", f"<store>-category-{_slug(key)}-v01.png",
                f"{title} category card showing a practical car storage scene")
        extras = [
            ("Header support visual", "HEADER_SUPPORT", "헤더 근처 소개/프로모션 보조 이미지", "A calm detail of an organized vehicle interior, with a simple focal point and space for separate interface text."),
            ("Featured collection support", "FEATURED_SUPPORT", "추천 컬렉션 섹션 보조 이미지", "A cohesive editorial view of practical vehicle storage categories; keep products distinct and the scene uncluttered."),
            ("About brand visual", "ABOUT_BRAND", "브랜드 소개 페이지", "A candid, trustworthy everyday vehicle organization scene with no people identifiable and no unsupported claims."),
            ("Contact support visual", "CONTACT_SUPPORT", "문의/고객 지원 안내 섹션", "A welcoming, simple travel-preparation scene that supports a helpful service tone without showing fake contact details."),
        ]
        for title, kind, use, subject in extras:
            add(title, kind, use, f"{common} {subject} No text, logo, watermark, or fake interface. Match the main homepage hero tone.",
                "1200 x 900", "4:3", f"<store>-{_slug(kind)}-v01.png", f"{name} {use} visual")
        records.append({"title": "Logo / Favicon reuse guidance", "asset_type": "BRAND_REUSE_GUIDANCE",
            "recommended_use": "기존 승인 로고와 파비콘을 안전하게 배치", "prompt_main": "Use only the existing approved Cabin Tidy logo artwork. Keep the full wordmark on a clean, high-contrast background; preserve clear space and do not redraw, recolor, stretch, or add effects. For favicon, use the approved symbol only, centered with generous edge padding.",
            "prompt_variant_1": "Header logo guidance: choose a quiet light or dark background that meets readable contrast and keeps the original logo intact.",
            "prompt_variant_2": "Favicon guidance: use the approved standalone mark, centered and recognizable at 32 x 32 pixels.",
            "negative_prompt": "redrawn logo, modified wordmark, extra text, watermark", "suggested_size": "Use existing approved source files", "suggested_aspect_ratio": "Preserve source ratio; favicon 1:1",
            "file_naming_rule": "Reuse existing approved brand asset; do not create a duplicate", "alt_text_suggestion": f"{name} logo",
            "overlay_text_suggestion": None, "CTA_suggestion": None,
            "notes_for_manual_generation": "This is placement guidance, not permission to recreate the mark.",
            "notes_for_local_generator": "Do not send the logo through image generation; reuse the approved source asset.",
            "notes_for_shopify_placement": "Preserve approved logo and favicon mappings; preview before any explicit theme apply."})
        run_id = "AP_" + secrets.token_hex(8)
        has_collection_plan = bool(collection_plan and collection_plan.get("collections"))
        groups = {
            "hero": {"status": "READY", "asset_types": ["HERO_BANNER", "HERO_MOBILE_CROP_GUIDE", "HERO_ALT_TEXT"]},
            "collection": {"status": "READY" if has_collection_plan else "WAITING_FOR_COLLECTION_PLAN",
                           "asset_types": ["COLLECTION_IMAGE"]},
            "category_shortcut": {"status": "READY" if has_collection_plan else "WAITING_FOR_COLLECTION_PLAN",
                                  "asset_types": ["CATEGORY_SHORTCUT"]},
            "header_section": {"status": "READY", "asset_types": ["HEADER_SUPPORT", "FEATURED_SUPPORT", "ABOUT_BRAND", "CONTACT_SUPPORT", "BRAND_REUSE_GUIDANCE"]},
        }
        return {"schema_version": "1.0", "run_id": run_id, "store": context["store_name"], "assets": records,
                "groups": groups,
                "summary": {"total": len(records), "hero": 1,
                            "collections": sum(x["asset_type"] == "COLLECTION_IMAGE" for x in records),
                            "categories": sum(x["asset_type"] == "CATEGORY_SHORTCUT" for x in records),
                            "collection_status": groups["collection"]["status"],
                            "category_status": groups["category_shortcut"]["status"]}}

    def export(self, prompt_set: dict, *, store_id: str, output_root: str | Path | None = None) -> dict:
        root = Path(output_root or EXPORT_DIR) / "asset_prompts" / _slug(store_id) / prompt_set["run_id"]
        root.mkdir(parents=True, exist_ok=True)
        groups = {"hero_prompts.md": {"HERO_BANNER", "HERO_MOBILE_CROP_GUIDE", "HERO_ALT_TEXT"}, "collection_prompts.md": {"COLLECTION_IMAGE"},
                  "category_prompts.md": {"CATEGORY_SHORTCUT"}, "header_prompts.md": {"HEADER_SUPPORT", "FEATURED_SUPPORT", "ABOUT_BRAND", "CONTACT_SUPPORT", "BRAND_REUSE_GUIDANCE"}}
        paths = {}
        for filename, kinds in groups.items():
            selected = [x for x in prompt_set["assets"] if x["asset_type"] in kinds]
            content = "\n\n".join(f"# {x['title']}\n\n{x['prompt_main']}\n\nNegative prompt: {x['negative_prompt']}\n\nSize: {x['suggested_size']} ({x['suggested_aspect_ratio']})\nAlt: {x['alt_text_suggestion']}" for x in selected)
            (root / filename).write_text(content + "\n", encoding="utf-8")
            paths[filename] = str(root / filename)
        bundle = "\n\n".join(f"[{x['title']} / Copy]\n{x['prompt_main']}\n\nNegative prompt:\n{x['negative_prompt']}\n\nSuggested size: {x['suggested_size']} ({x['suggested_aspect_ratio']})" for x in prompt_set["assets"])
        (root / "copy_paste_bundle.txt").write_text(bundle + "\n", encoding="utf-8")
        paths["copy_paste_bundle.txt"] = str(root / "copy_paste_bundle.txt")
        (root / "asset_prompts.json").write_text(json.dumps(prompt_set, ensure_ascii=False, indent=2), encoding="utf-8")
        paths["asset_prompts.json"] = str(root / "asset_prompts.json")
        return {"folder": str(root), "files": paths}
