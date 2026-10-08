"""Shared homepage prerequisites, state contract, and canonical preview workflow."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .brand_automation import brand_profile_from_store, get_brand_profile
from .collection_prerequisite import CollectionPrerequisiteService
from .db import connect, get_store
from .homepage_assignment import HomepageAssignmentService
from .homepage_automation import (build_homepage_plan, compose_homepage_preview,
                                  invalidate_homepage_previews, normalize_text_value, _hash)
from .homepage_featured_products import FeaturedProductAssignmentService
from .homepage_collections import ShopifyThemeReader
from .prompt_assets import PromptAssetService

SESSION_STAGES = (
    "STORE_SELECTED", "BRAND_READY", "COLLECTIONS_READY", "HOMEPAGE_PLAN_READY",
    "THEME_SNAPSHOT_READY", "FEATURED_PRODUCTS_READY", "HOMEPAGE_PREVIEW_READY",
    "ASSIGNMENT_REVIEW_READY", "APPLY_CONFIRMATION_REQUIRED", "VERIFIED",
)
PREVIEW_OPERATIONS = {"FEATURED_PREVIEW", "FULL_PREVIEW", "ASSIGNMENT_CHECK"}
HOMEPAGE_BUTTON_CONTRACTS = {
    "홈페이지 자동 설계": {"operation": "DESIGN", "visible_effect": "계획·단계 상태 카드 갱신", "remote": "read-only theme/product reads", "write": False},
    "홈페이지 자동 완성": {"operation": "FULL_PREVIEW", "visible_effect": "canonical preview와 apply confirmation gate", "remote": "read-only prerequisite reads", "write": False},
    "메인 배너 자동 만들기": {"operation": "PROMPT_PREPARATION", "visible_effect": "prompt panel 표시", "remote": "none unless separate opted-in asset action", "write": False},
    "카테고리 바로가기 자동 만들기": {"operation": "PROMPT_PREPARATION", "visible_effect": "category prompt panel 표시", "remote": "local plan/read-only data", "write": False},
    "추천 상품 자동 구성": {"operation": "FEATURED_PLAN", "visible_effect": "4-row summary and status", "remote": "read_products only", "write": False},
    "상품 4개 보기": {"operation": "FEATURED_REVIEW", "visible_effect": "four-card dialog", "remote": "none", "write": False},
    "다시 선택": {"operation": "FEATURED_RESELECT", "visible_effect": "updated four-row plan and stale badge", "remote": "cached/read_products only", "write": False},
    "Shopify 상품 다시 읽기": {"operation": "FEATURED_REFRESH", "visible_effect": "refreshed diagnostics and plan", "remote": "read_products only", "write": False},
    "추천 상품 미리보기": {"operation": "FEATURED_PREVIEW", "visible_effect": "canonical proposed section/status", "remote": "read-only theme discovery", "write": False},
    "홈페이지 미리보기": {"operation": "FULL_PREVIEW", "visible_effect": "canonical homepage preview panel", "remote": "read-only theme discovery", "write": False},
    "과제 제출용 확인": {"operation": "ASSIGNMENT_CHECK", "visible_effect": "pass/fail checklist dialog", "remote": "read-only prerequisite reads", "write": False},
    "Shopify 적용": {"operation": "APPLY_CONFIRMATION_REQUIRED", "visible_effect": "explicit confirmation dialog", "remote": "no call before confirmation", "write": "confirmation + guarded service gate"},
    "롤백": {"operation": "ROLLBACK_CONFIRMATION_REQUIRED", "visible_effect": "explicit rollback dialog", "remote": "no call before confirmation", "write": "confirmation + guarded service gate"},
}


@dataclass
class HomepageSession:
    store_id: str
    stage: str = "STORE_SELECTED"
    brand_profile: dict | None = None
    collection_plan: dict | None = None
    homepage_plan: dict | None = None
    theme_snapshot: dict | None = None
    featured_product_plan: dict | None = None
    prompt_set: dict | None = None
    canonical_preview: dict | None = None
    assignment_preview: dict | None = None
    assignment_check: dict | None = None
    statuses: dict[str, str] = field(default_factory=dict)
    error: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def validate_homepage_contract(name: str, value: Any) -> dict:
    """Small boundary validators with field-path diagnostics and safe status codes."""
    errors: list[str] = []
    if name == "brand_profile":
        if not isinstance(value, dict): errors.append("brand_profile")
        else:
            profile = value.get("profile", value)
            if not isinstance(profile, dict): errors.append("brand_profile.profile")
            elif not any(normalize_text_value(profile.get(key)) for key in ("brand_name", "primary_category", "brand_keywords")):
                errors.append("brand_profile.profile.brand_name|primary_category|brand_keywords")
    elif name == "collection_plan":
        if not isinstance(value, dict): errors.append("collection_plan")
        elif not isinstance(value.get("collections"), list): errors.append("collection_plan.collections")
        elif any(not isinstance(row, dict) for row in value["collections"]): errors.append("collection_plan.collections[]")
    elif name == "homepage_plan":
        if not isinstance(value, dict): errors.append("homepage_plan")
        elif not isinstance(value.get("hero"), dict): errors.append("homepage_plan.hero")
        elif not isinstance(value.get("categories"), list): errors.append("homepage_plan.categories")
    elif name == "theme_snapshot":
        if not isinstance(value, dict): errors.append("theme_snapshot")
        elif not isinstance(value.get("theme_files", {}), dict): errors.append("theme_snapshot.theme_files")
        elif value.get("template") is not None and not isinstance(value.get("template"), dict): errors.append("theme_snapshot.template")
    elif name == "featured_product_plan":
        if not isinstance(value, dict): errors.append("featured_product_plan")
        elif not isinstance(value.get("items"), list): errors.append("featured_product_plan.items")
    elif name == "featured_preview":
        if not isinstance(value, dict): errors.append("featured_preview")
        elif value.get("status") == "PREVIEW" and not isinstance(value.get("proposed"), dict): errors.append("featured_preview.proposed")
    else:
        errors.append(name)
    return {"status": "INVALID_DATA" if errors else "VALID", "paths": errors}


def _normalized_brand(brand: dict) -> dict:
    """Make a planning-only copy; persisted profile values are never rewritten."""
    result = dict(brand)
    profile = dict(brand.get("profile", brand))
    for key in ("brand_name", "tagline", "primary_category", "target_customer", "target_country",
                "brand_keywords", "personality", "colors", "avoid_styles", "background_preference",
                "typography_style", "logo_style", "icon_style"):
        if key in profile:
            profile[key] = normalize_text_value(profile[key])
    result["profile"] = profile
    return result


class HomepagePrerequisiteService:
    """Resolve the complete local/read-only context for any homepage action."""

    def __init__(self, *, db=None, theme_reader=None, product_reader=None, theme_reader_factory=ShopifyThemeReader):
        self.db = db
        self.theme_reader = theme_reader
        self.product_reader = product_reader
        self.theme_reader_factory = theme_reader_factory

    def resolve(self, store_id: str, operation: str, *, state: dict | None = None,
                force_theme: bool = False, force_products: bool = False,
                maximum_categories: int = 8) -> HomepageSession:
        session = HomepageSession(str(store_id), statuses={"store": "READY"})
        state = state if isinstance(state, dict) else {}
        if operation not in {"DESIGN", "FEATURED_PLAN", "FEATURED_PREVIEW", "FULL_PREVIEW", "ASSIGNMENT_CHECK"}:
            session.error = {"status": "INVALID_DATA", "message": "지원하지 않는 홈페이지 작업입니다.", "path": "operation"}
            return session
        try:
            store = get_store(store_id, self.db)
            brand = get_brand_profile(store_id, db=self.db) or brand_profile_from_store(store_id, db=self.db)
            validation = validate_homepage_contract("brand_profile", brand)
            if validation["status"] != "VALID":
                return self._invalid(session, "브랜드 정보를 확인할 수 없습니다. Brand Profile을 확인하세요.", validation["paths"])
            brand = _normalized_brand(brand)
            session.brand_profile = brand
            session.stage = "BRAND_READY"
            session.statuses["brand"] = "READY"

            collection_result = CollectionPrerequisiteService(self.db).ensure_collection_plan(str(store_id))
            if collection_result.status != "READY" or not collection_result.plan:
                session.statuses["collections"] = collection_result.status
                session.error = {"status": "MISSING_PREREQUISITE", "message": "컬렉션 계획을 준비할 수 없습니다. 상품/카테고리 입력을 확인하세요.", "reason": collection_result.reason}
                return session
            collection_plan = collection_result.plan
            validation = validate_homepage_contract("collection_plan", collection_plan)
            if validation["status"] != "VALID": return self._invalid(session, "컬렉션 계획 형식이 올바르지 않습니다.", validation["paths"])
            session.collection_plan = collection_plan
            session.stage = "COLLECTIONS_READY"
            session.statuses["collections"] = "READY"

            mappings = self._collection_handles(store_id)
            assets = self._collection_assets(store_id)
            homepage_plan = build_homepage_plan(store_id=store_id, brand=brand, collection_plan=collection_plan,
                collection_handles=mappings, collection_assets=assets, maximum_categories=maximum_categories, db=self.db)
            hero_inputs = state.get("hero_overrides") or {}
            if isinstance(hero_inputs, dict):
                for key in ("image_url", "asset_approved", "theme_image_ref", "theme_image_ref_confirmed"):
                    if key in hero_inputs:
                        homepage_plan["hero"][key] = normalize_text_value(hero_inputs[key]) if key in {"image_url", "theme_image_ref"} else bool(hero_inputs[key])
                if homepage_plan["hero"].get("image_url"):
                    homepage_plan["hero"].setdefault("image_asset_id", "SHOPIFY_FILES_URL")
            validation = validate_homepage_contract("homepage_plan", homepage_plan)
            if validation["status"] != "VALID": return self._invalid(session, "홈페이지 계획 형식이 올바르지 않습니다.", validation["paths"])
            session.homepage_plan = homepage_plan
            session.prompt_set = PromptAssetService().build(store=store, brand=brand,
                collection_plan=collection_plan, homepage_plan=homepage_plan)
            session.stage = "HOMEPAGE_PLAN_READY"
            session.statuses["homepage_plan"] = "READY"

            snapshot = self._resolve_snapshot(store_id, state, force_theme)
            validation = validate_homepage_contract("theme_snapshot", snapshot)
            if validation["status"] != "VALID": return self._invalid(session, "Shopify 테마 정보 형식이 올바르지 않습니다.", validation["paths"])
            session.theme_snapshot = snapshot
            session.statuses["theme"] = snapshot.get("status", "NOT_CHECKED")
            if snapshot.get("status") == "CONNECTED": session.stage = "THEME_SNAPSHOT_READY"
            elif operation in PREVIEW_OPERATIONS:
                session.error = self._theme_error(snapshot)
                return session

            saved_featured = state.get("featured_products_plan")
            if (not isinstance(saved_featured, dict) or saved_featured.get("store_id") != str(store_id)
                    or saved_featured.get("status") != "READY" or not self._featured_plan_is_current(store_id, saved_featured)):
                saved_featured = FeaturedProductAssignmentService(db=self.db).create_plan(store_id,
                    mode="BALANCED_CATEGORIES", requested_count=4, include_existing=True,
                    force_remote=force_products, reader=self.product_reader)
            validation = validate_homepage_contract("featured_product_plan", saved_featured)
            if validation["status"] != "VALID": return self._invalid(session, "추천 상품 계획 형식이 올바르지 않습니다.", validation["paths"])
            session.featured_product_plan = saved_featured
            if saved_featured.get("status") == "READY":
                session.statuses["featured_products"] = "READY"
                session.stage = "FEATURED_PRODUCTS_READY"
            else:
                session.statuses["featured_products"] = saved_featured.get("status", "REVIEW_REQUIRED")
                if operation in PREVIEW_OPERATIONS:
                    session.error = {"status": "MISSING_PREREQUISITE", "message": "추천 상품 4개가 준비되지 않았습니다. 적격 상품과 read_products 권한을 확인하세요.",
                                     "reason": saved_featured.get("reasons"), "diagnostics": saved_featured.get("diagnostics")}
                    return session

            if operation in PREVIEW_OPERATIONS:
                if snapshot.get("status") != "CONNECTED":
                    session.error = self._theme_error(snapshot)
                    return session
                builder = normalize_text_value(snapshot.get("page_builder") or snapshot.get("builder") or snapshot.get("provider")).casefold()
                if any(name in builder for name in ("pagefly", "gempages", "shogun", "external")):
                    session.canonical_preview = {"status": "MANUAL_ACTION_REQUIRED", "store_id": str(store_id),
                        "current": snapshot.get("template"), "proposed": snapshot.get("template"),
                        "actions": [{"action": "MANUAL_ACTION_REQUIRED", "reason": "외부 페이지 빌더 화면은 자동 클릭하지 않습니다. Shopify/PageFly 편집기에서 수동으로 확인하세요."}],
                        "write_performed": False}
                    session.error = {"status": "MISSING_PREREQUISITE", "message": session.canonical_preview["actions"][0]["reason"]}
                    return session
                if operation == "FEATURED_PREVIEW":
                    from .homepage_automation import discover_homepage_sections, _hash
                    feature_preview = FeaturedProductAssignmentService(db=self.db).build_theme_preview(saved_featured, snapshot)
                    feature_preview.update(store_id=str(store_id), theme=snapshot.get("theme"),
                        template_filename=snapshot.get("template_filename"),
                        featured_products_plan_id=saved_featured.get("plan_id"))
                    section_discovery = discover_homepage_sections(snapshot.get("theme_files") or {})
                    category_supported = bool(section_discovery.get("category"))
                    hero_supported = bool(section_discovery.get("hero"))
                    preview = {**feature_preview,
                        "store_id": str(store_id), "theme": snapshot.get("theme"),
                        "template_filename": snapshot.get("template_filename"),
                        "featured_products_plan_id": saved_featured.get("plan_id"),
                        "featured_products_preview": feature_preview,
                        "discovery": {"hero": section_discovery.get("hero_status"),
                                      "category": section_discovery.get("category_status")},
                        "capabilities": {
                            "hero": "AUTO" if hero_supported else "MANUAL",
                            "categories": "AUTO" if category_supported else "MANUAL",
                            "featured_products": "AUTO" if feature_preview.get("status") == "PREVIEW" else "MANUAL",
                            "theme_template": "COMMENTED_JSON_SUPPORTED" if (snapshot.get("template_document") or {}).get("had_leading_comment") else "JSON_SUPPORTED",
                        },
                        "actions": ([{"action": "FEATURED_PRODUCTS_PREVIEW", "section_id": feature_preview.get("section_id"), "status": feature_preview.get("status")}]
                                    if feature_preview.get("status") == "PREVIEW" else [{"action": "MANUAL_ACTION_REQUIRED", "reason": feature_preview.get("reason")}]),
                        "write_performed": False}
                    preview["diff"] = {"before_hash": _hash(preview.get("current")), "proposed_hash": _hash(preview.get("proposed"))}
                    if not category_supported:
                        preview["manual_reasons"] = ["추천 상품 자동 미리보기 가능 · 카테고리 바로가기는 수동 확인 필요"]
                else:
                    preview = compose_homepage_preview(homepage_plan, snapshot, collection_plan,
                        collection_handles=mappings, featured_products_plan=saved_featured if saved_featured.get("status") == "READY" else None, db=self.db)
                session.canonical_preview = preview
                session.statuses["preview"] = preview.get("status", "NOT_READY")
                if preview.get("status") == "PREVIEW": session.stage = "HOMEPAGE_PREVIEW_READY"
                from .homepage_automation import discover_homepage_sections
                category_schema = discover_homepage_sections(snapshot.get("theme_files") or {}).get("category") or {}
                assignment_theme = {"sections": ([{"mode": category_schema.get("mode")}] if category_schema else []),
                                    "builder": snapshot.get("page_builder") or ""}
                mappings = {key: {"collection_key": key, "handle": handle, "remote_id": handle, "status": "VERIFIED"}
                            for key, handle in mappings.items()}
                session.assignment_preview = HomepageAssignmentService().build(
                    homepage={"hero": homepage_plan.get("hero"), "unrelated_sections": (snapshot.get("template") or {}).get("sections", {})},
                    collection_plan=collection_plan, mappings=mappings, approved_images=assets,
                    theme=assignment_theme, featured_products=saved_featured)
                session.assignment_preview["canonical_homepage_preview"] = preview
                session.assignment_preview["preview_id"] = preview.get("preview_id")
                if operation == "ASSIGNMENT_CHECK":
                    session.assignment_check = HomepageAssignmentService().checklist(session.assignment_preview,
                        desktop_checked=False, mobile_checked=False)
                    session.assignment_check["checks"].update(self._assignment_check(preview, saved_featured)["checks"])
                    session.assignment_check["preview_status"] = preview.get("status")
                    session.assignment_check["write_status"] = "NOT_RUN"
                    session.stage = "ASSIGNMENT_REVIEW_READY"
            return session
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            # Never expose bare type errors; retain exact field/error context for developer diagnostics.
            session.error = {"status": "INVALID_DATA" if isinstance(exc, (TypeError, AttributeError)) else "MISSING_PREREQUISITE",
                             "message": "홈페이지 준비 중 입력 형식 또는 필수 자료를 확인하지 못했습니다.",
                             "developer_path": f"HomepagePrerequisiteService.resolve:{type(exc).__name__}"}
            return session

    def _collection_handles(self, store_id: str) -> dict[str, str]:
        with connect(self.db) as con:
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shopify_collection_mappings'").fetchone()
            if not exists: return {}
            return {row["collection_key"]: row["handle"] for row in con.execute(
                "SELECT collection_key,handle FROM shopify_collection_mappings WHERE store_id=?", (str(store_id),)) if row["handle"]}

    def _featured_plan_is_current(self, store_id: str, plan: dict) -> bool:
        from .homepage_featured_products import _install as install_featured_schema
        install_featured_schema(self.db)
        with connect(self.db) as con:
            latest = con.execute("SELECT plan_id FROM homepage_featured_product_plans WHERE store_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (str(store_id),)).fetchone()
        return bool(latest and latest["plan_id"] == plan.get("plan_id"))

    def _collection_assets(self, store_id: str) -> dict:
        try:
            from .collection_images import approved_collection_images
            return {key: {**asset, "approval_status": "APPROVED"} for key, asset in approved_collection_images(store_id, db=self.db).items()}
        except (KeyError, TypeError, ValueError):
            return {}

    def _resolve_snapshot(self, store_id: str, state: dict, force: bool) -> dict:
        cached = state.get("snapshot")
        cached_at = state.get("snapshot_read_at")
        if not force and state.get("snapshot_store_id") == str(store_id) and isinstance(cached_at, str):
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached_at)).total_seconds()
                if age <= 300 and isinstance(cached, dict): return cached
            except (ValueError, TypeError): pass
        reader = self.theme_reader or self.theme_reader_factory(db=self.db)
        try:
            snapshot = reader.discover(store_id)
            if isinstance(cached, dict) and cached.get("status") == "CONNECTED" and snapshot.get("status") == "CONNECTED" and _hash(cached) != _hash(snapshot):
                invalidate_homepage_previews(store_id, reason="Shopify theme snapshot changed", db=self.db)
            state["snapshot"] = snapshot
            state["snapshot_store_id"] = str(store_id)
            state["snapshot_read_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return snapshot
        except Exception as exc:
            return {"status": "READ_FAILED", "theme_files": {}, "template": None, "reason": type(exc).__name__}

    @staticmethod
    def _theme_error(snapshot: dict) -> dict:
        if snapshot.get("status") == "MISSING_READ_SCOPE":
            message = "Shopify 테마 읽기 실패: read_themes 권한을 확인하세요."
        else:
            message = "Shopify 테마 정보를 읽지 못했습니다. 연결과 read_themes 권한을 확인하세요."
        return {"status": "MISSING_PREREQUISITE", "message": message, "reason": snapshot.get("reason") or snapshot.get("status")}

    @staticmethod
    def _invalid(session: HomepageSession, message: str, paths: list[str]) -> HomepageSession:
        session.error = {"status": "INVALID_DATA", "message": message, "developer_paths": paths}
        return session

    @staticmethod
    def _assignment_check(preview: dict, featured: dict) -> dict:
        canonical = preview.get("proposed") if isinstance(preview, dict) else None
        product_preview = preview.get("featured_products_preview") or {}
        section_id = product_preview.get("section_id")
        section = ((canonical or {}).get("sections") or {}).get(section_id) if section_id else None
        settings = (section or {}).get("settings") or {}
        selected = [x.get("shopify_product_id") for x in (featured or {}).get("items", [])]
        visible = bool(section and any(value == selected or (isinstance(value, list) and value == selected) for value in settings.values()))
        items = (featured or {}).get("items", [])
        checks = {"featured_products_4_of_4": len(items) == 4,
                  "featured_products_unique_ids_handles": len({x.get("shopify_product_id") for x in items}) == 4 and len({x.get("shopify_handle") for x in items}) == 4,
                  "featured_products_active": len(items) == 4 and all(x.get("remote_status") == "ACTIVE" for x in items),
                  "featured_products_images": len(items) == 4 and all(x.get("image_url") for x in items),
                  "featured_products_prices": len(items) == 4 and all(float(x.get("price") or 0) > 0 for x in items),
                  "featured_products_links": len(items) == 4 and all(x.get("shopify_handle") for x in items),
                  "featured_products_visible_in_proposal": visible,
                  "canonical_preview_current": preview.get("status") == "PREVIEW" and preview.get("featured_products_plan_id") == featured.get("plan_id"),
                  "not_stale": preview.get("status") != "STALE",
                  "desktop_human_check": False, "mobile_human_check": False,
                  "shopify_write_not_run": True}
        ready = all(checks.values())
        return {"status": "ASSIGNMENT_READY" if ready else "REVIEW_REQUIRED", "ready": ready,
                "checks": checks, "write_status": "NOT_RUN", "preview_status": preview.get("status")}
