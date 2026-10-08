"""Beginner-safe homepage assignment automation.

This module only builds plans and workflow gates.  Remote theme mutation stays
in :mod:`homepage_automation` and can run only after explicit confirmation.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


CAPABILITIES = {
    "NATIVE_THEME_AUTO",
    "NATIVE_THEME_REVIEW_REQUIRED",
    "EXTERNAL_PAGE_BUILDER_MANUAL",
    "UNSUPPORTED_MANUAL",
}


def discover_assignment_capability(theme: dict) -> dict:
    """Describe safe native support without guessing a theme implementation."""
    marker = " ".join(str(theme.get(k, "")) for k in ("builder", "template", "provider")).casefold()
    if any(name in marker for name in ("pagefly", "gempages", "shogun", "external")):
        return {"status": "EXTERNAL_PAGE_BUILDER_MANUAL", "category_mode": None,
                "reason": "외부 페이지 빌더 화면은 자동 클릭하지 않습니다."}
    sections = theme.get("sections") or []
    modes = {str(x.get("mode") or x.get("type") or "").upper().replace("-", "_") for x in sections}
    if "COLLECTION_LIST" in modes:
        return {"status": "NATIVE_THEME_AUTO", "category_mode": "COLLECTION_LIST", "reason": None}
    if "MULTICOLUMN" in modes:
        return {"status": "NATIVE_THEME_REVIEW_REQUIRED", "category_mode": "MULTICOLUMN",
                "reason": "Multicolumn fallback의 링크와 이미지를 검토해야 합니다."}
    return {"status": "UNSUPPORTED_MANUAL", "category_mode": None,
            "reason": "확인 가능한 카테고리 섹션 schema가 없습니다."}


def prepare_banner(hero: dict, *, real_targets: set[str], asset: dict | None = None) -> dict:
    """Prepare an assignment hero; generated assets always use theme text overlay."""
    result = deepcopy(hero)
    warnings: list[str] = []
    target = str(result.get("cta_target") or "").strip()
    target_ok = bool(target and target != "#" and target in real_targets)
    chosen = deepcopy(asset or {})
    if chosen:
        result["image_asset_id"] = chosen.get("asset_id") or chosen.get("path")
        result["image_url"] = chosen.get("url")
        result["asset_kind"] = "MANUAL_ASSET" if chosen.get("provider") == "MANUAL" else "GENERATED"
        result["asset_approved"] = chosen.get("approval_status") == "APPROVED"
        if chosen.get("precomposed_text"):
            warnings.extend(["MOBILE_CROP_REVIEW", "ACCESSIBILITY_TEXT_DUPLICATION_REVIEW"])
    else:
        result["asset_kind"] = "GENERATED_PENDING"
        result["asset_approved"] = False
    result["text_in_image"] = False
    result["theme_text_overlay"] = True
    result["cta_valid"] = target_ok
    if not target_ok:
        warnings.append("CTA_REQUIRES_REAL_TARGET")
    if not result.get("asset_approved"):
        warnings.append("HERO_ASSET_APPROVAL_REQUIRED")
    return {"hero": result, "warnings": warnings,
            "status": "READY" if not warnings else "REVIEW_REQUIRED"}


def prepare_category_shortcuts(collection_plan: dict, mappings: dict[str, dict],
                               approved_images: dict[str, dict], theme: dict,
                               *, maximum: int = 8) -> dict:
    capability = discover_assignment_capability(theme)
    items, skipped, warnings = [], [], []
    enabled = [x for x in collection_plan.get("collections", []) if x.get("enabled", True)]
    enabled.sort(key=lambda x: (int(x.get("priority", 999)), str(x.get("collection_key", ""))))
    seen: dict[str, str] = {}
    for source in enabled:
        key = str(source.get("collection_key") or "")
        mapping = mappings.get(key) or {}
        handle = str(mapping.get("handle") or "").strip()
        remote_id = mapping.get("remote_id") or mapping.get("shopify_collection_id")
        if not handle or not remote_id or mapping.get("status", "READY") not in {"READY", "VERIFIED", "NO_CHANGE", "SAFE_ADOPT"}:
            skipped.append({"collection_key": key, "reason": "MISSING_REMOTE_COLLECTION"})
            continue
        target = f"/collections/{handle}"
        if target in seen and seen[target] != key:
            warnings.append({"code": "WRONG_DUPLICATE_TARGET", "collection_key": key,
                             "other_collection_key": seen[target], "target": target})
        else:
            seen[target] = key
        image = approved_images.get(key) or {}
        image_ok = image.get("approval_status") == "APPROVED"
        items.append({"collection_key": key, "title": source.get("title") or key,
                      "target": target, "target_collection_id": remote_id,
                      "image": image.get("url") or image.get("path") if image_ok else None,
                      "image_source": "APPROVED_COLLECTION_IMAGE_REUSE" if image_ok else "MISSING",
                      "alt_text": image.get("alt_text") or source.get("image_alt_text") or source.get("title") or key,
                      "target_correct": mapping.get("collection_key", key) == key})
        if len(items) >= min(8, max(4, int(maximum))):
            break
    wrong = [x for x in items if not x["target_correct"]]
    if wrong:
        warnings.extend({"code": "WRONG_COLLECTION_TARGET", "collection_key": x["collection_key"]} for x in wrong)
    if capability["status"] != "NATIVE_THEME_AUTO":
        warnings.append({"code": capability["status"], "reason": capability["reason"]})
    return {"items": items, "skipped": skipped, "warnings": warnings, "capability": capability,
            "status": "BLOCKED" if any(x.get("code") in {"WRONG_DUPLICATE_TARGET", "WRONG_COLLECTION_TARGET"} for x in warnings)
            else "READY" if len(items) >= 4 and capability["status"] == "NATIVE_THEME_AUTO" else "REVIEW_REQUIRED"}


class HomepageAssignmentService:
    """Compose a one-click, preview-first assignment flow."""

    def build(self, *, homepage: dict, collection_plan: dict, mappings: dict[str, dict],
              approved_images: dict[str, dict], theme: dict, hero_asset: dict | None = None,
              featured_products: dict | None = None) -> dict:
        original = deepcopy(homepage)
        targets = {f"/collections/{m['handle']}" for m in mappings.values()
                   if m.get("handle") and (m.get("remote_id") or m.get("shopify_collection_id"))}
        banner = prepare_banner(homepage.get("hero") or {}, real_targets=targets, asset=hero_asset)
        categories = prepare_category_shortcuts(collection_plan, mappings, approved_images, theme)
        featured = [x for x in categories["items"][:4]]
        proposed = deepcopy(original)
        proposed.update({"hero": banner["hero"], "category_shortcuts": categories["items"],
                         "featured_collections": featured,
                         "featured_products": deepcopy((featured_products or {}).get("items", []))})
        # Non-managed content is copied, never rebuilt.
        proposed["unrelated_sections"] = deepcopy(original.get("unrelated_sections", []))
        capability = categories["capability"]
        featured_status = (featured_products or {}).get("status", "READY")
        can_apply = banner["status"] == "READY" and categories["status"] == "READY" and featured_status in {"READY", "VERIFIED"}
        return {"sequence": ["HERO", "CATEGORY_SHORTCUTS", "FEATURED_COLLECTIONS", "FEATURED_PRODUCTS_PLAN",
                             "FEATURED_PRODUCTS_VERIFY", "LINK_CHECK",
                             "THEME_PREVIEW", "THEME_WRITE_CONFIRMATION", "REMOTE_VERIFY", "ASSIGNMENT_READY"],
                "current": original, "proposed": proposed, "banner": banner, "categories": categories,
                "featured_products": featured_products or {"status": "REVIEW_REQUIRED", "items": [], "reasons": ["FEATURED_PRODUCTS_PLAN_REQUIRED"]},
                "featured_products_required": featured_products is not None,
                "capability": capability, "theme_write_requires_confirmation": True,
                "theme_write_allowed": can_apply, "external_gui_automation": False}

    def checklist(self, preview: dict, *, applied=False, verified=False, desktop_checked=False,
                  mobile_checked=False) -> dict:
        hero = preview["banner"]["hero"]
        cats = preview["categories"]
        items = cats["items"]
        featured = preview.get("featured_products") or {"items": []}
        product_items = featured.get("items", [])
        checks = {
            "hero_visible": bool(hero.get("enabled", True) and hero.get("image_asset_id")),
            "title_and_description": bool(hero.get("headline") and hero.get("body")),
            "cta": bool(hero.get("cta_label")), "cta_real_link": bool(hero.get("cta_valid")),
            "banner_image": bool(hero.get("image_asset_id") and hero.get("asset_approved")),
            "four_category_shortcuts": len(items) >= 4,
            "shortcut_images": bool(items) and all(x.get("image") for x in items),
            "correct_collection_targets": bool(items) and all(x.get("target_correct") for x in items),
            "no_blank_or_hash": all(x.get("target") not in {None, "", "#"} for x in items),
            "no_wrong_duplicate_target": not any(x.get("code") == "WRONG_DUPLICATE_TARGET" for x in cats["warnings"]),
            "desktop_checked": bool(desktop_checked), "mobile_checked": bool(mobile_checked),
            "applied": bool(applied), "remote_verified": bool(verified),
        }
        if preview.get("featured_products_required"):
            canonical = preview.get("canonical_homepage_preview") or {}
            canonical_template = canonical.get("proposed") or {}
            feature_preview = canonical.get("featured_products_preview") or {}
            feature_section_id = feature_preview.get("section_id")
            feature_section = (canonical_template.get("sections") or {}).get(feature_section_id) if feature_section_id else None
            feature_settings = (feature_section or {}).get("settings") or {}
            expected_ids = [x.get("shopify_product_id") for x in product_items]
            feature_visible = bool(feature_section and any(value == expected_ids for value in feature_settings.values()))
            checks.update({
                "featured_products_count": len(product_items) >= int(featured.get("requested_count", 4)),
                "featured_products_unique": len({x.get("shopify_product_id") for x in product_items}) == len(product_items) and len({x.get("shopify_handle") for x in product_items}) == len(product_items),
                "featured_products_active": bool(product_items) and all(x.get("remote_status") == "ACTIVE" for x in product_items),
                "featured_products_real_links": bool(product_items) and all(x.get("shopify_handle") for x in product_items),
                "featured_products_price_valid": bool(product_items) and all(float(x.get("price") or 0) > 0 for x in product_items),
                "featured_products_images_ready": bool(product_items) and all(x.get("image_url") for x in product_items),
                "featured_products_storefront_eligible": bool(product_items) and all(x.get("remote_status") == "ACTIVE" for x in product_items),
                "featured_products_visible_in_proposal": feature_visible,
                "homepage_preview_current": canonical.get("status") == "PREVIEW" and canonical.get("featured_products_plan_id") == featured.get("plan_id"),
                "homepage_preview_not_stale": canonical.get("status") != "STALE",
                "write_not_run": not bool(preview.get("applied")),
            })
        ready = all(checks.values()) and preview["capability"]["status"] in {"NATIVE_THEME_AUTO", "NATIVE_THEME_REVIEW_REQUIRED"}
        return {"checks": checks, "status": "ASSIGNMENT_READY" if ready else "REVIEW_REQUIRED",
                "ready": ready, "manual_reason": preview["capability"].get("reason")}


def homepage_assignment_workflow(preview: dict, *, preview_id: str | None = None,
                                 assets_approved: bool = False) -> list[dict[str, Any]]:
    """Queue specification: all local stages run; write is the single confirmation gate."""
    tasks = [
        {"task_key": "HERO", "title": "메인 배너 자동 만들기", "stage": "Hero"},
        {"task_key": "CATEGORY_SHORTCUTS", "title": "카테고리 바로가기 자동 만들기", "stage": "Category Shortcuts"},
        {"task_key": "FEATURED_COLLECTIONS", "title": "추천 컬렉션 구성", "stage": "Featured Collections"},
        {"task_key": "LINK_CHECK", "title": "링크 검사", "stage": "링크 검사"},
        {"task_key": "THEME_PREVIEW", "title": "테마 미리보기", "stage": "테마 미리보기"},
    ]
    tasks[3:3] = [
        {"task_key": "FEATURED_PRODUCTS_PLAN", "title": "추천 상품 자동 구성", "stage": "Featured Products plan"},
        {"task_key": "FEATURED_PRODUCTS_VERIFY", "title": "추천 상품 검증", "stage": "Featured Products verify"},
    ]
    capability = preview["capability"]["status"]
    if capability in {"EXTERNAL_PAGE_BUILDER_MANUAL", "UNSUPPORTED_MANUAL"} or not preview_id:
        tasks.append({"task_key": "EXTERNAL_MANUAL", "title": "외부 빌더 수동 적용", "stage": "수동 작업",
                      "requires_user_input": True})
    else:
        tasks.append({"task_key": "THEME_WRITE", "title": "홈페이지 실제 적용", "stage": "Theme write",
                      "requires_confirmation": True,
                      "confirmation_prompt": "미리보기대로 홈페이지 Theme 변경을 실제 적용합니다.",
                      "checkpoint": {"preview_id": preview_id, "assets_approved": bool(assets_approved)}})
        tasks.append({"task_key": "VERIFY", "title": "적용 결과 자동 검증", "stage": "검증"})
    tasks.append({"task_key": "ASSIGNMENT_READY", "title": "과제 제출용 확인", "stage": "완료"})
    return tasks
