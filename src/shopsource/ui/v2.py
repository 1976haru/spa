from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from ..connectors.spark_center_package import (
    confirm_spark_desktop_roundtrip, list_packages, mark_package,
    stage_package_for_spark_desktop,
)
from ..connectors.spark_desktop_staging import DatasetAlreadyExists
from ..capture.service import CaptureService
from ..capture.batch import BatchSourcingService
from ..capture.campaign import CampaignService
from ..collection_planner import CollectionPlanner
from ..shopify_collections import (DEV_DASHBOARD_CLIENT_CREDENTIALS, LEGACY_ADMIN_TOKEN,
    ShopifyGraphQLClient, ShopifyCollectionPublisher, get_connection as get_shopify_connection,
    get_shopify_token, save_connection as save_shopify_connection, save_shopify_token)
from ..shopify_auth import (ShopifyAuthService, delete_dev_credentials,
    verify_and_bind_app_profile, bind_existing_app_profile, list_app_profiles)
from ..collection_images import (ManualImageProvider, OpenAIImagesProvider, approve_collection_image,
    generate_collection_image, approved_collection_images)
from ..classifier import classify_store
from ..db import connect, get_store, init_db, upsert_store
from ..homepage_collections import HomepageCollectionService, ShopifyThemeReader, build_homepage_plan
from ..homepage_automation import (HomepageAutomationService, assignment_banner_check,
    assignment_category_check, build_homepage_plan as build_storefront_homepage_plan,
    build_homepage_preview as build_storefront_homepage_preview, compose_homepage_preview,
    discover_homepage_sections,
    approve_hero_asset, generate_hero_image, latest_hero_asset, register_manual_hero_asset,
    suggested_theme_image_ref, upload_approved_hero_asset, validate_homepage_image)
from ..homepage_assignment import HomepageAssignmentService, homepage_assignment_workflow
from ..homepage_featured_products import FeaturedProductAssignmentService
from ..automation import AutomationTaskError, WorkflowAutomationService, collection_prerequisite_workflow
from ..collection_prerequisite import CollectionPrerequisiteService, prepare_homepage_prerequisites
from ..prompt_assets import PromptAssetService
from ..local_image_studio import LocalImageStudioProvider
from ..runtime_doctor import PIL_MISSING_KO, dependency_doctor, repair_runtime_dependencies
from ..navigation import MegaMenuThemeService, NavigationService
from ..store_build import StoreBuildOrchestrator
from ..store_completion import DOMAINS, StoreCompletionService
from ..production import GATE_LABELS_KO, ProductionGoldenPathService
from ..production_runner import ProductionEvidenceRunner
from ..security import redact_text
from ..intelligence.keyword_engine import KeywordEngine
from ..paths import AMAZON_INBOX_DIR, EXPORT_DIR, STORE_DIR
from ..store_portfolio import production_bootstrap, selector_label, store_metadata
from ..importer import import_amazon_source
from ..sourcing.credentials import delete_api_key, get_api_key, save_api_key
from ..sourcing.engine import SourcingEngine, new_run_id
from ..sourcing.planner import CategoryPlanner
from ..sourcing.providers.keepa import KeepaProvider
from .v2_service import (
    bulk_override, clear_bulk_override, create_spark_package, create_store_profile,
    dashboard_data, get_app_setting, list_recent_errors, list_sourcing_runs, list_stores, set_app_settings,
    open_package, product_detail, product_page,
)
from .beginner import BEGINNER_NAV_GROUPS, GLOBAL_UI_CSS
from .production_control_center import (GATE_ACTION_REGISTRY, gate_board_rows,
    production_summary, resolve_gate_click)

NAV_ITEMS = [
    ("/production", "rocket_launch", "실전 스토어 완성"),
    ("/completion", "fact_check", "스토어 완성"),
    ("/source-safety", "health_and_safety", "소스 재고·가격 안전"),
    ("/homepage", "web", "Homepage Automation"),
    ("/build", "rocket_launch", "스토어 자동 구축"),
    ("/pilot", "science", "Cabin Tidy 파일럿"),
    ("/brand", "palette", "브랜드·로고·파비콘"),
    ("/", "dashboard", "대시보드"),
    ("/stores", "storefront", "스토어"),
    ("/sourcing", "travel_explore", "소싱"),
    ("/collections", "collections_bookmark", "컬렉션 자동화"),
    ("/navigation", "account_tree", "메가메뉴 자동화"),
    ("/products", "inventory_2", "상품"),
    ("/packages", "outbox", "패키지"),
    ("/history", "history", "기록"),
    ("/settings", "settings", "설정"),
]


def _workflow_service():
    """Use durable task handlers so a confirmed theme task cannot be a no-op."""
    def apply_homepage(task):
        checkpoint = task.get("checkpoint") or {}
        preview_id = checkpoint.get("preview_id")
        if not preview_id:
            raise AutomationTaskError("PREVIEW_STALE", "Homepage preview id missing")
        result = HomepageAutomationService().apply(preview_id, confirmed=True,
                                                   approved_assets=bool(checkpoint.get("assets_approved")))
        if result.get("status") != "VERIFIED":
            raise AutomationTaskError("CONFLICT", result.get("reason") or result.get("status") or "Theme write did not verify")
        return {"status": "SUCCEEDED", "message": "홈페이지 적용 후 Shopify에서 검증했습니다.",
                "backup_id": result.get("backup_id"), "theme_id": result.get("theme_id")}

    def verify_homepage(task):
        checkpoint = task.get("checkpoint") or {}
        preview_id = checkpoint.get("preview_id")
        if not preview_id:
            raise AutomationTaskError("PREVIEW_STALE", "Homepage preview id missing")
        result = HomepageAutomationService().verify(preview_id)
        if result.get("status") != "VERIFIED":
            raise AutomationTaskError("CONFLICT", result.get("reason") or result.get("status") or "Remote verification failed")
        return {"message": "홈페이지 원격 검증을 완료했습니다.", "theme_id": result.get("theme_id")}

    def generate_local_image(task):
        from ..local_image_studio import LocalImageStudioProvider
        checkpoint = task.get("checkpoint") or {}
        result = LocalImageStudioProvider().generate(checkpoint.get("job") or {})
        if result.get("status") not in {"SUCCEEDED", "PARTIAL"}:
            error = result.get("error") or {}
            code = str(error.get("code") or "IMAGE_PROVIDER_FAILED").upper()
            transient = code in {"TIMEOUT", "NETWORK_INTERRUPTION", "HTTP_429", "TEMPORARY_PROVIDER", "TEMPORARY_SERVER"}
            raise AutomationTaskError("BUSINESS_INPUT" if result.get("status") == "WAITING_FOR_CONFIGURATION" else code,
                                      error.get("message_ko") or result.get("message") or code, transient=transient)
        return {"status": result["status"], "candidates": result.get("candidates", []),
                "message": f"이미지 후보 {len(result.get('candidates', []))}개를 준비했습니다."}

    def validate_local_image(task):
        service = _workflow_service()
        generated = service.task_result(task["run_id"], "LOCAL_IMAGE_GENERATE").get("result") or {}
        candidates = generated.get("candidates") or []
        if not candidates:
            raise AutomationTaskError("CONFLICT", "검사를 통과한 이미지 후보가 없습니다.")
        from ..image_validation import inspect_image
        for candidate in candidates:
            result = inspect_image(candidate["path"], asset_type=(task.get("checkpoint") or {}).get("asset_type", "HERO_BANNER"))
            if not result.get("valid"):
                raise AutomationTaskError("CONFLICT", result.get("message_ko") or "이미지 검증에 실패했습니다.")
        return {"candidate_count": len(candidates), "message": "파일 형식, 크기, 비율 검사를 완료했습니다."}

    def approve_local_image(task):
        from ..local_image_studio import LocalImageStudioProvider
        from ..paths import EXPORT_DIR
        checkpoint = task.get("checkpoint") or {}
        choice = checkpoint.get("user_input") or {}
        candidate_id = choice.get("candidate_id")
        generated = _workflow_service().task_result(task["run_id"], "LOCAL_IMAGE_GENERATE").get("result") or {}
        candidates = generated.get("candidates") or []
        candidate = next((x for x in candidates if x.get("candidate_id") == candidate_id), None)
        if not candidate:
            raise AutomationTaskError("BUSINESS_INPUT", "승인할 후보를 찾지 못했습니다. 후보를 다시 확인하세요.")
        asset = checkpoint.get("asset") or {}
        job = checkpoint.get("job") or {}
        store_id = str(job.get("store_id") or "")
        if asset.get("asset_type") == "HERO_BANNER":
            plan_id = str(asset.get("plan_id") or "")
            if not plan_id:
                raise AutomationTaskError("BUSINESS_INPUT", "홈페이지 계획이 없어 Hero 이미지를 연결할 수 없습니다.")
            destination = EXPORT_DIR / "homepage_assets" / store_id / plan_id / "hero" / "local_studio"
        else:
            key = str(asset.get("collection_key") or "")
            if not key:
                raise AutomationTaskError("BUSINESS_INPUT", "컬렉션 식별 정보가 없어 이미지를 연결할 수 없습니다.")
            destination = EXPORT_DIR / "collection_images" / store_id / key / "local_studio"
        imported = LocalImageStudioProvider.import_candidate(candidate, destination)
        if asset.get("asset_type") == "HERO_BANNER":
            asset_row = register_manual_hero_asset(store_id, str(asset["plan_id"]), Path(imported["path"]).name,
                Path(imported["path"]).read_bytes(), provider="LOCAL_IMAGE_STUDIO")
            approve_hero_asset(asset_row["asset_id"])
        else:
            from ..collection_images import approve_collection_image, register_image_asset
            register_image_asset(store_id, str(asset["collection_key"]), imported["path"], provider="LOCAL_IMAGE_STUDIO",
                model="headless-bridge", alt_text=asset.get("alt_text_suggestion", ""), metadata={"sha256": imported["sha256"]})
            if not approve_collection_image(store_id, str(asset["collection_key"])):
                raise AutomationTaskError("CONFLICT", "컬렉션 이미지 검증을 마치지 못했습니다.")
        return {"asset_id": asset_row["asset_id"] if asset.get("asset_type") == "HERO_BANNER" else str(asset["collection_key"]),
                "candidate_id": candidate_id, "message": "추천 이미지 승인 및 로컬 자산 등록을 완료했습니다."}

    def ensure_collection(task):
        store_id = str((task.get("checkpoint") or {}).get("store_id") or
                       WorkflowAutomationService().status(task["run_id"])["store_id"])
        result = CollectionPrerequisiteService().ensure_collection_plan(store_id)
        if result.status != "READY" or not result.plan:
            raise AutomationTaskError("BUSINESS_INPUT", result.reason or "LOCAL_CATALOG_OR_CATEGORY_INPUT_REQUIRED")
        return {"plan_id": result.plan["plan_id"], "source": result.source,
                "created_at": result.created_at, "collection_count": result.plan["collection_count"]}

    return WorkflowAutomationService(handlers={"THEME_WRITE": apply_homepage, "VERIFY": verify_homepage,
        "LOCAL_IMAGE_GENERATE": generate_local_image, "LOCAL_IMAGE_VALIDATE": validate_local_image,
        "LOCAL_IMAGE_APPROVAL": approve_local_image, "ENSURE_COLLECTION_PLAN": ensure_collection})
STATUS_OPTIONS = ["ALL", "PRIMARY", "RESERVE_A", "RESERVE_B", "RESERVE_C", "LOW_RESERVE",
                  "HIGH_RESERVE", "REVIEW", "RESTRICTED", "ARCHIVED"]
PRODUCT_SOURCE_OPTIONS = ["ALL", "BROWSER_CAPTURE", "SPARK_STORAGE", "AMAZON_SOURCE_FOLDER", "KEEPA"]


def _safe_error(exc: Exception) -> str:
    return redact_text(exc, limit=500)


def product_row_from_event_args(args) -> dict:
    payload = args or {}
    if not isinstance(payload, dict):
        return {}
    row = payload.get("data", payload)
    return row if isinstance(row, dict) else {}


def selected_product_asins(rows: list[dict]) -> list[str]:
    return [str(row["asin"]) for row in rows if row.get("asin")]


def product_grid_options() -> dict:
    """Build the initial client options with empty data; rows are pushed by update()."""
    return {
        "columnDefs": [
            {"field": "thumbnail", "headerName": "Thumbnail", "width": 100,
             ":cellRenderer": "params => { try { if (!params.value || !params.value.startsWith('https://')) return ''; const img=document.createElement('img'); img.src=params.value; img.loading='lazy'; img.style.width='48px'; img.style.height='48px'; img.style.objectFit='contain'; img.onerror=() => { img.replaceWith(document.createTextNode('')); }; return img; } catch (_) { return ''; } }"},
            {"field": "asin", "headerName": "ASIN", "checkboxSelection": True, "headerCheckboxSelection": True},
            {"field": "title", "headerName": "Title", "flex": 2},
            {"field": "brand", "headerName": "Brand"}, {"field": "price", "headerName": "Price"},
            {"field": "source_kind", "headerName": "Source"}, {"field": "fit_score", "headerName": "Fit"},
            {"field": "price_status", "headerName": "Price Status"}, {"field": "risk_status", "headerName": "Risk"},
            {"field": "final_status", "headerName": "Final"}, {"field": "first_seen", "headerName": "First Seen"},
            {"field": "last_seen", "headerName": "Last Seen"},
        ],
        "rowData": [],
        "rowSelection": "multiple",
        "pagination": False,
        "defaultColDef": {"sortable": True, "filter": True, "resizable": True},
        ":getRowId": "params => params.data.asin",
    }


def update_product_grid(grid, rows: list[dict]) -> None:
    grid.options["rowData"] = rows
    grid.update()


class OperatorUI:
    """Local operator console. Business operations live in service modules."""

    def __init__(self, ui):
        self.ui = ui
        self._sync_store_profiles()
        self.stores = list_stores()
        self.current_store = self.stores[0]["store_id"] if self.stores else "001"
        self.session_api_key = None
        self.active_run_id = None
        self.package_selected_asins: list[str] = []
        self.package_selected_statuses: list[str] = []
        self._register_pages()

    @staticmethod
    def _sync_store_profiles():
        """Keep the v2 console aligned with the same profile files as the Tk UI."""
        init_db()
        for path in STORE_DIR.glob("*.json"):
            try:
                profile = json.loads(path.read_text(encoding="utf-8"))
                if profile.get("store_id") and profile.get("store_name"):
                    upsert_store(profile)
            except (OSError, json.JSONDecodeError, ValueError):
                continue

    def _register_pages(self):
        for path, _icon, title in NAV_ITEMS:
            self.ui.page(path)(lambda path=path, title=title: self._page(path, title))

    def _page(self, path: str, title: str):
        ui = self.ui
        init_db()
        ui.add_head_html(GLOBAL_UI_CSS)
        logging.getLogger().setLevel(getattr(logging, str(get_app_setting("log_level", "INFO")).upper(), logging.INFO))
        if get_app_setting("theme", "Light") == "Dark":
            ui.dark_mode().enable()
        else:
            ui.dark_mode().disable()
        ui.colors(primary="#2563eb", secondary="#475569", accent="#14b8a6",
                  positive="#16a34a", negative="#dc2626", warning="#d97706")
        ui.query("body").classes("bg-slate-50 ss-comfortable")
        with ui.left_drawer(value=True).classes("bg-slate-950 text-white w-64"):
            ui.label("ShopSource Studio").classes("text-xl font-bold px-3 py-5")
            ui.separator().classes("bg-slate-700")
            for group, entries in BEGINNER_NAV_GROUPS:
                if len(entries) == 1:
                    href, icon, label = entries[0]
                    ui.link(label, href).classes("w-full rounded-lg px-4 py-3 text-slate-200 hover:bg-slate-800" + (" bg-slate-800" if path == href else "")).props(f"icon={icon}")
                else:
                    with ui.expansion(group, icon=entries[0][1], value=any(path == x[0] for x in entries)).classes("w-full text-slate-200"):
                        for href, icon, label in entries:
                            ui.link(label, href).classes("w-full rounded-lg px-5 py-2 text-slate-200 hover:bg-slate-800" + (" bg-slate-800" if path == href else "")).props(f"icon={icon}")
            with ui.expansion("전문가 메뉴 보기", icon="settings", value=False).classes("w-full text-slate-300"):
                beginner_routes={entry[0] for _group,entries in BEGINNER_NAV_GROUPS for entry in entries}
                for href, icon, label in NAV_ITEMS:
                    if href not in beginner_routes:
                        ui.link(label, href).classes("w-full rounded-lg px-5 py-2 text-slate-300 hover:bg-slate-800").props(f"icon={icon}")
        with ui.header().classes("bg-white text-slate-900 border-b border-slate-200 items-center"):
            ui.label(title).classes("text-lg font-semibold")
            ui.space()
            options = {store["store_id"]: selector_label(store["profile"], store_metadata(store["store_id"])) for store in self.stores}
            ui.select(options, value=self.current_store, label="Store").classes("w-64").on_value_change(
                lambda event: self._set_store(event.value, path)
            )
        with ui.column().classes("w-full max-w-[1600px] mx-auto p-6 gap-6"):
            if path == "/completion": self._store_completion()
            elif path == "/production": self._production_golden_path()
            elif path == "/source-safety": self._source_safety()
            elif path == "/": self._dashboard()
            elif path == "/build": self._store_build()
            elif path == "/pilot": self._shopify_pilot()
            elif path == "/brand": self._brand_automation()
            elif path == "/stores": self._stores()
            elif path == "/sourcing": self._sourcing()
            elif path == "/collections": self._collections()
            elif path == "/navigation": self._navigation()
            elif path == "/homepage": self._homepage_automation()
            elif path == "/products": self._products()
            elif path == "/packages": self._packages()
            elif path == "/history": self._history()
            elif path == "/settings": self._settings()

    def _set_store(self, store_id, path):
        self.current_store = store_id
        self.ui.navigate.to(path)

    def _production_golden_path(self):
        ui = self.ui
        selected = next((row for row in self.stores if row["store_id"] == self.current_store), None)
        if selected:
            bootstrap = production_bootstrap(selected["profile"], store_metadata(self.current_store))
            with ui.card().classes("w-full border border-slate-200 p-4"):
                if bootstrap.get("role") == "GOLDEN_REFERENCE":
                    ui.label("001 | Cabin Tidy — GOLDEN REFERENCE").classes("text-xl font-bold")
                    ui.label(f"PAUSED_REFERENCE · ShopSource evidence {bootstrap['evidence']} = {bootstrap['progress_percent']}% · read-only reference")
                else:
                    ui.label("002 | Garage / Workshop — PRODUCTION BUILD").classes("text-xl font-bold")
                    ui.label(f"Brand: {bootstrap['brand']} · Reference: 001 Cabin Tidy · Shopify: {bootstrap['shopify']}")
                    ui.label(f"Current step: {bootstrap['current_step']} · Store 002 real production progress: {bootstrap['progress_percent']}%")
                    if bootstrap.get("blocker"):
                        ui.label(bootstrap["blocker"]).classes("text-orange-700 font-semibold")
        if selected and bootstrap.get("role") == "PRODUCTION_BUILD":
            self._heading("Store 002 Production Build", "현재 단계와 다음 실제 행동을 우선합니다. 코드 capability는 Store 002 실전 진행률에 합산하지 않습니다.")
        else:
            self._heading("Golden Reference", "Cabin Tidy는 비교용 quality reference입니다. 기존 evidence와 pilot/batch 상태를 변경하지 않습니다.")
        service = ProductionGoldenPathService()
        runner = ProductionEvidenceRunner(service=service)
        state = {"run": None, "selected_gate": None}
        new_run_button = ui.button("새 점검 시작", icon="add_circle").props("outline")
        with ui.row().classes("w-full flex-wrap gap-3"):
            start = ui.button("실전 점검 자동 진행", icon="rocket_launch").props("color=primary size=lg")
            ui.button("기존 점검 다시 열기", icon="history", on_click=lambda: load_latest()).props("outline size=lg")
        notice = ui.label("게이트를 열어 내용을 확인하는 것만으로 외부 검사는 시작되지 않습니다. 외부 작업은 별도의 승인 버튼이 필요합니다.").classes("ss-help")
        if selected and production_bootstrap(selected["profile"], store_metadata(self.current_store)).get("read_only"):
            new_run_button.props("disable")
            start.props("disable")
            notice.set_text("GOLDEN REFERENCE는 read-only입니다. 기존 pilot/batch를 완료 처리하거나 재실행하지 않습니다.")
        board = ui.element("div").classes("ss-gate-board w-full")
        action_dialog = ui.dialog()
        with action_dialog, ui.card().classes("w-[min(95vw,1000px)] max-h-[88vh] overflow-auto"):
            action_dialog_title = ui.label("실전 점검에서 필요한 확인").classes("ss-card-title")
            action_content = ui.column().classes("w-full gap-3")
            ui.button("닫기", on_click=action_dialog.close, icon="close").props("outline")
        with ui.card().classes("ss-production-summary w-full border border-blue-100 p-3"):
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-3"):
                summary = ui.label("아직 production 점검 run이 없습니다.").classes("ss-card-title")
                progress_report_label = ui.label("현재 단계: 아직 시작 전").classes("ss-help")
            with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                current_action_button = ui.button("현재 단계 해결하기", icon="task_alt", on_click=lambda: open_current_action()).props("color=primary")
                ui.button("전체 재검증", icon="refresh", on_click=lambda: begin()).props("outline")
                ui.button("기존 점검 새로고침", icon="history", on_click=lambda: load_latest()).props("outline")
        def render(run):
            state["run"] = run
            progress_state = service.progress_report(run["run_id"])
            store_profile = get_store(str(run["store_id"]))
            overview = production_summary(run, progress_state, store_profile)
            summary.set_text(f"Store {overview['store_id']} | {overview['store_name']} · 실전 진행률 {overview['percent']}% · 검증 {overview['verified']} / {overview['denominator']} · {overview['status']}")
            progress_report_label.set_text(f"현재 단계: {overview['current_label']} · 남은 증거 단계 {overview['remaining']}개\n{overview['progress_note']}")
            current_key = progress_state["current_stage"]
            current_action_button.set_text(GATE_ACTION_REGISTRY.get(current_key, {}).get("label", "현재 단계 해결하기"))
            rows = gate_board_rows(run, progress_state)
            board.clear()
            with board:
                for gate_row in rows:
                    classes = f"ss-gate-row {gate_row['status_class']}" + (" ss-gate-current" if gate_row["current"] else "")
                    with ui.card().classes(classes):
                        with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                            ui.label(f"{gate_row['number']:02d}").classes("ss-gate-number")
                            with ui.column().classes("grow gap-0"):
                                with ui.row().classes("items-center gap-2 flex-wrap"):
                                    ui.label(gate_row["label"]).classes("font-semibold")
                                    ui.badge(gate_row["status_ko"]).classes(gate_row["status_class"])
                                    if gate_row["current"]: ui.badge("현재 단계").classes("bg-blue-100 text-blue-900")
                                ui.label(gate_row["summary"]).classes("ss-help")
                            ui.button(gate_row["action_label"], icon="chevron_right",
                                      on_click=lambda key=gate_row["gate_key"]: open_gate_action(key)).props("flat dense")
        def load_latest():
            with connect() as con:
                row = con.execute("SELECT run_id FROM production_runs WHERE store_id=? ORDER BY updated_at DESC LIMIT 1", (self.current_store,)).fetchone()
            if row: render(service.get(row["run_id"]))
            else: ui.notify("이 스토어의 저장된 production 점검이 없습니다.", type="info")
        def begin():
            run = runner.run(self.current_store)
            render(run)
            ui.notify("읽기 전용 production evidence 확인을 진행했습니다. Shopify 쓰기는 실행되지 않았습니다.", type="positive")
        def open_gate_action(gate_key=None):
            run = state.get("run")
            if not run:
                ui.notify("먼저 실전 점검 자동 진행을 눌러 주세요.", type="warning"); return
            progress_state = service.progress_report(run["run_id"])
            key = resolve_gate_click(run, gate_key or state.get("selected_gate") or progress_state["current_stage"])
            state["selected_gate"] = key
            gate = next(item for item in run["gates"] if item["gate_key"] == key)
            from .production_control_center import STATUS_KO
            action_dialog_title.set_text(f"{gate['position'] + 1:02d}. {GATE_LABELS_KO.get(key, key)} · {STATUS_KO.get(gate.get('status'), '상태 확인 필요')}")
            action_content.clear()
            with action_content:
                ui.label("이 패널은 현재 gate와 무관하게 미리 볼 수 있습니다. 여기서 화면을 여는 것만으로 외부 검사나 쓰기는 실행되지 않습니다.").classes("ss-help")
                if gate.get("status") in {"READY", "READY_WITH_WARNINGS", "VERIFIED"}:
                    ui.label("저장된 증거").classes("font-semibold")
                    ui.label(f"상태: {gate.get('status')} · 확인 시각: {gate.get('updated_at') or '기록 없음'}")
                    with ui.expansion("확인된 세부 증거", value=False):
                        ui.code(json.dumps(gate.get("evidence") or {}, ensure_ascii=False, indent=2)).classes("w-full max-h-72 overflow-auto")
                else:
                    ui.label("왜 아직 완료되지 않았나요?").classes("font-semibold")
                    for blocker in (gate.get("blockers") or [])[:8]:
                        ui.label(f"• {blocker}").classes("ss-help")
                if key == "SOURCE_SAFETY":
                    from ..source_safety import FreeSourceSafetyService
                    free = FreeSourceSafetyService()
                    audit = free.evidence_audit(str(run["store_id"]))
                    draft = runner.free_source_safety_preflight(run["run_id"], "SOURCE_CHECK_PILOT")
                    source_status = free.full_source_status(str(run["store_id"]))
                    validation_ready = free.source_check_pilot_passed(str(run["store_id"]))
                    ui.label("무료 Spark + 로컬 브라우저 안전검사").classes("ss-card-title")
                    ui.label("Keepa는 기본 경로가 아닙니다. Spark 캡처를 먼저 확인하며 재고 증거가 없거나 오래된 상품은 브라우저에서 새로 확인해야 합니다.").classes("ss-help")
                    ui.label(f"MASTER {audit['master_total']} · PRIMARY {draft['primary_count']} · Source-check eligible {draft['source_check_eligible']} · 현재 pilot 대상 {draft['target_count']}")
                    ui.label(f"Source checked {source_status['checked']} · pending {source_status['pending']} · UNKNOWN {source_status['unknown']} · OOS {source_status['out_of_stock']} · error {source_status['errors']}")
                    ui.label(f"Spark evidence: URL {audit['source_url']} · ASIN {audit['asin']} · 가격 {audit['price']} · 수집시각 {audit['_collectedAt']} · quantity 필드 {audit['quantity']} · 명시적 재고 {audit['explicit_availability']} · 최근 occurrence {audit['latest_occurrence_timestamp'] or '없음'}")
                    ui.label("quantity는 재고로 간주하지 않습니다. 기존 Spark 수집 사실만으로 IN_STOCK 처리하지 않습니다.").classes("ss-help")
                    ui.label(f"무료 모드 · 예상 token 0 · 예상 비용 FREE_LOCAL · 최신 확인 {draft['freshness'].get('FRESH', 0)} · 오래됨 {draft['freshness'].get('STALE_WARNING', 0) + draft['freshness'].get('STALE_BLOCKED', 0)} · 미확인 {draft['freshness'].get('NEVER_VERIFIED', 0)}")

                    pilot = runner.find_open_free_browser_capture_batch(run["run_id"], "SOURCE_CHECK_PILOT", include_completed=True)
                    pilot_box = ui.column().classes("w-full rounded-lg border border-amber-200 bg-amber-50 p-3 gap-2")
                    with pilot_box:
                        pilot_heading = ui.label("무료 Source Check Pilot").classes("ss-card-title")
                        pilot_summary = ui.label()
                        pilot_worker = ui.label()
                        pilot_notice = ui.label().classes("ss-help")
                        with ui.row().classes("gap-2 flex-wrap"):
                            pilot_start = ui.button("10개 무료 검사 시작", icon="play_arrow").props("color=primary")
                            worker_show = ui.button("Amazon 작업 탭 보기", icon="open_in_new").props("outline")
                            pilot_apply = ui.button("캡처 결과 반영", icon="publish").props("outline")
                        with ui.row().classes("gap-2 flex-wrap"):
                            pilot_pause = ui.button("일시정지", on_click=lambda: control_pilot("PAUSE")).props("outline")
                            pilot_resume = ui.button("계속", on_click=lambda: control_pilot("RESUME")).props("outline")
                            pilot_retry = ui.button("현재 상품 다시 시도", on_click=lambda: control_pilot("RETRY")).props("outline")
                            pilot_stop = ui.button("중단", on_click=lambda: control_pilot("STOP")).props("outline color=negative")
                        extension_help = ui.row().classes("gap-2 flex-wrap")
                        with extension_help:
                            ui.button("확장프로그램 연결 방법", on_click=lambda: ui.navigate.to("/settings")).props("outline")
                            ui.button("설정 화면 열기", on_click=lambda: ui.navigate.to("/settings")).props("outline")
                            ui.button("다시 연결 확인", on_click=lambda: launch_free("SOURCE_CHECK_PILOT")).props("outline")
                            pilot_manual_fallback = ui.button("원본 상품 직접 열기", on_click=lambda: open_current_source()).props("outline")
                            pilot_manual_fallback.set_enabled(False)

                    def update_pilot_view(current):
                        if not current:
                            pilot_summary.set_text("상태: 준비 · 진행 0 / 10 · 작업 탭 없음")
                            pilot_worker.set_text("Browser Capture 확장프로그램은 시작 버튼을 누를 때 확인합니다.")
                            pilot_start.set_text("10개 무료 검사 시작")
                            pilot_start.set_enabled(draft["target_count"] > 0)
                            pilot_apply.set_enabled(False)
                            pilot_notice.set_text("ShopSource가 전용 Amazon 작업 탭에서 상품을 하나씩 확인합니다. CAPTCHA 또는 Amazon 확인 화면이 나타날 때만 사용자 조치가 필요합니다.")
                            return
                        total = int(current.get("target_count", 0))
                        captured = int(current.get("captured_count", 0))
                        browser_state = current.get("browser_status", "UNKNOWN")
                        applied = int(current.get("checked_count", 0)) > 0
                        status_text = "결과 반영됨" if applied else "완료" if captured >= total and total else "사용자 조치 필요" if browser_state == "PAUSED_NEEDS_USER" else "실행 중" if browser_state == "RUNNING" else "준비"
                        pilot_summary.set_text(f"상태: {status_text} · 진행 {captured} / {total} · 현재 {min(captured + 1, total) if captured < total else total}/{total} · 대기 {max(0, total-captured)}")
                        current_item = current.get("current_item") or {}
                        current_asin = str(current_item.get("asin") or "")
                        current_label = ("…" + current_asin[-4:]) if current_asin else "다음 상품 대기"
                        pilot_worker.set_text(f"현재 상품: {current_label} · Worker: {'작업 요청됨' if browser_state == 'RUNNING' else '대기/사용자 조치'} · Batch {current.get('browser_batch_run_id')}")
                        if browser_state == "PAUSED_NEEDS_USER":
                            pilot_notice.set_text("Amazon 확인 화면 또는 확장 연결 확인이 필요합니다. 직접 CAPTCHA를 우회하지 말고 브라우저에서 확인하세요.")
                        elif applied:
                            counts = current.get("observation_counts", {})
                            pilot_notice.set_text(f"반영 결과 · IN_STOCK {counts.get('IN_STOCK', 0)} · UNKNOWN {counts.get('UNKNOWN', 0)} · OOS {counts.get('OUT_OF_STOCK', 0)} · 가격 누락/오류는 상세 결과에서 확인하세요. 전체 G2 완료를 뜻하지 않습니다.")
                        elif captured >= total and total:
                            pilot_notice.set_text("캡처 완료 — 결과 반영 버튼을 눌러 확인된 항목만 저장하세요. 전체 G2 통과로 자동 처리하지 않습니다.")
                        else:
                            pilot_notice.set_text("ShopSource가 전용 Amazon 작업 탭에서 상품을 하나씩 확인합니다. CAPTCHA 또는 Amazon 확인 화면이 나타날 때만 사용자 조치가 필요합니다.")
                        pilot_start.set_text("10개 무료 검사 계속")
                        pilot_start.set_enabled(not applied and browser_state not in {"DONE", "CANCELLED"} and captured < total)
                        pilot_apply.set_enabled(bool(not applied and total and captured >= total))

                    update_pilot_view(pilot)

                    def poll_pilot_state():
                        try:
                            update_pilot_view(runner.find_open_free_browser_capture_batch(run["run_id"], "SOURCE_CHECK_PILOT", include_completed=True))
                        except Exception as exc:
                            # Keep the operator panel usable while surfacing that local status could not be read.
                            pilot_notice.set_text(f"진행 상태를 새로 읽지 못했습니다 ({type(exc).__name__}). 잠시 뒤 다시 확인하세요.")

                    ui.timer(2.0, poll_pilot_state)

                    async def launch_free(kind):
                        try:
                            from .browser_capture_bridge import start_free_capture
                            launched = await start_free_capture(runner, run["run_id"], kind, ui)
                            result, bridge = launched["batch"], launched["bridge"]
                            browser_run_id = launched["browser_batch_run_id"]
                            current = runner.find_open_free_browser_capture_batch(run["run_id"], kind)
                            update_pilot_view(current)
                            if bridge.get("state") == "ACKNOWLEDGED":
                                pilot_worker.set_text(f"Worker 요청됨 · Batch {browser_run_id}")
                                pilot_manual_fallback.set_enabled(False)
                                ui.notify("기존 단일 Amazon 작업 탭에 첫 상품 열기를 요청했습니다.", type="positive")
                            else:
                                pilot_notice.set_text("Browser Capture 확장프로그램 연결이 필요합니다. 확장 설치·활성화 및 로컬 연결 상태를 확인한 뒤 다시 연결 확인을 누르세요.")
                                pilot_manual_fallback.set_enabled(True)
                                ui.notify("Browser Capture 확장프로그램 연결이 필요합니다.", type="warning")
                            if result.get("reused"):
                                ui.notify("새 batch를 만들지 않고 기존 미완료 파일럿을 이어갑니다.", type="info")
                        except Exception as exc:
                            pilot_notice.set_text(f"검사 시작을 완료하지 못했습니다 ({type(exc).__name__}). 설정을 확인하고 다시 연결해 주세요.")
                            pilot_manual_fallback.set_enabled(True)
                            ui.notify(f"무료 Source 검사 시작 실패 ({type(exc).__name__}).", type="negative")

                    pilot_start.on("click", lambda: launch_free("SOURCE_CHECK_PILOT"))
                    worker_show.on("click", lambda: show_pilot_worker())
                    pilot_apply.on("click", lambda: apply_pilot_capture())

                    async def show_pilot_worker():
                        current = runner.find_open_free_browser_capture_batch(run["run_id"], "SOURCE_CHECK_PILOT")
                        if not current:
                            ui.notify("열려 있는 Source Check 작업이 없습니다.", type="info"); return
                        from .browser_capture_bridge import request_worker_show
                        response = await request_worker_show(ui, current["browser_batch_run_id"])
                        if response.get("state") != "ACKNOWLEDGED":
                            pilot_notice.set_text("Browser Capture 확장프로그램 연결이 필요합니다. 연결 방법을 확인해 주세요.")
                            pilot_manual_fallback.set_enabled(True)
                            ui.notify("확장프로그램 응답을 받지 못했습니다.", type="warning")

                    def open_current_source():
                        current = runner.find_open_free_browser_capture_batch(run["run_id"], "SOURCE_CHECK_PILOT")
                        if not current:
                            ui.notify("열 수 있는 원본 상품이 없습니다.", type="info"); return
                        item = current.get("current_item") or {}
                        asin = item.get("asin")
                        if not asin:
                            ui.notify("현재 상품 링크를 찾지 못했습니다.", type="warning"); return
                        with connect() as con:
                            row = con.execute("SELECT search_payload_json FROM browser_capture_candidates WHERE run_id=? AND asin=? ORDER BY id DESC LIMIT 1",
                                (current["capture_run_id"], asin)).fetchone()
                        payload = json.loads(row["search_payload_json"] or "{}") if row else {}
                        url = str(payload.get("url") or "")
                        if not url.startswith(("https://www.amazon.com/", "https://amazon.com/")):
                            ui.notify("안전한 Amazon 상품 링크가 없어 직접 열 수 없습니다.", type="warning"); return
                        ui.run_javascript("window.open(" + json.dumps(url) + ", '_blank', 'noopener')")

                    async def control_pilot(action):
                        current = runner.find_open_free_browser_capture_batch(run["run_id"], "SOURCE_CHECK_PILOT")
                        if not current:
                            ui.notify("진행 중인 무료 파일럿이 없습니다.", type="info"); return
                        from ..capture.batch import BatchSourcingService
                        batches = BatchSourcingService()
                        try:
                            if action == "PAUSE":
                                if current["browser_status"] not in {"PAUSED", "PAUSED_NEEDS_USER"}:
                                    await asyncio.to_thread(batches.action, current["browser_batch_run_id"], "PAUSE")
                            elif action == "RESUME":
                                await asyncio.to_thread(runner.resume_free_browser_capture_batch, current["browser_batch_run_id"])
                                from .browser_capture_bridge import request_batch_open_next
                                response = await request_batch_open_next(ui, current["browser_batch_run_id"])
                                if response.get("state") != "ACKNOWLEDGED":
                                    pilot_notice.set_text("Browser Capture 확장프로그램 연결이 필요합니다. 연결 방법을 확인해 주세요.")
                            elif action == "STOP":
                                await asyncio.to_thread(batches.action, current["browser_batch_run_id"], "CANCEL")
                                await asyncio.to_thread(runner.free_source_batch_control, run["run_id"], "STOP")
                            elif action == "RETRY":
                                if current["browser_status"] == "DONE_WITH_ERRORS":
                                    await asyncio.to_thread(batches.action, current["browser_batch_run_id"], "RETRY")
                                elif (current.get("current_item") or {}).get("state") == "DETAIL_OPENED":
                                    await asyncio.to_thread(batches.recover_open_item, current["browser_batch_run_id"])
                                else:
                                    await asyncio.to_thread(runner.resume_free_browser_capture_batch, current["browser_batch_run_id"])
                                from .browser_capture_bridge import request_batch_open_next
                                await request_batch_open_next(ui, current["browser_batch_run_id"])
                            refreshed = runner.find_open_free_browser_capture_batch(run["run_id"], "SOURCE_CHECK_PILOT")
                            update_pilot_view(refreshed)
                        except Exception as exc:
                            ui.notify(f"파일럿 제어 실패 ({type(exc).__name__}).", type="warning")

                    async def apply_pilot_capture():
                        current = runner.find_open_free_browser_capture_batch(run["run_id"], "SOURCE_CHECK_PILOT")
                        if not current or int(current.get("captured_count", 0)) < int(current.get("target_count", 0)):
                            ui.notify("모든 파일럿 캡처가 끝난 뒤 결과를 반영할 수 있습니다. 미완료 항목은 PENDING으로 유지됩니다.", type="warning"); return
                        try:
                            result = await asyncio.to_thread(runner.apply_free_browser_capture_results,
                                run["run_id"], current["release_batch_id"], confirmed=True)
                            update_pilot_view(runner.find_open_free_browser_capture_batch(run["run_id"], "SOURCE_CHECK_PILOT", include_completed=True))
                            render(result["production_run"]); open_gate_action()
                            ui.notify(f"캡처 결과 반영: {result.get('verified_items', 0)}개 fresh IN_STOCK · 나머지는 검토 필요", type="positive" if result.get("status") == "COMPLETE" else "warning")
                        except Exception as exc:
                            ui.notify(f"Browser Capture 결과 반영 실패 ({type(exc).__name__}).", type="negative")

                    if draft["target_count"] == 0:
                        ui.label("검사할 미처리 PRIMARY source 후보가 없습니다. 분류·ASIN·원본 URL 또는 기존 batch 상태를 확인하세요.").classes("ss-help")
                    candidate_rows = free.unprocessed_source_candidates(str(run["store_id"]))
                    candidate_options = {int(row["master_product_id"]): f"{row.get('asin') or 'ASIN 없음'} · {row.get('title') or '제목 없음'}" for row in candidate_rows}
                    validation_selection = ui.select(candidate_options, label="검증 batch에 넣을 업로드 상품 선택 (100~200개)", multiple=True).classes("w-full")
                    async def run_validation_batch():
                        ids = validation_selection.value or []
                        if not 100 <= len(ids) <= 200:
                            ui.notify("실제 다음 업로드에 넣을 PRIMARY 상품을 100~200개 선택하세요.", type="warning"); return
                        try:
                            result = await asyncio.to_thread(runner.prepare_free_browser_capture_batch,
                                run["run_id"], "SOURCE_VALIDATION_BATCH", selected_product_ids=ids, confirmed=True)
                            browser_run_id = result["browser_batch_run_id"]
                            await asyncio.to_thread(runner.resume_free_browser_capture_batch, browser_run_id)
                            from .browser_capture_bridge import request_batch_open_next
                            bridge = await request_batch_open_next(ui, browser_run_id)
                            ui.notify("Validation batch 첫 상품 작업을 요청했습니다." if bridge.get("state") == "ACKNOWLEDGED" else "Browser Capture 확장프로그램 연결이 필요합니다.", type="positive" if bridge.get("state") == "ACKNOWLEDGED" else "warning")
                            render(runner.service.get(run["run_id"])); open_gate_action()
                        except Exception as exc:
                            ui.notify(f"검증 batch 준비 실패 ({type(exc).__name__}).", type="negative")
                    ui.button("100~200개 Source Validation Batch", on_click=run_validation_batch).props("outline" + (" disable" if not validation_ready or len(candidate_options) < 100 else ""))
                    ui.button("다음 Source Batch", on_click=lambda: launch_free("REMAINING_SOURCE_CHECK")).props("outline" + (" disable" if not validation_ready or not candidate_options else ""))
                    ui.label("Shopify 10개 DRAFT 업로드 파일럿은 G14에서 진행합니다.").classes("ss-help")
                    with ui.expansion("선택 유료 Provider · Keepa (무료 기본 검사에는 불필요)").classes("w-full"):
                        ui.label("Keepa profile은 기존 호환을 위해 유지됩니다. Keepa 미설정은 정상이며 무료 G2 경로를 막지 않습니다. 사용자가 선택하지 않는 한 Keepa API 호출은 하지 않습니다.").classes("ss-help")
                        keepa_key = ui.input("Keepa API key · 선택 유료 사용 시에만", password=True).props("autocomplete=new-password").classes("w-full")
                        keepa_profile_id = ui.input("공유 Keepa profile ID", value="keepa-production-shared").classes("w-full")
                        async def save_optional_keepa():
                            if not keepa_key.value:
                                ui.notify("선택한 유료 Keepa 경로에 사용할 때만 API key를 입력하세요.", type="warning"); return
                            try:
                                await asyncio.to_thread(runner.source_profiles.save_keepa_profile,
                                    keepa_profile_id.value, "Optional paid Keepa Source Safety", keepa_key.value,
                                    store_id=str(run["store_id"]))
                                keepa_key.value = ""
                                ui.notify("Keepa credential을 Windows Credential Manager에 저장했습니다. G2 기본 provider는 계속 무료입니다.", type="positive")
                            except Exception as exc:
                                ui.notify(f"선택 유료 profile 저장 실패 ({type(exc).__name__}).", type="negative")
                        ui.button("선택 유료 Keepa profile 저장", on_click=save_optional_keepa).props("outline")
                    if draft.get("target_product_ids"):
                        with ui.expansion("DRAFT 후보 원본 링크와 현재 로컬 evidence"):
                            inspected = free.inspect_local_batch(str(run["store_id"]), "SOURCE_CHECK_PILOT")
                            for item in inspected["items"]:
                                ui.label(f"{item['asin']} · source 재고 {item['availability']} · 가격 {item['source_price'] or '확인 필요'} · freshness {item['freshness']}")
                                if item.get("source_url"):
                                    ui.link("원본 상품을 브라우저에서 직접 확인", item["source_url"], new_tab=True)
                if key == "SOURCE_SAFETY_LEGACY":
                    profiles = runner.source_profiles
                    preflight = runner.source_provider_preflight(run["run_id"])
                    ui.label("G2 Source Safety | Provider 사전점검").classes("ss-card-title")
                    ui.label(f"Provider: {preflight.get('provider_status', 'Keepa / NOT CONFIGURED')} · Credential: {'YES' if preflight.get('credential_present') else 'NO'} · Health: {preflight.get('health', 'NOT_CHECKED')}")
                    ui.label(f"Tokens left: {preflight.get('tokensLeft', '확인 전')} · 예상 tokens: {preflight.get('estimated_tokens', 0)} · target: {preflight.get('full_due_count', 0)} · batches: {preflight.get('batch_count', 0)} · 비용: UNKNOWN")
                    ui.label(f"Fresh {preflight.get('fresh', 0)} · Stale {preflight.get('stale', 0)} · Never verified {preflight.get('never_verified', 0)} · 마지막 health: {preflight.get('last_health_check') or '없음'}").classes("ss-help")
                    ui.label("G1 소싱 품질과 G2 원본 재고·가격 검사는 별도 단계입니다. Keepa는 G2 확인용이며 상품 후보 소싱 provider를 바꾸지 않습니다.").classes("ss-help")
                    api_key_input = ui.input("Keepa API key (Windows Credential Manager에 저장)").props("type=password autocomplete=new-password").classes("w-full")
                    profile_id_input = ui.input("공유 Provider Profile ID", value="keepa-production-shared").classes("w-full")
                    existing_profiles = {item["profile_id"]: item["display_name"] for item in profiles.profiles()}
                    if existing_profiles:
                        selected_profile = ui.select(existing_profiles, label="기존 공유 Provider Profile 재사용")
                        def bind_existing_profile():
                            try:
                                profiles.bind(str(run["store_id"]), selected_profile.value)
                                ui.notify("선택한 공유 Provider Profile을 Store에 연결했습니다. API key 재입력은 필요 없습니다.", type="positive")
                                render(runner.run(str(run["store_id"]), run_id=run["run_id"]))
                                open_gate_action()
                            except Exception as exc:
                                ui.notify(f"Profile 연결 실패 ({type(exc).__name__}).", type="warning")
                        ui.button("기존 Profile 연결", on_click=bind_existing_profile).props("outline")
                    async def save_provider():
                        try:
                            if not api_key_input.value:
                                ui.notify("Keepa key를 입력하세요.", type="warning"); return
                            await asyncio.to_thread(profiles.save_keepa_profile, profile_id_input.value,
                                "Keepa Production Source Safety", api_key_input.value, store_id=str(run["store_id"]))
                            api_key_input.value = ""
                            ui.notify("Provider profile을 안전하게 저장했습니다. key 원문은 DB에 저장하지 않습니다.", type="positive")
                            render(runner.run(str(run["store_id"]), run_id=run["run_id"]))
                            open_gate_action()
                        except Exception as exc:
                            ui.notify(f"Provider profile 저장 실패 ({type(exc).__name__}). Credential Manager 설정을 확인하세요.", type="negative")
                    ui.button("Provider 설정 저장", on_click=save_provider).props("outline")
                    async def check_provider_health():
                        try:
                            result = await asyncio.to_thread(runner.source_provider_health_check, run["run_id"])
                            ui.notify(f"Keepa health: {result.get('health')} · tokensLeft {result.get('tokensLeft', '확인 불가')}", type="positive" if result.get("health") == "PASS" else "warning")
                            render(runner.run(str(run["store_id"]), run_id=run["run_id"]))
                            open_gate_action()
                        except Exception as exc:
                            ui.notify(f"Health 확인 실패 ({type(exc).__name__}). key/네트워크 상태를 확인하세요.", type="negative")
                    ui.button("Health check", on_click=check_provider_health).props("outline")
                    refreshed = runner.source_provider_preflight(run["run_id"], pilot=True)
                    pilot_enabled = bool(refreshed.get("can_run_pilot"))
                    full_enabled = bool(refreshed.get("can_run_full"))
                    async def run_provider_pilot():
                        try:
                            result = await asyncio.to_thread(runner.run_source_provider_pilot, run["run_id"], confirmed=True)
                            ui.notify(f"Provider pilot: {result['status']} · {result['provider_pilot']['checked']}개 검사 · UNKNOWN {result['provider_pilot']['unknown']}", type="positive" if result["status"] == "PASS" else "warning")
                            render(result["production_run"])
                            open_gate_action()
                        except Exception as exc:
                            ui.notify(f"Provider pilot 중단 ({type(exc).__name__}). G2는 완료 처리되지 않았습니다.", type="warning")
                    ui.button("100개 Provider Pilot", on_click=run_provider_pilot).props("color=primary" + ("" if pilot_enabled else " disable"))
                    ui.label("Provider Pilot은 최대 100개·1회 batch만 검사하며, 성공해도 G2 전체 완료가 아닙니다.").classes("ss-help")
                    async def approve_full_source():
                        dialog = ui.dialog()
                        with dialog, ui.card():
                            ui.label(f"전체 due 상품 {refreshed.get('full_due_count', 0)}개를 Keepa로 확인합니다. 예상 token {refreshed.get('full_due_count', 0)}개, 금액 비용은 확인할 수 없습니다.")
                            with ui.row():
                                async def do_run():
                                    dialog.close()
                                    try:
                                        result = await asyncio.to_thread(runner.confirm_source_audit, run["run_id"], confirmed=True)
                                        render(result["production_run"])
                                    except Exception as exc:
                                        ui.notify(f"Full Source Audit을 시작하지 못했습니다 ({type(exc).__name__}). G2는 승인되지 않았습니다.", type="warning")
                                ui.button("취소", on_click=dialog.close).props("outline")
                                ui.button("Full Audit 승인", on_click=do_run).props("color=negative")
                        dialog.open()
                    ui.button("Full Source Audit", on_click=approve_full_source).props("color=negative" + ("" if full_enabled else " disable"))
                    with ui.row().classes("gap-2"):
                        ui.button("Pause", on_click=lambda: runner.source_audit_control(run["run_id"], "PAUSE")).props("outline")
                        async def resume_provider_audit():
                            try:
                                result = await asyncio.to_thread(runner.source_audit_control, run["run_id"], "RESUME")
                                render(result["production_run"])
                            except Exception as exc: ui.notify(f"Resume 불가 ({type(exc).__name__}).", type="warning")
                        ui.button("Resume", on_click=resume_provider_audit).props("outline")
                        ui.button("Stop", on_click=lambda: runner.source_audit_control(run["run_id"], "STOP")).props("outline color=negative")
                        async def retry_provider_audit():
                            try:
                                result = await asyncio.to_thread(runner.source_audit_control, run["run_id"], "RETRY_FAILED")
                                render(result["production_run"])
                            except Exception as exc: ui.notify(f"Retry 실패 ({type(exc).__name__}).", type="warning")
                        ui.button("Retry failed", on_click=retry_provider_audit).props("outline")
                if key == "SOURCE_SAFETY_LEGACY":
                    preview = runner._source(str(run["store_id"]))
                    ui.label(f"대상 {preview.get('target_count', 0)}개 · batch {preview.get('estimated_batches', 0)}개 · 추정 token {preview.get('estimated_tokens', 0)} · 금액 비용 견적은 provider 미연결 시 확인 불가")
                    ui.label("승인 시 설정된 source provider를 호출합니다. provider 미설정이면 외부 호출 없이 멈춥니다.").classes("ss-help")
                    async def approve_source():
                        try:
                            result = await asyncio.to_thread(runner.confirm_source_audit, run["run_id"], confirmed=True)
                            render(result["production_run"])
                        except Exception as exc: ui.notify(f"실행 전 중단: {exc}", type="warning")
                    ui.button("실제 Source 안전검사 승인", on_click=approve_source).props("color=negative")
                    with ui.row().classes("gap-2"):
                        ui.button("일시정지", on_click=lambda: runner.source_audit_control(run["run_id"], "PAUSE")).props("outline")
                        async def resume_source():
                            try:
                                result = await asyncio.to_thread(runner.source_audit_control, run["run_id"], "RESUME")
                                render(result["production_run"])
                            except Exception as exc: ui.notify(f"재개하지 못했습니다: {exc}", type="warning")
                        ui.button("계속", on_click=resume_source).props("outline")
                        ui.button("중단", on_click=lambda: runner.source_audit_control(run["run_id"], "STOP")).props("outline color=negative")
                        async def retry_source():
                            try:
                                result = await asyncio.to_thread(runner.source_audit_control, run["run_id"], "RETRY_FAILED")
                                render(result["production_run"])
                            except Exception as exc: ui.notify(f"재시도하지 못했습니다: {exc}", type="warning")
                        ui.button("실패 항목 재시도", on_click=retry_source).props("outline")
                elif key == "PRODUCT_CONTENT":
                    evidence = runner._content(str(run["store_id"]))
                    counts = evidence.get("counts", {})
                    ui.label("상품 설명·검색 정보 준비 (로컬 초안)").classes("ss-card-title")
                    ui.label(f"대상 {counts.get('total', 0)}개 · 보완 필요 {counts.get('content_review', 0)}개 · 초안 저장 {counts.get('drafted', 0)}개").classes("font-semibold")
                    ui.label("아래 내용은 사용자가 제공한 로컬 초안으로만 저장됩니다. MASTER 원본 데이터나 Shopify는 변경하지 않습니다. 확인되지 않은 제품 사실은 입력하지 마세요.").classes("ss-help")
                    with ui.expansion("항목별 누락 수", value=True):
                        for field, missing_count in (counts.get("missing_by_field") or {}).items():
                            ui.label(f"{field}: {missing_count}개 확인 필요").classes("ss-help")
                    queue = evidence.get("review_queue") or []
                    options = {str(row["product_id"]): f"{row.get('asin') or 'ASIN 없음'} · {row.get('title') or '제목 없음'} · 누락 {len(row['missing_fields'])}개" for row in queue}
                    if options:
                        selected_product = ui.select(options, label="보완할 상품 선택").classes("w-full")
                        draft_fields = {
                            "storefront_title": ui.input("표시용 상품명").classes("w-full"),
                            "description": ui.textarea("상품 설명 (원본에서 확인된 사실만)").classes("w-full"),
                            "features": ui.textarea("주요 특징 (한 줄에 하나, 근거가 있는 내용만)").classes("w-full"),
                            "variant_summary": ui.input("옵션/variant 요약 (옵션이 없는 경우 '단일 옵션 확인' 등 실제 확인 내용)").classes("w-full"),
                            "seo_title": ui.input("검색 제목").classes("w-full"),
                            "seo_description": ui.textarea("검색 설명").classes("w-full"),
                            "handle": ui.input("상품 URL handle").classes("w-full"),
                        }
                        variant_reviewed = ui.checkbox("옵션/variant를 원본과 대조했고 실제 구성만 확인했습니다.")
                        facts_confirmed = ui.checkbox("입력한 제목·설명·특징은 원본 자료로 확인했습니다.")
                        def load_selected_content(_=None):
                            try:
                                saved = runner.get_content_draft(str(run["store_id"]), int(selected_product.value)) if selected_product.value else {}
                            except (TypeError, ValueError): saved = {}
                            for name, field in draft_fields.items(): field.value = saved.get(name, "")
                            variant_reviewed.value = bool(saved.get("variant_reviewed"))
                            facts_confirmed.value = bool(saved.get("source_facts_confirmed"))
                        selected_product.on_value_change(load_selected_content)
                        def save_and_recheck_content():
                            if not selected_product.value:
                                ui.notify("먼저 보완할 상품을 선택하세요.", type="warning"); return
                            try:
                                result = runner.save_content_draft(run["run_id"], int(selected_product.value),
                                    {name: field.value for name, field in draft_fields.items()} | {"variant_reviewed": variant_reviewed.value},
                                    source_facts_confirmed=facts_confirmed.value, confirmed=True)
                                render(result["production_run"])
                                open_gate_action("PRODUCT_CONTENT")
                                ui.notify("로컬 초안을 저장하고 상품 설명 gate를 다시 확인했습니다. MASTER와 Shopify는 변경하지 않았습니다.", type="positive")
                            except Exception as exc: ui.notify(f"로컬 초안을 저장하지 못했습니다: {exc}", type="warning")
                        ui.button("저장 후 재검증", on_click=save_and_recheck_content).props("color=primary")
                    else:
                        ui.label("현재 production 후보의 로컬 초안은 모두 준비되어 있습니다. 증거가 바뀌면 재검증하세요.").classes("ss-help")
                    with ui.expansion("미완료 상품 목록", value=False):
                        for row in queue[:50]:
                            ui.label(f"{row.get('asin') or 'ASIN 없음'} · {row.get('title') or '제목 없음'} · {', '.join(row['missing_fields'])}").classes("ss-help")
                    ui.button("저장된 내용 gate 재검증", on_click=lambda: (render(runner.run(str(run["store_id"]), run_id=run["run_id"])), open_gate_action("PRODUCT_CONTENT"))).props("outline")
                elif key == "PRODUCT_MEDIA":
                    checks = {}
                    for row in runner._catalog_rows(str(run["store_id"]))[:50]:
                        with ui.row().classes("items-center"):
                            checks[row["product_id"]] = ui.checkbox()
                            ui.label(f"{row.get('asin') or 'ASIN 없음'} · {row.get('product_id')} · {row.get('classification')}")
                    policy = ui.select(["SUPPLIER_AUTHORIZED", "MERCHANT_OWNED", "LICENSED", "MANUAL_REVIEW_REQUIRED", "NO_RIGHTS_CONFIRMED"], value="MANUAL_REVIEW_REQUIRED", label="선택 상품의 검토 결과")
                    notes = ui.input(label="검토 메모 (선택)")
                    def save_rights():
                        selected = [pid for pid, box in checks.items() if box.value]
                        if not selected: ui.notify("검토할 상품을 선택하세요.", type="warning"); return
                        result = runner.review_media_rights(run["run_id"], selected, policy.value, confirmed=True,
                                                           notes={pid: notes.value or "" for pid in selected})
                        render(result["production_run"])
                    ui.button("선택한 상품의 권리 검토 저장", on_click=save_rights).props("color=primary")
                    ui.label("전체 승인 기능은 없습니다. GENERATED_LIFESTYLE_ONLY는 상품 이미지 권리를 대신하지 않습니다.").classes("ss-help")
                elif key == "PRICING_MARGIN":
                    fields = {name: ui.input(label=label, value=value) for name, label, value in (
                        ("currency", "통화", "USD"), ("source_cost_buffer_fixed", "원가 고정 buffer", "0"),
                        ("source_cost_buffer_percent", "원가 비율 buffer", "0"), ("min_margin_amount", "최소 마진 금액", ""),
                        ("min_margin_percent", "최소 마진 %", ""), ("unknown_fee_handling", "알 수 없는 수수료 처리", "BLOCK"),
                        ("warning_source_price_change_percent", "원본 가격 변동 경고 %", ""))}
                    ui.label("추천/초기값은 승인된 정책이 아닙니다. 자동 가격 변경은 사용할 수 없습니다.").classes("ss-help")
                    def save_price():
                        try:
                            payload = {name: (box.value if name in {"currency", "unknown_fee_handling"} else float(box.value)) for name, box in fields.items()}
                            payload["auto_reprice_enabled"] = False
                            render(runner.save_pricing_policy(run["run_id"], payload, confirmed=True))
                        except Exception as exc: ui.notify(f"정책을 저장하지 못했습니다: {exc}", type="warning")
                    ui.button("가격 정책 저장 및 재검사", on_click=save_price).props("color=primary")
                elif key == "PAGES_POLICIES":
                    evidence = next(g["evidence"] for g in run["gates"] if g["gate_key"] == key)
                    ui.label("Shopify에서 읽은 페이지/정책 상태").classes("ss-card-title")
                    for name, item in (evidence.get("items") or {}).items():
                        ui.label(f"{name}: {item.get('status')} · {item.get('count', 0)}개 remote 항목")
                    ui.label("필요한 사업정보는 추측하지 않습니다. 아래 값은 로컬에만 저장되며 Shopify에는 쓰지 않습니다.").classes("ss-help")
                    local_inputs = {}
                    fields = (("support_email", "지원 이메일"), ("legal_name", "사업자/법인명"),
                              ("business_address", "사업장 주소"), ("phone", "전화번호"),
                              ("return_window", "반품 기간"), ("return_address", "반품 주소"),
                              ("processing_time", "처리 시간"), ("shipping_time", "배송 기간"),
                              ("shipping_fee", "배송비"), ("governing_law", "준거법"))
                    for name, label in fields:
                        local_inputs[name] = ui.input(label=label)
                    def save_business_inputs():
                        saved = runner.save_business_inputs(str(run["store_id"]),
                            {name: field.value for name, field in local_inputs.items()}, confirmed=True)
                        ui.notify(f"{len(saved['saved_fields'])}개 항목을 로컬에 저장했습니다. 원격 변경은 없습니다.", type="positive")
                    ui.button("사업정보를 로컬에 저장", on_click=save_business_inputs).props("outline")
                    checks = {name: ui.checkbox(name) for name in ("Contact", "About", "Shipping", "Returns/Refund", "Privacy", "Terms")}
                    def save_pages():
                        payload = {name: box.value for name, box in checks.items()}
                        if not all(payload.values()): ui.notify("각 항목을 검토한 후 체크해 주세요.", type="warning"); return
                        result = runner.save_manual_evidence(run["run_id"], key, "PAGES_POLICIES_SIGNOFF", payload, confirmed=True)
                        render(result["production_run"])
                    ui.button("페이지/정책 검토 완료", on_click=save_pages).props("color=primary")
                    ui.label("이 확인은 페이지를 게시하거나 법률 적합성을 보증하지 않습니다.").classes("ss-help")
                elif key == "SEO_ACCESSIBILITY_MOBILE":
                    evidence = next(g["evidence"] for g in run["gates"] if g["gate_key"] == key)
                    fingerprint = evidence.get("visual_signoff_fingerprint")
                    checks = {name: ui.checkbox(label) for name, label in (("desktop", "데스크톱"), ("mobile", "모바일"),
                        ("hero_crop", "배너 잘림"), ("category_cards", "카테고리 카드"), ("menu", "메뉴"), ("footer", "푸터"), ("readability", "읽기 쉬움"))}
                    def save_visual():
                        result = runner.save_manual_evidence(run["run_id"], key, "VISUAL_SIGNOFF",
                            {name: box.value for name, box in checks.items()}, confirmed=True, fingerprint=fingerprint)
                        render(result["production_run"])
                    ui.button("현재 테마의 시각 검수 저장", on_click=save_visual).props("color=primary")
                    ui.label("자동 검사는 WCAG 완전 준수를 인증하지 않습니다.").classes("ss-help")
                elif key == "COMMERCE_READINESS":
                    evidence = next(g["evidence"] for g in run["gates"] if g["gate_key"] == key)
                    items = evidence.get("manual_verification_required") or ["US market", "shipping", "tax", "payment", "checkout", "store password"]
                    checks = {name: ui.checkbox(name) for name in items}
                    def save_commerce():
                        result = runner.save_manual_evidence(run["run_id"], key, "COMMERCE_SIGNOFF",
                                                             {name: box.value for name, box in checks.items()}, confirmed=True)
                        render(result["production_run"])
                    ui.button("판매 설정 수동확인 저장", on_click=save_commerce).props("color=primary")
                elif key == "READY_FOR_PILOT":
                    ui.label("G0~G13 사전 증거가 준비되었습니다. 이는 출시 완료가 아닙니다. 이 점검은 상품을 Shopify에 올리지 않습니다.").classes("ss-help")
                    ui.button("별도 10개 DRAFT 파일럿 화면 열기", on_click=lambda: ui.navigate.to("/pilot")).props("color=primary")
                elif key == "ENVIRONMENT_STORE_IDENTITY":
                    current_connection=get_shopify_connection(str(run["store_id"]))
                    auth_mode=ui.select({DEV_DASHBOARD_CLIENT_CREDENTIALS:"Dev Dashboard 앱 (권장)",
                                         LEGACY_ADMIN_TOKEN:"기존 Legacy 토큰"},
                        value=(current_connection or {}).get("auth_mode") or DEV_DASHBOARD_CLIENT_CREDENTIALS,
                        label="Shopify 연결 방식").classes("w-full")
                    domain=ui.input("Shopify shop domain (*.myshopify.com)",value=(current_connection or {}).get("shop_domain") or "").classes("w-full")
                    client_id=ui.input("Dev Dashboard Client ID").classes("w-full")
                    client_secret=ui.input("Dev Dashboard Client Secret").props("type=password autocomplete=new-password").classes("w-full")
                    profiles=list_app_profiles(db=runner.db)
                    app_profile=ui.select({p["profile_id"]:f"{p['display_name']} · {p['expected_app_gid']}" for p in profiles},
                        value=(current_connection or {}).get("app_profile_id"),label="기존 Production App 재사용 (선택사항)").classes("w-full")
                    app_identity=ui.label("Target Production App: " + ("BOUND" if (current_connection or {}).get("app_profile_id") else "NOT BOUND")).classes("ss-help")
                    legacy_token=ui.input("기존 Legacy Admin API access token").props("type=password autocomplete=new-password").classes("w-full")
                    ui.label("Client ID/Secret은 Shopify app.apiKey와 Store ID 검증을 통과한 뒤에만 Windows Credential Manager에 Production App 프로필로 저장합니다.").classes("ss-help")
                    def update_g0_auth_fields():
                        dev=auth_mode.value==DEV_DASHBOARD_CLIENT_CREDENTIALS
                        client_id.set_visibility(dev);client_secret.set_visibility(dev);legacy_token.set_visibility(not dev)
                    auth_mode.on_value_change(lambda _:update_g0_auth_fields());update_g0_auth_fields()
                    health=ui.label("").classes("ss-help")
                    def show_g0_auth_health():
                        info=ShopifyAuthService(db=runner.db).status(str(run["store_id"]))
                        health.set_text(f"Target Production App: {info.get('expected_app_title') or 'NOT BOUND'} · Authenticated App: {info.get('authenticated_app_title') or 'identity 확인 전'} [{info.get('authenticated_app_gid') or '—'}] · Shop GID: {info.get('shopify_shop_gid') or '—'} · {info['auth_mode']} · 자격 증명 {'YES' if info['credential_present'] else 'NO'} · {info['token_state']} · 만료 {info.get('expires_in_minutes') if info.get('expires_in_minutes') is not None else '확인 전'}분")
                    show_g0_auth_health()
                    async def save_and_continue_g0():
                        try:
                            mode=auth_mode.value
                            if mode==DEV_DASHBOARD_CLIENT_CREDENTIALS and bool(client_id.value)!=bool(client_secret.value):
                                raise ValueError("Client ID와 Client Secret을 모두 입력하세요.")
                            if mode==DEV_DASHBOARD_CLIENT_CREDENTIALS and client_id.value and client_secret.value:
                                bound=await asyncio.to_thread(verify_and_bind_app_profile,str(run["store_id"]),domain.value or "",
                                    client_id.value,client_secret.value,db=runner.db)
                                client_id.value="";client_secret.value=""
                                app_identity.set_text(f"Authenticated App: {bound['app_title']} · {bound['app_id']} · Shop: {bound['shop_name']} · {bound['shop_id']}")
                            elif mode==DEV_DASHBOARD_CLIENT_CREDENTIALS and app_profile.value:
                                bound=await asyncio.to_thread(bind_existing_app_profile,str(run["store_id"]),domain.value or "",
                                    str(app_profile.value),db=runner.db)
                                app_identity.set_text(f"Authenticated App: {bound['app_title']} · {bound['app_id']} · Shop: {bound['shop_name']} · {bound['shop_id']}")
                            elif mode==DEV_DASHBOARD_CLIENT_CREDENTIALS:
                                raise ValueError("Client ID/Secret을 입력하거나 기존 Production App 프로필을 선택하세요.")
                            else:
                                save_shopify_connection(str(run["store_id"]),domain.value or "",auth_mode=mode,db=runner.db)
                            if mode==LEGACY_ADMIN_TOKEN and legacy_token.value:
                                save_shopify_token(str(run["store_id"]),legacy_token.value);legacy_token.value=""
                            updated=await asyncio.to_thread(runner.run,str(run["store_id"]),run_id=run["run_id"])
                            render(updated);ui.notify("앱/스토어 신원을 확인하고 같은 점검을 이어갔습니다. Shopify 원격 쓰기는 하지 않았습니다.",type="positive")
                        except Exception as exc: ui.notify(_safe_error(exc),type="negative")
                    ui.button("연결 정보 저장 후 Shopify 읽기 확인 / 같은 점검 계속",on_click=save_and_continue_g0).props("color=primary size=lg")
                    ui.button("Shopify 연결 다시 확인", on_click=begin).props("color=primary")
                elif key == "SOURCING_QUALITY":
                    evidence = gate.get("evidence") or {}
                    ui.label("MASTER 소싱 품질 증거").classes("ss-card-title")
                    ui.label(f"현재 상태: {STATUS_KO.get(gate.get('status'), '확인 필요')} · 증거 집계: {json.dumps(evidence.get('counts', {}), ensure_ascii=False)}")
                    ui.label("PRIMARY 분류만으로 통과하지 않습니다. ASIN·원본 참조·제한/중복·variant 등 소싱 근거를 확인합니다.").classes("ss-help")
                    ui.button("소싱 품질 화면 열기", on_click=lambda: ui.navigate.to("/sourcing")).props("outline")
                elif key == "COLLECTION_ARCHITECTURE":
                    ui.label("상품 분포를 기준으로 컬렉션 계획을 확인합니다. 빈 컬렉션이나 지나치게 넓은 규칙은 준비 완료로 보지 않습니다.").classes("ss-help")
                    ui.button("컬렉션 설계 열기", on_click=lambda: ui.navigate.to("/collections")).props("outline")
                elif key == "COLLECTION_CATEGORY_MEDIA":
                    ui.label("컬렉션별 승인 이미지와 비율 검토가 필요합니다. 이미지 생성·업로드는 이 패널을 여는 것만으로 실행되지 않습니다.").classes("ss-help")
                    ui.button("이미지·홈페이지 준비 열기", on_click=lambda: ui.navigate.to("/homepage")).props("outline")
                elif key == "BRAND_HEADER_NAVIGATION":
                    brand_state=(gate.get("evidence") or {}).get("brand_assets") or {}
                    ui.label(f"브랜드명: {brand_state.get('brand_name') or '미설정'} · {brand_state.get('name_status','확인 필요')}").classes("font-semibold")
                    ui.label(f"로고: {brand_state.get('logo','확인 필요')} · 파비콘: {brand_state.get('favicon','확인 필요')} · 메뉴: {brand_state.get('navigation','확인 필요')}").classes("ss-help")
                    ui.label("기술 검사는 시각 품질 승인을 대신하지 않습니다. remote 적용/검증도 별도 상태로 관리합니다.").classes("ss-help")
                    with ui.row().classes("gap-2 flex-wrap"):
                        ui.button("브랜드 이름 확인", on_click=lambda: ui.navigate.to("/brand")).props("outline")
                        ui.button("로고 준비", on_click=lambda: ui.navigate.to("/brand")).props("outline")
                        ui.button("파비콘 준비", on_click=lambda: ui.navigate.to("/brand")).props("outline")
                        ui.button("Shopify 적용 미리보기", on_click=lambda: ui.navigate.to("/brand")).props("outline")
                        ui.button("수동 적용 안내", on_click=lambda: ui.navigate.to("/brand")).props("outline")
                        ui.button("메뉴 확인", on_click=lambda: ui.navigate.to("/navigation")).props("outline")
                elif key == "HOMEPAGE":
                    ui.label("Hero·CTA·카테고리 대상 링크와 현재 테마를 미리보기로 확인합니다. 테마 쓰기는 별도 승인 없이는 실행되지 않습니다.").classes("ss-help")
                    ui.button("홈페이지 자동화 열기", on_click=lambda: ui.navigate.to("/homepage")).props("outline")
                elif key == "PRODUCT_COLLECTION_TEMPLATES":
                    ui.label("상품/컬렉션 템플릿은 읽을 수 있는 실제 theme evidence만 검토합니다. 모르는 schema는 수동 확인으로 남깁니다.").classes("ss-help")
                    ui.button("스토어 완성도 화면 열기", on_click=lambda: ui.navigate.to("/completion")).props("outline")
                elif key == "CONTROLLED_LIVE_PILOT":
                    unresolved = [f"{i + 1:02d} {GATE_LABELS_KO.get(item['gate_key'], item['gate_key'])}" for i, item in enumerate(run["gates"][:14]) if item.get("status") not in {"READY", "READY_WITH_WARNINGS", "VERIFIED"}]
                    ui.label("10개 상품 DRAFT 파일럿은 별도 명시 승인 전에는 실행되지 않습니다.").classes("ss-help")
                    if unresolved:
                        ui.label("아직 충족되지 않은 선행 확인:").classes("font-semibold")
                        for item in unresolved: ui.label(f"• {item}").classes("ss-help")
                    ui.button("파일럿 화면 열기", on_click=lambda: ui.navigate.to("/pilot")).props("outline" + (" disable" if unresolved else ""))
                elif key == "BATCH_EXPANSION":
                    ui.label("10개 파일럿의 remote 재조회 검증이 완료되기 전에는 확대 batch를 시작할 수 없습니다.").classes("ss-help")
                    ui.label(f"저장 증거: {json.dumps(gate.get('evidence') or {}, ensure_ascii=False)}").classes("ss-help")
                elif key == "FINAL_LAUNCH_READINESS":
                    ui.label("최종 체크리스트는 source·권리·마진·배송·결제 등 실제 evidence를 요구합니다. ACTIVE 공개는 별도의 명시 승인이 필요합니다.").classes("ss-help")
                    ui.label(f"현재 선행 gate 미완료: {sum(item.get('status') not in {'READY', 'READY_WITH_WARNINGS', 'VERIFIED'} for item in run['gates'][:16])}개").classes("font-semibold")
                else:
                    ui.label("기존 해당 기능에서 항목을 준비한 뒤 실전 점검을 다시 실행하세요.").classes("ss-help")
                ui.button("이 gate와 전체 증거 재검증", on_click=lambda: begin()).props("outline")
            action_dialog.open()
        def open_current_action():
            run = state.get("run")
            if not run:
                ui.notify("먼저 실전 점검 자동 진행을 눌러 주세요.", type="warning"); return
            open_gate_action(service.progress_report(run["run_id"])["current_stage"])
        start.on_click(begin)
        def create_confirmed_run():
            run = runner.start_or_resume(self.current_store, new_run=True, confirmed=True)
            render(run)
        with ui.dialog() as confirm_new_run, ui.card():
            ui.label("기존 기록은 보존됩니다. 새 점검 기록을 추가로 만들까요?")
            with ui.row():
                ui.button("취소", on_click=confirm_new_run.close).props("outline")
                ui.button("새 점검 확인", on_click=lambda: (confirm_new_run.close(), create_confirmed_run())).props("color=primary")
        new_run_button.on_click(confirm_new_run.open)
        if self.current_store == "001": load_latest()

    def _source_safety(self):
        ui = self.ui
        from ..automation import WorkflowAutomationService, source_safety_workflow
        from ..source_safety import SourceMonitorService, SourceSafetyService
        self._heading("소스 재고·가격 안전", "Amazon/source 재고와 가격을 확인하고 Shopify 판매 위험을 자동으로 막습니다.")
        safety=SourceSafetyService(); monitor=SourceMonitorService(); automation=_workflow_service()
        state={"run_id":None,"preview":None,"filter":"ALL","page":0}
        mode=ui.toggle({"AUTO":"자동","REVIEW":"검토","MANUAL":"수동"},value="AUTO").props("unelevated")

        with ui.row().classes("w-full flex-wrap items-center gap-3"):
            auto_button=ui.button("자동 안전검사 시작",icon="auto_awesome").props("color=primary size=lg")
            ui.button("Sold Out 원인 진단",icon="troubleshoot",on_click=lambda:ui.notify("읽기 전용 진단을 준비했습니다. Shopify 변경 없음.")).props("outline")
            ui.button("가격변동 보기",icon="price_change",on_click=lambda:(state.update(filter="PRICE",page=0),render_table())).props("outline")
            ui.button("모니터링 설정",icon="settings",on_click=lambda:ui.navigate.to("/settings")).props("outline")

        card_values={}
        with ui.row().classes("w-full grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4"):
            for key,label,icon in (("targets","검사 대상","inventory_2"),("fresh","최신 재고 확인","verified"),("out_of_stock","품절","remove_shopping_cart"),("attention","확인 필요","warning"),("price_changed","가격변동","price_change"),("blocked","판매차단 후보","block")):
                with ui.card().classes("rounded-xl border border-slate-200 p-4"):
                    ui.label(label).classes("ss-help")
                    card_values[key]=ui.label("0").classes("ss-kpi")
                    ui.icon(icon).classes("text-slate-500")

        with ui.card().classes("w-full rounded-xl border border-blue-100 p-5"):
            ui.label("자동 작업 진행").classes("ss-card-title")
            current_stage=ui.label("대기 중")
            progress=ui.linear_progress(value=0,show_value=False).classes("w-full")
            progress_text=ui.label("0% · 0 / 0")
            progress_detail=ui.label("남은 batch 0 · 예상 token 0 · 최근 업데이트 없음").classes("ss-help")
            waiting=ui.label("").classes("text-amber-800 font-semibold")
            controls=ui.row().classes("w-full gap-3")
            activity=ui.column().classes("w-full gap-1")

        result_summary=ui.label("검사 전입니다.").classes("ss-card-title")
        with ui.row().classes("w-full items-center gap-3"):
            search=ui.input("상품명 또는 ASIN 검색").props("clearable").classes("min-w-80")
            filter_select=ui.select({"ALL":"전체","SELLABLE":"판매 가능","OOS":"품절","ATTENTION":"확인 필요","PRICE":"가격변동","BLOCKED":"차단","RESTOCK":"재입고 후보"},value="ALL",label="결과 필터").classes("w-48")
        columns=[{"name":k,"label":label,"field":k,"sortable":True,"align":"left"} for k,label in (("title","상품명"),("asin","ASIN"),("source_availability","Source 재고"),("current_source_price","Source 가격"),("freshness_status","Freshness"),("sellability_status","판매 가능"),("action","조치"))]
        table=ui.table(columns=columns,rows=[],row_key="asin",pagination={"rowsPerPage":50}).classes("w-full sticky-header")
        with ui.row().classes("w-full items-center gap-3"):
            ui.button("이전",on_click=lambda:(state.update(page=max(0,state["page"]-1)),render_table()),icon="chevron_left").props("outline")
            page_label=ui.label("0건")
            ui.button("다음",on_click=lambda:(state.update(page=state["page"]+1),render_table()),icon="chevron_right").props("outline")
        advanced_data={"value":{}}
        with ui.expansion("고급 정보 · 원본 데이터 보기 · 로그 보기",icon="code",value=False).classes("w-full"):
            ui.label("기술 ID와 원본 상태는 초보자 화면에서 숨겨집니다.").classes("ss-help")
            advanced=ui.code("아직 기술 정보가 없습니다.").classes("w-full max-h-80 overflow-auto")

        def refresh_summary():
            values=safety.beginner_summary(self.current_store)
            for key,label in card_values.items(): label.set_text(f"{values.get(key,0):,}")
            result_summary.set_text(f"판매 가능 {values['sellable']:,} · 확인 필요 {values['attention']:,} · 원본 품절 {values['out_of_stock']:,} · 가격/마진 차단 {values['blocked']:,} · 재확인 예정 {values['recheck']:,}")

        def render_table():
            result=safety.result_page(self.current_store,page=state["page"],page_size=50,search=search.value or "",filter_key=filter_select.value or state["filter"])
            korean={"IN_STOCK":"재고 있음","OUT_OF_STOCK":"품절","UNKNOWN":"확인 필요","FRESH":"최신","STALE_BLOCKED":"오래됨","SELLABLE":"판매 가능","NEEDS_PRICING_POLICY":"가격정책 필요"}
            table.rows=[{**row,"source_availability":korean.get(row["source_availability"],row["source_availability"]),"freshness_status":korean.get(row["freshness_status"],row["freshness_status"]),"sellability_status":korean.get(row["sellability_status"],row["sellability_status"]),"action":"검토" if row["sellability_status"]!="SELLABLE" else "조치 없음"} for row in result["rows"]]
            table.update(); page_label.set_text(f"총 {result['total']:,}건 · 페이지 {result['page']+1} · 화면 최대 50개")
            advanced_data["value"]=result; advanced.set_content(json.dumps(result,ensure_ascii=False,indent=2))

        def show_run(result):
            state["run_id"]=result["run_id"]; progress.value=result["progress_percent"]/100
            current=result.get("current_task") or {}; current_stage.set_text("현재 단계: "+(current.get("title") or "완료"))
            progress_text.set_text(f"{result['progress_percent']}% · {result['completed_tasks']} / {result['total_tasks']}")
            preview=state.get("preview") or {}; progress_detail.set_text(f"남은 batch {max(0,preview.get('estimated_batches',0))} · 예상 token {preview.get('estimated_tokens',0)} · 최근 업데이트 {result['updated_at']}")
            waiting.set_text("사람 확인 필요: "+(current.get("error_message") or "승인 후 자동으로 계속합니다.")) if result["status"].startswith("WAITING") else waiting.set_text("")
            activity.clear()
            with activity:
                ui.label("최근 활동").classes("font-semibold")
                for event in list(reversed(result.get("events",[])[:5])):
                    ui.label(f"{event['created_at'][11:16]} {event['message']}").classes("ss-help")
            advanced.set_content(json.dumps({"run":result,"preview":preview},ensure_ascii=False,indent=2))

        def run_auto():
            try:
                preview=monitor.preview_due_checks(self.current_store,limit=2000); state["preview"]=preview
                result=automation.create_run(self.current_store,"SOURCE_SAFETY",source_safety_workflow(preview),mode=mode.value or "AUTO")
                result=automation.run(result["run_id"]); show_run(result); refresh_summary(); render_table()
                ui.notify("안전한 단계는 자동 진행했으며 비용/외부 작업이 필요하면 한 번만 멈춥니다.",type="positive")
            except Exception as exc: ui.notify(_safe_error(exc),type="negative")
        auto_button.on_click(run_auto)

        with controls:
            ui.button("일시정지",on_click=lambda:show_run(automation.pause(state["run_id"])) if state["run_id"] else None,icon="pause").props("outline")
            ui.button("계속",on_click=lambda:show_run(automation.resume(state["run_id"])) if state["run_id"] else None,icon="play_arrow").props("outline")
            ui.button("중단",on_click=lambda:show_run(automation.stop(state["run_id"])) if state["run_id"] else None,icon="stop").props("outline color=negative class=ml-6")
            ui.button("실패만 재시도",on_click=lambda:show_run(automation.retry_failed(state["run_id"])) if state["run_id"] else None,icon="refresh").props("outline")
            def confirm_current():
                if not state["run_id"]: return
                result=automation.status(state["run_id"])
                if result["status"]!="WAITING_FOR_CONFIRMATION": ui.notify("현재 승인 대기 중인 작업이 없습니다.",type="warning"); return
                with ui.dialog() as dialog,ui.card():
                    ui.label((result.get("current_task") or {}).get("error_message") or "비용 또는 외부 실행을 승인하시겠습니까?")
                    with ui.row().classes("gap-3"):
                        ui.button("취소",on_click=dialog.close).props("outline")
                        def approve(): dialog.close(); show_run(automation.confirm(state["run_id"],result["current_task_key"]))
                        ui.button("확인하고 계속",on_click=approve).props("color=primary")
                dialog.open()
            ui.button("확인하고 계속",on_click=confirm_current,icon="verified_user").props("color=primary")
        search.on_value_change(lambda _:(state.update(page=0),render_table())); filter_select.on_value_change(lambda _:(state.update(page=0),render_table()))
        refresh_summary(); render_table()

    def _store_completion(self):
        ui = self.ui
        self._heading("스토어 완성", "샵 전체 요소를 한 곳에서 검사합니다. 점수보다 차단 요소가 우선입니다.")
        state = {"plan": None, "filter": "ALL"}
        overall = ui.label("아직 검사하지 않았습니다.").classes("text-2xl font-bold")
        cards = ui.column().classes("w-full gap-2")

        def render():
            cards.clear()
            plan = state["plan"]
            if not plan:
                return
            labels = {"VERIFIED": "완료", "MANUAL_ACTION_REQUIRED": "수동 작업", "BLOCKED": "차단"}
            colors = {"VERIFIED": "text-green-700", "MANUAL_ACTION_REQUIRED": "text-amber-700", "BLOCKED": "text-red-700"}
            overall.set_text(f"{plan['readiness_score']} / 100 · {plan['status']}")
            with cards:
                for item in plan["items"]:
                    if state["filter"] == "MANUAL" and item["status"] != "MANUAL_ACTION_REQUIRED": continue
                    if state["filter"] == "BLOCKED" and item["status"] != "BLOCKED": continue
                    with ui.card().classes("w-full py-2"):
                        with ui.row().classes("w-full items-center"):
                            ui.label(item["title"]).classes("font-semibold")
                            ui.space()
                            ui.label(labels[item["status"]]).classes(colors[item["status"]])
                        with ui.expansion("고급 설정 · 기술 정보").classes("w-full text-xs"):
                            ui.code(json.dumps({"item_id": item["id"], "source": item["source_component"],
                                                "remote_reference": item["remote_reference"], "details": item["details"]},
                                               ensure_ascii=False, indent=2))

        def inspect_all():
            state["plan"] = StoreCompletionService().build_plan(self.current_store)
            render(); ui.notify("전체 스토어 검사가 완료되었습니다.", type="positive")

        def design_missing():
            if not state["plan"]: inspect_all()
            result = StoreCompletionService().preview_fixes(state["plan"]["plan_id"])
            ui.notify(f"로컬 안전 설계 {len(result['fixes'])}건 · 외부 쓰기 0건")

        def preview_completion():
            if not state["plan"]: inspect_all()
            ui.notify("완성 미리보기는 읽기 전용이며 실제 Shopify 변경을 하지 않습니다.")

        def apply_safe():
            if not state["plan"]: inspect_all()
            selected = [x["id"] for x in state["plan"]["items"] if x["automation_mode"] == "AUTO_SAFE"]
            result = StoreCompletionService().apply_safe(state["plan"]["plan_id"], selected)
            ui.notify(f"로컬 안전 항목 {len(result['applied'])}건 처리 · 원격 쓰기 {result['remote_writes']}건")

        def verify():
            if not state["plan"]: inspect_all()
            result = StoreCompletionService().verify(state["plan"]["plan_id"])
            ui.notify(f"검증: {result['status']} · blocker {result['blocker_count']}건")

        def launch_readiness():
            if not state["plan"]: inspect_all()
            result = StoreCompletionService().generate_report(state["plan"]["plan_id"])
            ui.notify(f"Launch Readiness 보고서 생성: {result['status']}", type="positive")

        with ui.row().classes("gap-2"):
            ui.button("전체 스토어 검사", on_click=inspect_all, icon="fact_check").props("color=primary")
            ui.button("누락 요소 자동 설계", on_click=design_missing, icon="auto_awesome").props("outline")
            ui.button("완성 미리보기", on_click=preview_completion, icon="preview").props("outline")
            ui.button("안전한 항목 적용", on_click=apply_safe, icon="shield").props("outline")
            ui.button("검증", on_click=verify, icon="verified").props("outline")
            ui.button("Launch Readiness", on_click=launch_readiness, icon="rocket_launch").props("color=positive")
        with ui.row():
            ui.button("수동 작업만 보기", on_click=lambda: (state.update(filter="MANUAL"), render())).props("flat")
            ui.button("차단 요소만 보기", on_click=lambda: (state.update(filter="BLOCKED"), render())).props("flat")
            ui.button("전체 보기", on_click=lambda: (state.update(filter="ALL"), render())).props("flat")

    def _store_build(self):
        ui = self.ui
        from ..automation import WorkflowAutomationService, store_build_workflow
        self._heading("자동으로 스토어 완성", "안전한 계획과 검사는 자동으로 진행하고, 비용이나 실제 외부 변경에서만 멈춥니다.")
        beginner_auto=_workflow_service(); beginner_state={"run_id":None}
        with ui.card().classes("w-full rounded-xl border-2 border-blue-200 p-5"):
            ui.label("원클릭 AUTO").classes("ss-card-title")
            ui.label("Store Profile → Source Safety → 상품 → 컬렉션 → 브랜드 → 메뉴 → 홈페이지 → 완성도 검사를 이어서 진행합니다.").classes("ss-help")
            auto_status=ui.label("자동 구축을 시작할 수 있습니다.").classes("font-semibold")
            auto_progress=ui.linear_progress(value=0).classes("w-full")
            def auto_build():
                result=beginner_auto.create_run(self.current_store,"STORE_COMPLETION",store_build_workflow(),mode="AUTO")
                result=beginner_auto.run(result["run_id"]); beginner_state["run_id"]=result["run_id"]
                auto_progress.value=result["progress_percent"]/100; auto_status.set_text(f"{result['status']} · {result['completed_tasks']} / {result['total_tasks']}")
            with ui.row().classes("gap-3"):
                ui.button("자동 구축 시작",on_click=auto_build,icon="auto_awesome").props("color=primary size=lg")
                ui.button("일시정지",on_click=lambda:beginner_auto.pause(beginner_state["run_id"]) if beginner_state["run_id"] else None,icon="pause").props("outline")
                ui.button("계속",on_click=lambda:beginner_auto.resume(beginner_state["run_id"]) if beginner_state["run_id"] else None,icon="play_arrow").props("outline")
                ui.button("중단",on_click=lambda:beginner_auto.stop(beginner_state["run_id"]) if beginner_state["run_id"] else None,icon="stop").props("outline color=negative class=ml-6")
        with ui.expansion("전문가 구축 설정",icon="tune",value=False).classes("w-full"):
            ui.label("아래 기존 단계별 설정은 전문가용입니다.").classes("ss-help")
        ui.label("실제 소싱·Shopify 쓰기·유료 이미지 생성은 LIVE 실행과 해당 옵션을 모두 선택해야 합니다. Navigation/Theme 쓰기는 별도 확인을 거치며 지원되지 않는 테마는 수동 단계로 남습니다.").classes("text-sm text-amber-800")
        store = next((row for row in self.stores if row["store_id"] == self.current_store), None)
        ui.label(f"Store: {self.current_store} | {store['store_name'] if store else self.current_store}").classes("text-xl font-semibold")
        with ui.card().classes("w-full border border-sky-200"):
            homepage_sync = ui.checkbox("Apply homepage theme patch in this LIVE run", value=False)
            homepage_assets_approved = ui.checkbox("Homepage image assets are reviewed and approved", value=False)
            mode = ui.radio({"PREVIEW": "미리보기", "LIVE": "실제 실행"}, value="PREVIEW").props("inline")
            provider = ui.radio({"DIRECT_SHOPIFY": "DIRECT_SHOPIFY (권장)", "SPARK_FALLBACK": "SPARK_FALLBACK"}, value="DIRECT_SHOPIFY").props("inline")
            with ui.expansion("고급 설정", icon="tune"):
                auto_source = ui.checkbox("자동소싱", value=True)
                product_sync = ui.checkbox("Shopify 상품 자동 업로드", value=True)
                collection_design = ui.checkbox("컬렉션 자동 설계", value=True)
                images = ui.checkbox("컬렉션 이미지 자동 생성", value=False)
                image_paid_opt_in = ui.checkbox("유료 이미지 생성 opt-in", value=False)
                collection_sync = ui.checkbox("Shopify 컬렉션 자동 생성", value=True)
                publish_collections = ui.checkbox("Online Store 공개", value=False)
                homepage = ui.checkbox("홈페이지 컬렉션 계획", value=True)
                navigation_automation = ui.checkbox("메가메뉴 계획/검증 포함", value=False)
                navigation_sync = ui.checkbox("Shopify Navigation 실제 동기화", value=False)
                mega_menu_apply = ui.checkbox("테마 메가메뉴 적용 허용", value=False)
                brand_automation = ui.checkbox("브랜드 profile/자산 단계 포함", value=False)
                brand_paid = ui.checkbox("브랜드 이미지 자동 생성 사용 (유료 opt-in)", value=False)
                brand_model = ui.input("OpenAI 이미지 모델 (기본: gpt-image-1)", value="gpt-image-1").classes("w-72")
                brand_apply = ui.checkbox("브랜드 theme 적용 단계 포함", value=False)
                target = ui.number("소싱 목표", value=2000, min=1, max=10000).classes("w-48")
                product_status = ui.select({"DRAFT": "DRAFT (권장)", "ACTIVE": "ACTIVE"}, value="DRAFT", label="Shopify 상품 상태").classes("w-56")
                product_media = ui.select({"MANUAL_MEDIA": "수동 이미지 (권장)", "SOURCE_MEDIA": "SOURCE_MEDIA", "GENERATED_MEDIA": "GENERATED_MEDIA", "MIXED": "MIXED"}, value="MANUAL_MEDIA", label="상품 미디어 정책").classes("w-72")
                source_rights = ui.checkbox("원본 상품 이미지 재사용 권리 확인", value=False)
                confirmation = ui.select({"source_complete": "Amazon 소싱 worker 완료 확인", "amazon_challenge_resolved": "Amazon 확인 화면을 직접 해결 완료", "spark_upload_confirmed": "SparkShopify 업로드 완료 확인", "theme_manual_apply_confirmed": "Theme patch 수동 적용 완료 확인", "mega_menu_manual_applied":"Mega menu 테마 수동 적용 확인", "navigation_manual_synced":"Shop 메뉴 수동 동기화 완료 확인"}, label="수동 단계 확인", value=None).classes("w-96")
            current = {"run_id": None}
            status_label = ui.label("Run: 미리보기 전").classes("font-medium")
            timeline = ui.column().classes("w-full gap-1")

            def show_run(result):
                current["run_id"] = result["run_id"]
                status_label.set_text(f"{result['status']} · run {result['run_id']} · stage {result.get('stage', 'PLAN')}")
                timeline.clear()
                with timeline:
                    for name, state in result.get("stages", {}).items():
                        ui.label(f"{name.replace('_', ' ')} · {state}").classes("text-sm text-amber-800" if state in {"FAILED", "MANUAL_ACTION_REQUIRED"} else "text-sm")
                    if result.get("last_error"):
                        ui.label("오류: " + result["last_error"]).classes("text-sm text-red-700")
                    details = result.get("stage_data", {}).get(result.get("stage"), {})
                    if details.get("instructions"):
                        ui.label(details["instructions"]).classes("text-sm text-amber-800")

            latest = StoreBuildOrchestrator().latest(self.current_store)
            if latest:
                show_run(latest)

            async def create_preview():
                try:
                    options = {"auto_sourcing": bool(auto_source.value), "product_sync": bool(product_sync.value),
                               "collection_design": bool(collection_design.value), "collection_images": bool(images.value),
                               "paid_image_opt_in": bool(image_paid_opt_in.value), "collection_sync": bool(collection_sync.value),
                               "publish_collections": bool(publish_collections.value), "homepage_plan": bool(homepage.value),
                               "source_target": int(target.value or 2000), "publish_status": product_status.value or "DRAFT",
                               "media_mode": product_media.value or "MANUAL_MEDIA",
                               "source_media_rights_confirmed": bool(source_rights.value),
                               "brand_automation": bool(brand_automation.value), "brand_image_opt_in": bool(brand_paid.value),
                               "brand_image_model": brand_model.value or "gpt-image-1", "brand_apply": bool(brand_apply.value),
                               "navigation_automation":bool(navigation_automation.value),"navigation_sync":bool(navigation_sync.value),
                               "mega_menu_apply":bool(mega_menu_apply.value), "homepage_sync":bool(homepage_sync.value),
                               "homepage_assets_approved":bool(homepage_assets_approved.value)}
                    service = StoreBuildOrchestrator()
                    result = service.preview(self.current_store, options=options, provider=provider.value or "DIRECT_SHOPIFY", mode=mode.value or "PREVIEW")
                    show_run(service.get(result["run_id"]))
                    ui.notify(f"미리보기 run 생성: {result['run_id']} · 외부 쓰기 없음", type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            async def start_build():
                if not current["run_id"]:
                    ui.notify("먼저 최신 미리보기를 만드세요.", type="warning"); return
                if mode.value != "LIVE":
                    ui.notify("실제 실행 모드를 선택해야 시작할 수 있습니다.", type="warning"); return
                with ui.dialog() as dialog,ui.card():
                    ui.label("LIVE Store Build를 시작합니다. 선택한 sourcing, Shopify 상품/컬렉션/navigation, 이미지 및 Theme 옵션은 실제 서비스에 적용될 수 있습니다.")
                    ui.label(f"Store {self.current_store} · Navigation sync {'ON' if navigation_sync.value else 'OFF'} · Mega-menu Theme apply {'ON' if mega_menu_apply.value else 'OFF'} · 유료 브랜드 이미지 {'ON' if brand_paid.value else 'OFF'}")
                    with ui.row():
                        ui.button("취소",on_click=dialog.close).props("outline")
                        async def confirm_live():
                            dialog.close()
                            try:
                                service=StoreBuildOrchestrator()
                                result=await asyncio.to_thread(service.start,current["run_id"],live_confirmed=True)
                                show_run(result)
                                ui.notify("Store Build 단계가 실행/수동 게이트까지 진행되었습니다.",type="positive" if result["status"]!="FAILED" else "negative")
                            except Exception as exc:ui.notify(_safe_error(exc),type="negative")
                        ui.button("LIVE 실행 확인",on_click=confirm_live).props("color=negative")
                dialog.open()

            async def pause_build():
                if current["run_id"] and StoreBuildOrchestrator().pause(current["run_id"]):
                    show_run(StoreBuildOrchestrator().get(current["run_id"]))
                    ui.notify("현재 단계의 체크포인트에서 일시정지합니다.", type="warning")
                else: ui.notify("일시정지할 실행 중인 run이 없습니다.", type="warning")

            async def resume_build():
                if not current["run_id"]: return
                try:
                    result = await asyncio.to_thread(StoreBuildOrchestrator().resume, current["run_id"], manual_confirmation=confirmation.value)
                    show_run(result)
                    ui.notify("Store Build를 이어갔습니다.", type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            async def retry_build():
                if not current["run_id"]: return
                try:
                    result = await asyncio.to_thread(StoreBuildOrchestrator().retry_failed, current["run_id"], live_confirmed=(mode.value == "LIVE"))
                    show_run(result)
                    ui.notify("실패 단계 재시도를 마쳤습니다.", type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            with ui.row().classes("flex-wrap"):
                ui.button("스토어 자동 구축 미리보기", on_click=create_preview, icon="preview").props("outline")
                ui.button("스토어 자동 구축 시작", on_click=start_build, icon="rocket_launch").props("color=primary")
                ui.button("일시정지", on_click=pause_build, icon="pause").props("outline")
                ui.button("계속", on_click=resume_build, icon="play_arrow").props("outline")
                ui.button("실패 항목 재시도", on_click=retry_build, icon="refresh").props("outline")

    def _shopify_pilot(self):
        """Phase 4.2 gate UI. No write button is enabled by a read/preview action."""
        ui = self.ui
        from ..live_pilot import ControlledLivePilotService

        self._heading("실전 테스트", "현재 단계만 확인하면 됩니다. 실제 변경은 승인 버튼을 직접 누를 때만 발생합니다.")
        steps=("Source 안전 확인","Shopify 연결 확인","10개 DRAFT 미리보기","실제 업로드 승인","자동 검증","최대 3개 컬렉션","메뉴","홈페이지","완료 보고")
        with ui.row().classes("w-full grid grid-cols-2 md:grid-cols-3 xl:grid-cols-9 gap-2"):
            for index,label in enumerate(steps,1):
                with ui.card().classes("rounded-lg border border-slate-200 p-3"):
                    ui.label(f"{index}").classes("text-blue-700 font-bold")
                    ui.label(label).classes("font-semibold")
        connection = get_shopify_connection("001") or {}
        state = {"service": None, "run_id": None, "preview": None}
        status = ui.label("GATE A · READ-ONLY PREFLIGHT 대기").classes("font-semibold text-lg")
        ui.label("실제 Shopify 변경은 각 단계에서 사용자가 ‘실행’을 직접 누른 뒤에만 발생합니다.").classes("text-amber-800")
        expected_domain = ui.input("확인할 Cabin Tidy Shopify domain",
                                   value=connection.get("shop_domain") or "").classes("w-96")
        live_mode = ui.checkbox("LIVE PILOT 선택 (미리보기만으로는 실제 변경되지 않음)", value=False)
        table = ui.table(columns=[{"name": key, "label": label, "field": key, "align": "left"} for key, label in (
            ("source_id", "ASIN"), ("title", "Title"), ("source_price", "Source price"),
            ("selling_price", "Selling price"), ("currency", "Currency"), ("status", "Status"),
            ("action", "Action"), ("tags", "Tags"), ("warning", "Warnings"))], rows=[]).classes("w-full")
        write_button = None
        verify_button = None
        collection_button = None

        def refresh():
            run = state["service"].get_run(state["run_id"]) if state["service"] and state["run_id"] else None
            gate = run["gate"] if run else "PREFLIGHT"
            status.set_text(f"현재 gate: {gate} · 상태: {run['status'] if run else 'PENDING'}")
            if write_button:
                (write_button.enable() if gate == "PRODUCT_WRITE" and live_mode.value else write_button.disable())
            if verify_button:
                (verify_button.enable() if gate == "PRODUCT_VERIFY" else verify_button.disable())
            if collection_button:
                (collection_button.enable() if gate == "COLLECTION_PREVIEW" else collection_button.disable())

        async def preflight():
            if self.current_store != "001":
                ui.notify("Store = 001 | Cabin Tidy를 선택해야 합니다.", type="negative"); return
            try:
                service = ControlledLivePilotService()
                run = await asyncio.to_thread(service.create_run, "001", expected_domain.value, store_name="Cabin Tidy")
                await asyncio.to_thread(service.preflight, run["run_id"])
                state.update(service=service, run_id=run["run_id"], preview=None)
                ui.notify("도메인·인증·API·상품 권한을 읽기 전용으로 확인했습니다. 토큰은 표시하지 않습니다.", type="positive")
            except Exception as exc: ui.notify(_safe_error(exc), type="negative")
            refresh()

        async def product_preview():
            if not state["service"]: ui.notify("먼저 READ-ONLY PREFLIGHT를 실행하세요.", type="warning"); return
            try:
                result = await asyncio.to_thread(state["service"].preview_products, state["run_id"], limit=10)
                state["preview"] = result
                table.rows = [{**item, "tags": ", ".join(item.get("tags") or item.get("collection_tags") or [])}
                              for item in result["items"]]
                table.update(); ui.notify("최대 10개 DRAFT 미리보기 완료 · 이미지 OFF · Shopify write 없음", type="positive")
            except Exception as exc: ui.notify(_safe_error(exc), type="negative")
            refresh()

        async def confirmed_product_write():
            try:
                await asyncio.to_thread(state["service"].write_products, state["run_id"], confirmed=True)
                result=await asyncio.to_thread(state["service"].verify_products,state["run_id"])
                ui.notify(f"업로드 후 자동 검증: {result['status']}", type="positive")
            except Exception as exc: ui.notify(_safe_error(exc), type="negative")
            refresh()

        async def request_product_write():
            if not live_mode.value: ui.notify("LIVE PILOT을 직접 선택하세요.", type="warning"); return
            with ui.dialog() as dialog, ui.card():
                ui.label("Cabin Tidy에 최대 10개의 DRAFT 상품을 실제 생성/수정합니다. 실제 Shopify 변경이 발생합니다.")
                with ui.row():
                    ui.button("취소", on_click=dialog.close).props("outline")
                    async def run_write(): dialog.close(); await confirmed_product_write()
                    ui.button("실행", on_click=run_write).props("color=negative")
            dialog.open()

        async def remote_verify():
            try:
                result = await asyncio.to_thread(state["service"].verify_products, state["run_id"])
                ui.notify(f"Shopify API 재조회 검증: {result['status']}", type="positive")
            except Exception as exc: ui.notify(_safe_error(exc), type="negative")
            refresh()

        async def collection_preview():
            try:
                result = await asyncio.to_thread(state["service"].preview_collections, state["run_id"])
                ui.notify(f"최대 {len(result['items'])}개 컬렉션 미리보기 완료 · 별도 확인 전 write 없음", type="positive")
            except Exception as exc: ui.notify(_safe_error(exc), type="negative")
            refresh()

        with ui.row().classes("flex-wrap"):
            ui.button("GATE A · READ-ONLY PREFLIGHT", on_click=preflight, icon="verified_user").props("outline")
            ui.button("GATE B · 10개 DRAFT 미리보기", on_click=product_preview, icon="preview").props("outline")
            write_button = ui.button("GATE C · 10개 실제 업로드", on_click=request_product_write, icon="upload").props("color=negative").disable()
            verify_button = ui.button("GATE D · Shopify API 재조회 검증", on_click=remote_verify, icon="fact_check").props("outline").disable()
            collection_button = ui.button("GATE E · 최대 3개 컬렉션 미리보기", on_click=collection_preview, icon="collections_bookmark").props("outline").disable()
        with ui.expansion("이후 안전 gate", icon="security").classes("w-full"):
            ui.label("GATE F Shop branch 내비게이션 → Logo/Favicon 현황 → Hero/Category 미리보기 → 별도 Theme 확인 → StoreCompletion")
            ui.label("정책은 사업정보 누락 시 REQUIRES_BUSINESS_INPUT, Shipping/Tax/Payment/Domain은 READ ONLY입니다.")
        table
        refresh()

    def _legacy_shopify_pilot(self):
        ui = self.ui
        self._heading("Cabin Tidy 실전 테스트", "최대 20개, DRAFT 상품만 대상으로 하는 통제된 Shopify 파일럿입니다.")
        from ..shopify_pilot import ShopifyLivePilot
        with ui.card().classes("w-full border-2 border-amber-200 bg-amber-50"):
            connection = ui.label("Shopify: 검사 전")
            scope_state = ui.label("상품 권한: 검사 전 · 판매가: Store pricing 값을 사용")
            limit = ui.number("상품 수", value=10, min=1, max=20).classes("w-40")
            ui.label("상태: DRAFT (고정) · 업로드: DIRECT_SHOPIFY · 이미지 업로드: OFF (기본)").classes("font-semibold")
            include_images = ui.checkbox("기존 media mode 이미지 업로드 사용 (권리 확인 필수)", value=False)
            source_rights = ui.checkbox("원본 이미지 사용 권리를 확인했습니다", value=False)
            live_mode = ui.checkbox("LIVE MODE 활성화", value=False)
            state = {"ready": False, "preview": None, "pilot_id": None}
            upload_button = None
            collection_button = None
            def refresh_pilot_buttons():
                if upload_button is not None:
                    if state["ready"] and state["preview"] and live_mode.value: upload_button.enable()
                    else: upload_button.disable()
                if collection_button is not None:
                    if state.get("verified"): collection_button.enable()
                    else: collection_button.disable()
            live_mode.on_value_change(lambda _event: refresh_pilot_buttons())
            table = ui.table(columns=[{"name": key, "label": label, "field": key, "align": "left"} for key, label in (
                ("source_id", "ASIN / Source ID"), ("title", "상품명"), ("source_price", "Source 가격"),
                ("selling_price", "ShopSource 판매가"), ("currency", "통화"), ("status", "상태"),
                ("action", "Shopify 작업"), ("collection_tags", "컬렉션 tag"), ("media_mode", "이미지"), ("warning", "주의"))], rows=[], row_key="master_product_id").classes("w-full")
            result_label = ui.label("아직 파일럿 미리보기가 없습니다.").classes("font-medium")
            details = ui.column().classes("w-full")

            async def check_connection():
                try:
                    result = await asyncio.to_thread(ShopifyLivePilot().connection_preflight, self.current_store)
                    state["ready"] = result["product_ready"]
                    refresh_pilot_buttons()
                    connection.set_text(f"Shopify: {'CONNECTED' if result['credential_present'] and result['shop_domain'] and result['api_version_ready'] else '미연결/버전 확인 필요'} · {result.get('shop_domain') or '도메인 없음'} · API {result.get('api_version') or '미설정'}")
                    missing = ", ".join(result["missing_product_scopes"]) or "충족"
                    scope_state.set_text(f"상품 권한: {'READY' if state['ready'] else '부족 — ' + missing} · 컬렉션 별도 권한: {', '.join(result['missing_collection_scopes']) or '충족'}")
                    if result.get("errors"): ui.notify("; ".join(result["errors"]), type="warning")
                    else: ui.notify("토큰 값은 표시하지 않고 연결/권한만 확인했습니다.", type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            async def make_preview():
                if not state["ready"]:
                    ui.notify("먼저 Shopify 연결 및 read_products/write_products 권한을 확인하세요.", type="warning"); return
                try:
                    service = ShopifyLivePilot()
                    state["verified"] = False
                    media_mode = "SOURCE_MEDIA" if include_images.value else "MANUAL_MEDIA"
                    result = await asyncio.to_thread(service.preview, self.current_store, limit=int(limit.value or 10),
                                                     media_mode=media_mode, source_media_rights_confirmed=bool(source_rights.value))
                    state["preview"] = result
                    state["pilot_id"] = result["pilot_run_id"]
                    refresh_pilot_buttons()
                    table.rows = [{**item, "collection_tags": ", ".join(item["collection_tags"])} for item in result["items"]]
                    table.update()
                    actions = result["counts"]
                    result_label.set_text(f"선택 {result['requested']} · 생성 {actions.get('CREATE', 0)} · 업데이트 {actions.get('UPDATE', 0)} · 변경 없음 {actions.get('NO CHANGE', 0)} · 건너뜀 {actions.get('SKIP', 0)} · 충돌 {actions.get('CONFLICT', 0)} · DRAFT")
                    ui.notify("10개 파일럿 미리보기를 만들었습니다. Shopify 변경 없음.", type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            async def execute_confirmed():
                preview = state["preview"]
                if not live_mode.value or not preview:
                    ui.notify("LIVE MODE와 최신 미리보기가 필요합니다.", type="warning"); return
                result = await asyncio.to_thread(ShopifyLivePilot().execute, preview["run_id"], live_confirmed=True)
                result_label.set_text("파일럿 완료 · " + " · ".join(f"{key} {value}" for key, value in result.get("counts", {}).items()))
                actual_verified = result.get("counts", {}).get("SYNCED", 0) + result.get("counts", {}).get("NO CHANGE", 0)
                expected_verified = sum(preview.get("counts", {}).get(key, 0) for key in ("CREATE", "UPDATE", "NO CHANGE"))
                state["verified"] = (actual_verified == expected_verified and expected_verified > 0
                                      and not result.get("counts", {}).get("FAILED", 0)
                                      and not result.get("counts", {}).get("VERIFY_FAILED", 0))
                refresh_pilot_buttons()
                details.clear()
                with details:
                    ui.label("Shopify에서 확인: 상품 ID · 제목 · DRAFT 상태 · 판매가 · ShopSource tag · variant 매핑").classes("text-sm")
                ui.notify("Shopify 파일럿 동기화가 끝났습니다. 결과는 read-after-write 검증을 포함합니다.", type="positive")

            async def request_live():
                if not state["ready"] or not state["preview"] or not live_mode.value:
                    ui.notify("연결 검사, 미리보기, LIVE MODE를 먼저 완료하세요.", type="warning"); return
                with ui.dialog() as dialog, ui.card():
                    ui.label(f"{self.current_store}에 DRAFT 상품 최대 {state['preview']['limit']}개를 실제 생성/수정합니다.")
                    with ui.row():
                        ui.button("취소", on_click=dialog.close).props("outline")
                        async def run_live():
                            dialog.close()
                            try: await execute_confirmed()
                            except Exception as exc: ui.notify(_safe_error(exc), type="negative")
                        ui.button("실행", on_click=run_live).props("color=negative")
                dialog.open()

            async def collection_preview():
                pilot_id = state["pilot_id"]
                if not pilot_id:
                    ui.notify("먼저 pilot 상품을 업로드하고 검증해야 합니다.", type="warning"); return
                try:
                    result = await asyncio.to_thread(ShopifyLivePilot().collection_preview, self.current_store, pilot_id)
                    ui.notify(f"컬렉션 미리보기 완료: {result['collection_preview']['counts']} · Shopify write 없음", type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="warning")

            with ui.row().classes("flex-wrap"):
                ui.button("Shopify 연결 검사", on_click=check_connection, icon="verified_user").props("outline")
                ui.button("10개 미리보기", on_click=make_preview, icon="preview").props("outline")
                upload_button = ui.button("10개 실제 업로드", on_click=request_live, icon="upload").props("color=primary").disable()
                collection_button = ui.button("파일럿 컬렉션 미리보기", on_click=collection_preview, icon="collections_bookmark").props("outline").disable()
            result_label
            table
            details

    def _brand_automation(self):
        ui=self.ui
        from ..brand_automation import (BrandThemeService,approve_asset,brand_name_prompt,brand_profile_from_store,
            compose_horizontal_logo,derive_favicon,generate_logo_mark,get_brand_profile,list_brand_assets,
            logo_mark_prompt,manual_theme_instructions,register_manual_asset_bytes,suggest_brand_names,validate_manual_assets,
            brand_name_state,export_brand_prompts,import_brand_name_candidates,lock_brand_name,begin_brand_name_change,
            derive_initial_favicon,logo_preview_report,validate_favicon,logo_prompt,brand_identity_evidence)
        from ..collection_images import OpenAIImagesProvider
        self._heading("브랜드 · 로고 · 파비콘", "Store Profile에서 브랜드 방향을 만들고, 승인된 자산만 Shopify Theme에 제안합니다.")
        state={"profile":get_brand_profile(self.current_store),"candidates":suggest_brand_names(self.current_store),"preview":None,"uploaded":[],"backup_id":None}
        if not state["profile"]:
            try:state["profile"]=brand_profile_from_store(self.current_store)
            except Exception:pass
        profile_card=ui.label("브랜드 profile 불러오는 중")
        status_card=ui.label("로고: MISSING · 파비콘: MISSING · Shopify Theme: MANUAL ACTION")
        asset_area=ui.column().classes("w-full gap-2")
        candidate_area=ui.column().classes("w-full gap-2")
        prompt_area=ui.textarea("생성 프롬프트", value="").props("readonly autogrow").classes("w-full")
        candidate_json=ui.textarea("외부 AI에서 받은 후보 JSON 붙여넣기", value="").props("autogrow").classes("w-full")
        clearance_confirm=ui.checkbox("도메인 및 상표를 직접 별도 검토했으며 법적 사용 가능 판정이 아님을 이해합니다.",value=False)
        candidate_select=ui.select({},label="브랜드명 후보 (선택 시에만 변경)").classes("w-96")
        asset_select=ui.select({},label="승인할 자산").classes("w-96")
        mark_select=ui.select({},label="로고 mark source").classes("w-96")
        favicon_select=ui.select({},label="favicon source mark (APPROVED)").classes("w-96")
        favicon_initial=ui.select({"C":"C 이니셜","CT":"CT 이니셜"},label="파비콘 이니셜 직접 선택 (선택사항)").classes("w-72")
        logo_upload=ui.upload(label="로고 파일 선택",auto_upload=True,on_upload=lambda e:save_manual_upload(e,"LOGO_HORIZONTAL")).props("accept=.png,.jpg,.jpeg,.svg").classes("w-full")
        favicon_upload=ui.upload(label="파비콘 파일 선택",auto_upload=True,on_upload=lambda e:save_manual_upload(e,"FAVICON_32")).props("accept=.png").classes("w-full")
        opt_in=ui.checkbox("이미지 자동 생성 사용 (유료 이미지 API opt-in)",value=False)
        transparent=ui.checkbox("거의 흰색 배경을 투명 처리",value=False)
        ui.label("로고 설계 입력 · 스토어명은 현재 Brand Profile에서 가져옵니다.").classes("text-base font-semibold")
        category=ui.input("주요 판매 카테고리").classes("w-96")
        customer=ui.input("주요 고객층").classes("w-96")
        desired_image=ui.input("원하는 브랜드 이미지 / 느낌 / 키워드").classes("w-96")
        primary=ui.input("선호 색상 · Primary",value="#24364B").classes("w-48")
        secondary=ui.input("선호 색상 · Secondary",value="#FFFFFF").classes("w-48")
        accent=ui.input("선호 색상 · Accent",value="#D7C7A6").classes("w-48")
        avoid_colors=ui.input("피하고 싶은 색상 (쉼표 구분)").classes("w-96")
        avoid_styles=ui.input("피하고 싶은 스타일 (쉼표 구분)").classes("w-96")
        with ui.expansion("고급 설정",icon="tune"):
            country=ui.input("판매 국가").classes("w-48")
            keywords=ui.input("브랜드 키워드 (쉼표 구분)").classes("w-96")
            model=ui.input("OpenAI image model",value="gpt-image-1").classes("w-72")
        generated=ui.label("이미지 자동 생성 예상 호출: 로고 mark 1회 · 파비콘 0회 (mark에서 결정적 파생)").classes("text-sm text-slate-600")

        def refresh():
            profile=get_brand_profile(self.current_store);state["profile"]=profile
            assets=list_brand_assets(self.current_store)
            name=profile["profile"]["brand_name"] if profile else "미설정"
            name_state=brand_name_state(self.current_store)
            direction=" · ".join(str(x) for x in (profile["profile"].get("personality") or [])) if profile else ""
            profile_card.set_text(f"브랜드: {name} · 이름 {name_state.get('name_status','REVIEW_REQUIRED')} · 방향: {direction} · Profile v{profile['version'] if profile else '-'} · {profile['approval_status'] if profile else 'DRAFT'}")
            logo=next((x for x in reversed(assets) if x["asset_type"]=="LOGO_HORIZONTAL" and x["approval_status"]=="APPROVED"),None)
            fav=next((x for x in reversed(assets) if x["asset_type"]=="FAVICON_32" and x["approval_status"]=="APPROVED"),None)
            identity=brand_identity_evidence(self.current_store)
            logo_state=identity["logo"]
            fav_state=identity["favicon"]
            status_card.set_text(f"이름: {name_state.get('name_status','REVIEW_REQUIRED')} · 로고: {logo_state} · 파비콘: {fav_state} · Shopify Theme: {'PREVIEW 생성됨' if state['preview'] else 'MANUAL ACTION / 검사 전'}")
            options={x["asset_id"]:f"{x['asset_type']} v{x['version']} · {x['approval_status']}" for x in assets}
            asset_select.options=options;mark_select.options={x["asset_id"]:f"{x['asset_type']} v{x['version']}" for x in assets if x["asset_type"]=="LOGO_MARK"}
            favicon_select.options={x["asset_id"]:f"{x['asset_type']} v{x['version']}" for x in assets if x["asset_type"]=="LOGO_MARK" and x["approval_status"]=="APPROVED"}
            candidate_select.options={x["brand_name"]:f"{x['brand_name']} · {x['direction']}" for x in state.get("candidates",[]) if x.get("shortlisted")}
            asset_select.update();mark_select.update();favicon_select.update();candidate_select.update()
            asset_area.clear()
            with asset_area:
                for asset in assets:
                    ui.label(f"{asset['asset_type']} v{asset['version']} · {asset['width']}×{asset['height']} {asset['format']} · {asset['approval_status']} · {asset['sha256'][:12]}…")
                    if Path(asset["local_path"]).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".svg"}:
                        ui.image(asset["local_path"]).classes("max-w-md max-h-48 object-contain bg-white rounded border")
                        if asset["asset_type"] == "LOGO_HORIZONTAL" and asset["format"] != "SVG":
                            try:
                                report=logo_preview_report(asset)
                                ui.label(f"Desktop header 축소 {report['desktop_header_width']}px · Mobile {report['mobile_header_width']}px · 여백 {report['safe_padding_ratio']:.1%} · {report['technical_status']} · 최종 시각 승인 필요").classes("ss-help")
                                ui.image(asset["local_path"]).style("width:300px;max-height:80px;object-fit:contain;background:#fff;padding:8px;border:1px solid #ddd")
                                ui.image(asset["local_path"]).style("width:168px;max-height:56px;object-fit:contain;background:#24364b;padding:8px;border:1px solid #ddd")
                            except Exception: pass
                    if asset["asset_type"] == "FAVICON_32" and asset["format"] == "PNG":
                        try:
                            check=validate_favicon(asset)
                            ui.label(f"32×32 enlarged preview · safe padding {check['safe_padding_ratio']:.1%} · coverage {check['coverage_ratio']:.1%} · {check['technical_status']} · 시각 검수 필요").classes("ss-help")
                            ui.image(asset["local_path"]).style("width:128px;height:128px;image-rendering:pixelated;background:#fff;padding:16px;border:1px solid #ddd")
                        except Exception: pass
            candidate_area.clear()
            with candidate_area:
                for candidate in state.get("candidates",[]):
                    shortlist=" · 전략 shortlist" if candidate.get("shortlisted") else ""
                    ui.label(f"{candidate['brand_name']} ({candidate['pronunciation']}){shortlist} · {candidate['review_status']}").classes("font-semibold")
                    ui.label(f"{candidate['meaning_and_rationale']} · 이미지: {candidate['brand_image']} · 방향: {candidate['direction']} · 기본 충돌: {len(candidate.get('conflicts',[]))}건").classes("ss-help")

        async def design_profile():
            try:
                overrides={"primary_category":category.value or None,"target_customer":customer.value or None,"target_country":country.value or None,
                           "brand_keywords":[x.strip() for x in (keywords.value or "").split(",") if x.strip()],
                           "desired_brand_image":desired_image.value or "",
                           "avoid_colors":[x.strip() for x in (avoid_colors.value or "").split(",") if x.strip()],
                           "avoid_styles":[x.strip() for x in (avoid_styles.value or "").split(",") if x.strip()] or ["3D","watermarks","busy patterns","thin details"],
                           "colors":{"primary":primary.value,"secondary":secondary.value,"accent":accent.value}}
                overrides={k:v for k,v in overrides.items() if v not in (None,[])}
                state["profile"]=brand_profile_from_store(self.current_store,overrides=overrides)
                refresh();ui.notify("BrandProfile을 저장했습니다. 기존 브랜드명은 자동 변경하지 않습니다.",type="positive")
            except Exception as exc:ui.notify(_safe_error(exc),type="negative")

        async def recommend_names():
            try:
                prompt_area.value=brand_name_prompt(self.current_store)
                state["prompt_exports"]=export_brand_prompts(self.current_store)
                state["candidates"]=suggest_brand_names(self.current_store)
                refresh();ui.notify(f"이름 후보 prompt를 내보냈습니다: {state['prompt_exports']['brand_name_prompt']} · 도메인/상표는 별도 검토",type="positive")
            except Exception as exc:ui.notify(_safe_error(exc),type="negative")

        async def import_names():
            try:
                state["candidates"]=import_brand_name_candidates(self.current_store,candidate_json.value)
                refresh();ui.notify("정확히 10개 후보와 3개 전략 shortlist를 검증해 저장했습니다.",type="positive")
            except Exception as exc:ui.notify(_safe_error(exc),type="negative")

        async def choose_name():
            if not candidate_select.value:return
            if not clearance_confirm.value:ui.notify("도메인·상표를 직접 별도 검토했음을 체크하세요.",type="warning");return
            try:state["profile"]=lock_brand_name(self.current_store,candidate_select.value,confirmed=True,clearance_reviewed=True);refresh()
            except Exception as exc:ui.notify(_safe_error(exc),type="negative")

        async def show_logo_prompt():
            try:
                state["profile"]=get_brand_profile(self.current_store) or brand_profile_from_store(self.current_store)
                state["prompt_exports"]=export_brand_prompts(self.current_store)
                prompt_area.value="LOGO MARK (text-free)\n"+logo_mark_prompt(state["profile"])+"\n\nHORIZONTAL LOGO GUIDANCE\n"+logo_prompt(state["profile"])
                ui.notify("정확한 워드마크는 이미지 모델이 아니라 프로그램이 합성합니다.",type="positive")
            except Exception as exc:ui.notify(_safe_error(exc),type="negative")

        async def generate_mark():
            if not opt_in.value:ui.notify("유료 호출 전에 이미지 자동 생성 opt-in이 필요합니다.",type="warning");return
            try:
                profile=get_brand_profile(self.current_store) or brand_profile_from_store(self.current_store)
                mark=await asyncio.to_thread(generate_logo_mark,self.current_store,provider=OpenAIImagesProvider(model=model.value or "gpt-image-1"),enabled=True,model=model.value or "gpt-image-1")
                state["last_mark"]=mark["asset_id"]
                logo=compose_horizontal_logo(self.current_store,mark["asset_id"])
                prompt_area.value=f"생성 mark: {mark['local_path']}\n정확한 워드마크: {profile['profile']['brand_name']}\n수평 로고: {logo['local_path']}"
                refresh();ui.notify("심볼과 exact wordmark를 로컬 합성했습니다. 승인 전에는 Shopify 적용 불가.",type="positive")
            except Exception as exc:ui.notify(_safe_error(exc),type="negative")

        async def compose_logo():
            mark_id=mark_select.value or state.get("last_mark")
            if not mark_id:ui.notify("먼저 logo mark 파일을 선택하거나 생성하세요.",type="warning");return
            try:
                result=await asyncio.to_thread(compose_horizontal_logo,self.current_store,mark_id);refresh();ui.notify(f"가로 로고 생성: {result['local_path']}",type="positive")
            except Exception as exc:ui.notify(_safe_error(exc),type="negative")

        async def make_favicon():
            mark_id=favicon_select.value
            if not mark_id:ui.notify("승인된 LOGO_MARK를 선택하세요. 전체 wordmark 축소는 금지됩니다.",type="warning");return
            try:result=await asyncio.to_thread(derive_favicon,self.current_store,mark_id,transparent_white=bool(transparent.value));refresh();ui.notify(f"32×32 파비콘 생성: {result['favicon_32']['local_path']}",type="positive")
            except Exception as exc:ui.notify(_safe_error(exc),type="negative")

        async def make_initial_favicon():
            if favicon_initial.value not in {"C","CT"}:ui.notify("먼저 C 또는 CT를 직접 선택하세요.",type="warning");return
            try:
                await asyncio.to_thread(derive_initial_favicon,self.current_store,favicon_initial.value,confirmed=True)
                refresh();ui.notify("선택한 이니셜로 파비콘 초안을 만들었습니다. 시각 검토 후 승인하세요.",type="positive")
            except Exception as exc:ui.notify(_safe_error(exc),type="negative")

        async def request_name_change():
            with ui.dialog() as dialog,ui.card():
                ui.label("브랜드명 변경은 기존 이름을 바꾸는 별도 작업입니다. 기존 로고/파비콘 파일은 삭제하지 않지만 favicon identity 검토가 필요해집니다.")
                confirmed=ui.checkbox("이름 변경 절차를 시작하겠습니다.",value=False)
                async def proceed():
                    if not confirmed.value:ui.notify("확인란을 선택하세요.",type="warning");return
                    try:
                        begin_brand_name_change(self.current_store,confirmed=True)
                        dialog.close();refresh();ui.notify("이름 변경 검토 상태로 전환했습니다. 기존 자산은 보존됩니다.",type="warning")
                    except Exception as exc:ui.notify(_safe_error(exc),type="negative")
                ui.button("취소",on_click=dialog.close).props("outline")
                ui.button("변경 절차 시작",on_click=proceed).props("color=negative")
            dialog.open()

        async def approve_selected():
            if not asset_select.value:return
            try:await asyncio.to_thread(approve_asset,asset_select.value);refresh();ui.notify("자산 승인 완료. 이전 동일 type 승인본은 SUPERSEDED 처리했습니다.",type="positive")
            except Exception as exc:ui.notify(_safe_error(exc),type="negative")

        async def save_manual_upload(event, asset_type):
            try:
                content=await event.file.read();asset=await asyncio.to_thread(register_manual_asset_bytes,self.current_store,asset_type,event.file.name,content)
                refresh();ui.notify(f"직접 선택 자산 저장: {asset['asset_type']} · NEEDS_REVIEW",type="positive")
            except Exception as exc:ui.notify(_safe_error(exc),type="negative")

        async def validate_files():
            results=validate_manual_assets(self.current_store);ui.notify(json.dumps(results,ensure_ascii=False),type="positive" if all(x["valid"] for x in results) else "warning")

        async def upload_assets():
            assets=list_brand_assets(self.current_store);approved=[x for x in assets if x["approval_status"]=="APPROVED" and x["asset_type"] in {"LOGO_HORIZONTAL","FAVICON_32"}]
            outcomes=[]
            for asset in approved:
                try:outcomes.append(await asyncio.to_thread(BrandThemeService().upload_approved_asset,self.current_store,asset["asset_id"]))
                except Exception as exc:outcomes.append({"asset_type":asset["asset_type"],"error":_safe_error(exc)})
            state["uploaded"]=outcomes;refresh();ui.notify(f"Shopify file 업로드 개별 결과: {sum(bool(x.get('shopify_file_id')) for x in outcomes)} 성공 / {len(outcomes)} 시도",type="positive" if outcomes and all(x.get("shopify_file_id") for x in outcomes) else "warning")

        async def make_apply_preview():
            assets=list_brand_assets(self.current_store)
            logo=next((x for x in reversed(assets) if x["asset_type"]=="LOGO_HORIZONTAL" and x["approval_status"]=="APPROVED"),None)
            fav=next((x for x in reversed(assets) if x["asset_type"]=="FAVICON_32" and x["approval_status"]=="APPROVED"),None)
            if not logo or not fav:ui.notify("승인된 가로 로고와 32×32 파비콘이 필요합니다.",type="warning");return
            try:
                result=await asyncio.to_thread(BrandThemeService().preview_apply,self.current_store,logo_asset_id=logo["asset_id"],favicon_asset_id=fav["asset_id"])
                state["preview"]=result
                ui.notify(" · ".join(f"{x['action']} {x.get('kind','')} conf={x.get('confidence','-')}" for x in result["actions"]),type="positive" if result["status"]=="PREVIEW" else "warning")
                for item in result["actions"]:ui.label(f"{item['action']} · {item.get('kind')} · current={item.get('current')} · proposed={item.get('proposed')} · confidence={item.get('confidence')}")
            except Exception as exc:ui.notify(_safe_error(exc),type="negative")

        async def apply_theme():
            preview=state["preview"]
            if not preview or preview["status"]!="PREVIEW":ui.notify("High-confidence 최신 preview와 write_themes 권한이 필요합니다.",type="warning");return
            with ui.dialog() as dialog,ui.card():
                ui.label("승인된 로고/파비콘만 theme settings_data.json에 최소 반영합니다. 적용 전 백업 후 재조회합니다.")
                with ui.row():
                    ui.button("취소",on_click=dialog.close).props("outline")
                    async def confirmed():
                        dialog.close()
                        try:
                            result=await asyncio.to_thread(BrandThemeService().apply,preview["preview_id"],confirmed=True)
                            state["backup_id"]=result.get("backup_id")
                            ui.notify(f"Theme 결과: {result['status']}",type="positive" if result["status"]=="VERIFIED" else "negative")
                        except Exception as exc:ui.notify(_safe_error(exc),type="negative")
                    ui.button("적용",on_click=confirmed).props("color=negative")
            dialog.open()

        async def rollback_theme():
            backup_id=state.get("backup_id")
            if not backup_id:ui.notify("이 세션에서 확인된 Theme backup이 없습니다.",type="warning");return
            with ui.dialog() as dialog,ui.card():
                ui.label("백업된 settings_data.json을 Shopify Theme에 복원합니다. 최신 설정은 백업 시점으로 돌아갑니다.")
                with ui.row():
                    ui.button("취소",on_click=dialog.close).props("outline")
                    async def confirmed():
                        dialog.close()
                        try:
                            result=await asyncio.to_thread(BrandThemeService().rollback,backup_id,confirmed=True)
                            ui.notify(f"롤백 검증: {result['status']}",type="positive" if result["status"]=="VERIFIED" else "negative")
                        except Exception as exc:ui.notify(_safe_error(exc),type="negative")
                    ui.button("백업 복원",on_click=confirmed).props("color=negative")
            dialog.open()

        async def manual_instructions():
            assets=list_brand_assets(self.current_store);logo=next((x for x in reversed(assets) if x["asset_type"]=="LOGO_HORIZONTAL"),None);fav=next((x for x in reversed(assets) if x["asset_type"]=="FAVICON_32"),None)
            ui.notify("\n".join(manual_theme_instructions(self.current_store,logo,fav)["steps"]),type="info")

        with ui.card().classes("w-full border-2 border-violet-200 bg-violet-50"):
            profile_card;status_card
            ui.label("과제 모드: 승인 전에는 자동 적용되지 않습니다. 업로드 후에는 Theme Editor에서 저장·표시를 확인하세요.").classes("text-sm")
            with ui.row().classes("flex-wrap"):
                ui.button("브랜드 자동 설계",on_click=design_profile)
                ui.button("STEP 1 · 이름 prompt 생성",on_click=recommend_names).props("outline")
                ui.button("후보 JSON 검증/import",on_click=import_names).props("outline")
                ui.button("선택한 후보 잠금",on_click=choose_name).props("outline")
                ui.button("브랜드명 변경 시작",on_click=request_name_change).props("outline color=negative")
                ui.button("로고 프롬프트 생성",on_click=show_logo_prompt).props("outline")
                ui.button("로고 생성",on_click=generate_mark).props("color=primary")
                ui.button("가로 wordmark 합성",on_click=compose_logo).props("outline")
                ui.button("STEP 4 · 파비콘 prompt 생성",on_click=lambda:prompt_area.set_value(__import__('shopsource.brand_automation',fromlist=['favicon_prompt']).favicon_prompt(get_brand_profile(self.current_store),source_logo_mark_id=favicon_select.value) if favicon_select.value else __import__('shopsource.brand_automation',fromlist=['favicon_prompt']).favicon_prompt(get_brand_profile(self.current_store),selected_initial=favicon_initial.value))).props("outline")
                ui.button("파비콘 생성",on_click=make_favicon).props("outline")
                ui.button("선택한 C/CT로 favicon 초안",on_click=make_initial_favicon).props("outline")
                ui.button("승인",on_click=approve_selected).props("color=positive")
                ui.button("파일 검사",on_click=validate_files).props("outline")
                ui.button("승인 자산을 Shopify Files에 업로드",on_click=upload_assets).props("outline")
                ui.button("적용 미리보기",on_click=make_apply_preview).props("outline")
                ui.button("Shopify에 적용",on_click=apply_theme).props("color=negative")
                ui.button("롤백",on_click=rollback_theme).props("outline color=negative")
                ui.button("수동 적용 안내",on_click=manual_instructions).props("outline")
            candidate_json
            candidate_area
            candidate_select;clearance_confirm;asset_select;mark_select;favicon_select;favicon_initial
            logo_upload;favicon_upload;opt_in;transparent;generated
            prompt_area;asset_area
        refresh()

    def _navigation(self):
        ui=self.ui
        from ..navigation import inspect_assignment_menu
        self._heading("메가메뉴 자동화", "컬렉션 플랜을 Shopify 메뉴로 연결하고 published theme의 메가메뉴 지원을 점검합니다.")
        ui.label("미리보기는 읽기 전용입니다. Navigation sync와 theme 적용은 각각 별도 확인 후에만 실행됩니다.").classes("text-sm text-amber-800")
        state={"plan":None,"preview":None,"theme_preview":None,"menu":None}
        nav=NavigationService();theme_service=MegaMenuThemeService()
        with ui.card().classes("w-full border-2 border-violet-200 bg-violet-50"):
            ui.label(f"Store: {self.current_store} | {next((row['store_name'] for row in self.stores if row['store_id']==self.current_store),self.current_store)}").classes("text-xl font-bold")
            status=ui.label("Navigation: 검사 전 · Collections: — · Theme: 미검사")
            parent=ui.input("상위 메뉴 label",value="Shop").classes("w-64")
            with ui.expansion("고급 설정",icon="tune"):
                parent_url=ui.select({"/collections/all":"모든 컬렉션 (/collections/all)","/collections":"컬렉션 (/collections)"},value="/collections/all",label="상위 Shop 링크").classes("w-96")
            tree=ui.column().classes("w-full gap-1")
            action_area=ui.column().classes("w-full gap-1")
            checklist=ui.column().classes("w-full gap-1")
            theme_area=ui.column().classes("w-full gap-1")

            def render_tree(items):
                tree.clear()
                with tree:
                    def add(item,depth=0):
                        ui.label(f"{'　'*depth}{'├─ ' if depth else ''}{item.get('title','')} → {item.get('url') or item.get('resourceId') or '링크 미지정'}").classes("text-sm")
                        for child in item.get("items",[]):add(child,depth+1)
                    for item in items:add(item)

            def ensure_collection_plan_from_queue():
                automation = _workflow_service()
                active = next((item for item in automation.interrupted(self.current_store)
                    if item.get("workflow_key") == "HOMEPAGE_COLLECTION_PREREQUISITE"), None)
                if active and active.get("status") == "WAITING_FOR_INPUT":
                    status = automation.confirm(active["run_id"], "ENSURE_COLLECTION_PLAN", user_input={"continue": True})
                    run_id = active["run_id"]
                elif active:
                    run_id = active["run_id"]
                    status = automation.run(run_id)
                else:
                    run = automation.create_run(self.current_store, "HOMEPAGE_COLLECTION_PREREQUISITE", collection_prerequisite_workflow(self.current_store))
                    run_id = run["run_id"]
                    status = automation.run(run_id)
                state["collection_prerequisite_run_id"] = run_id
                if status.get("status") in {"WAITING_FOR_INPUT", "FAILED"}:
                    return {"status": "WAITING_FOR_INPUT", "reason": (status.get("current_task") or {}).get("error_message")}
                result = automation.task_result(run_id, "ENSURE_COLLECTION_PLAN").get("result") or {}
                plan_id = result.get("plan_id")
                plan = CollectionPlanner().get_plan(plan_id) if plan_id else None
                return {"status": "READY" if plan else "WAITING_FOR_INPUT", "plan": plan,
                        "source": result.get("source"), "created_at": result.get("created_at")}

            def show_prerequisite_status(collection_status="○ Collection Plan"):
                end = "✓ Homepage Plan   ✓ Prompt Set" if state.get("plan") and state.get("prompt_set") else "○ Homepage Plan   ○ Prompt Set"
                prereq_status.set_text(f"✓ Brand Profile   {collection_status}   {end}")

            def design():
                try:
                    plan=nav.build_plan(self.current_store,options={"parent_label":parent.value or "Shop","parent_url":parent_url.value or "/collections/all"})
                    state["plan"]=plan;state["preview"]=None
                    rows=plan["items"];render_tree([{"title":rows[0]["title"],"url":rows[0]["url"],"items":[{"title":x["title"],"url":x["url"],"warning":x["sync_status"]} for x in rows[1:]]}])
                    total=len(rows)-1;mapped=sum(item["sync_status"]=="PLANNED" for item in rows[1:])
                    status.set_text(f"Navigation: NEEDS REVIEW · Collections: {mapped}/{total} mapped · Theme: 미검사")
                    ui.notify(f"메뉴 계획 v{plan['plan_version']} 저장. Shopify write는 수행하지 않았습니다.",type="positive")
                except Exception as exc:ui.notify(_safe_error(exc),type="negative")

            def preview():
                try:
                    if not state["plan"]:raise ValueError("먼저 메뉴 자동 설계를 실행하세요.")
                    result=nav.preview(state["plan"]["plan_id"]);state["preview"]=result;render_tree(result.get("tree",[]));action_area.clear()
                    with action_area:
                        ui.label(" · ".join(f"{key} {value}" for key,value in result.get("summary",{}).items())).classes("font-semibold")
                        for action in result.get("actions",[]):ui.label(f"{action.get('action')} · {action.get('title',action.get('item_key',''))} · {action.get('target',action.get('warning',action.get('reason','')))}").classes("text-sm")
                    status.set_text(f"Navigation: {result['status']} · CREATE {result.get('summary',{}).get('CREATE',0)} / UPDATE {result.get('summary',{}).get('UPDATE',0)} / MOVE {result.get('summary',{}).get('MOVE',0)} / NO CHANGE {result.get('summary',{}).get('NO CHANGE',0)}")
                    ui.notify(f"메뉴 미리보기 {result['status']}",type="positive" if result["status"]=="READY" else "warning")
                except Exception as exc:ui.notify(_safe_error(exc),type="negative")

            async def sync():
                review=state.get("preview")
                if not review or review.get("status")!="READY":ui.notify("충돌 없는 최신 미리보기가 필요합니다.",type="warning");return
                with ui.dialog() as dialog,ui.card():
                    ui.label("Main menu의 ShopSource 관리 하위 항목을 생성/갱신합니다. 기존 다른 항목은 보존됩니다.")
                    with ui.row():
                        ui.button("취소",on_click=dialog.close).props("outline")
                        async def confirm():
                            dialog.close()
                            try:
                                result=await asyncio.to_thread(nav.sync,review["preview_id"],confirmed=True)
                                ui.notify(f"Navigation sync: {result['status']}",type="positive" if result["status"] in {"VERIFIED","NO_CHANGE"} else "negative")
                            except Exception as exc:ui.notify(_safe_error(exc),type="negative")
                        ui.button("동기화",on_click=confirm).props("color=negative")
                dialog.open()

            def inspect():
                checklist.clear()
                try:
                    snapshot=nav.discover_main_menu(self.current_store);state["menu"]=snapshot.get("menu")
                    expected=[item for item in state.get("plan",{}).get("items",[]) if item.get("depth")==1]
                    checks=inspect_assignment_menu(snapshot.get("menu"),expected)
                    with checklist:
                        ui.label(f"현재 메뉴 {snapshot['status']} · Shop parent {'있음' if checks['parent_exists'] else '없음'} · 하위 항목 {checks['child_count']}개")
                        checks_by_code={finding.get("code") for finding in checks["findings"]}
                        criteria=[("Shop parent 존재",not "MISSING_PARENT" in checks_by_code and checks["parent_exists"]),
                                  ("child 들여쓰기",checks["children_indented"] or not expected),
                                  ("child label",all(str(item.get("title","")).strip() for item in expected)),
                                  ("링크 존재","MISSING_LINK" not in checks_by_code),
                                  ("duplicate target","DUPLICATE_TARGET" not in checks_by_code),
                                  ("missing link","MISSING_CHILD" not in checks_by_code),
                                  ("임시 placeholder link","PLACEHOLDER_LINK" not in checks_by_code)]
                        for label,passed in criteria:ui.label(f"{'✓' if passed else '⚠'} 저장 전 확인: {label}").classes("text-sm")
                        for finding in checks["findings"]:ui.label(f"WARNING {finding['code']}: {finding.get('message',finding.get('label',''))}").classes("text-amber-800")
                    ui.notify(f"링크 검사 경고 {len(checks['findings'])}개",type="warning" if checks["findings"] else "positive")
                except Exception as exc:ui.notify(_safe_error(exc),type="negative")

            def preview_theme():
                try:
                    result=theme_service.preview(self.current_store,parent_label=parent.value or "Shop");state["theme_preview"]=result;theme_area.clear()
                    with theme_area:
                        ui.label(f"Theme: {(result.get('theme') or {}).get('name','—')} · {result.get('support',{}).get('status')} · {result['status']}")
                        ui.label(json.dumps(result.get("actions",[]),ensure_ascii=False,indent=2)).classes("text-xs whitespace-pre-wrap")
                    ui.notify(f"Theme mega-menu {result['status']}",type="positive" if result["status"]=="PREVIEW" else "warning")
                except Exception as exc:ui.notify(_safe_error(exc),type="negative")

            async def apply_theme():
                review=state.get("theme_preview")
                if not review or review.get("status")!="PREVIEW":ui.notify("고신뢰 최신 Theme preview와 write 권한이 필요합니다.",type="warning");return
                with ui.dialog() as dialog,ui.card():
                    ui.label("변경 전 settings_data 백업 후 Shop 메가메뉴 설정만 적용하고 Shopify에서 다시 확인합니다.")
                    with ui.row():
                        ui.button("취소",on_click=dialog.close).props("outline")
                        async def confirm():
                            dialog.close()
                            try:
                                result=await asyncio.to_thread(theme_service.apply,review["preview_id"],confirmed=True)
                                ui.notify(f"Mega menu theme: {result['status']}",type="positive" if result["status"]=="VERIFIED" else "negative")
                            except Exception as exc:ui.notify(_safe_error(exc),type="negative")
                        ui.button("적용",on_click=confirm).props("color=negative")
                dialog.open()

            def guide():ui.notify("Online Store → Theme Edit / Customize → Header → 메가메뉴 설정/블록 → Shop 연결 → Save",type="info")

            with ui.row().classes("flex-wrap"):
                ui.button("메뉴 자동 설계",on_click=design,icon="auto_awesome").props("color=primary")
                ui.button("메뉴 미리보기",on_click=preview,icon="visibility").props("outline")
                ui.button("Shopify 메뉴 동기화",on_click=sync,icon="sync").props("color=negative")
                ui.button("링크 검사",on_click=inspect,icon="link").props("outline")
                ui.button("메가메뉴 테마 미리보기",on_click=preview_theme,icon="preview").props("outline")
                ui.button("메가메뉴 적용",on_click=apply_theme,icon="publish").props("color=negative")
                ui.button("수동 적용 안내",on_click=guide,icon="help").props("outline")
            with ui.expansion("메가메뉴 과제 모드",icon="school"):
                ui.label("현재 메뉴 검사 · Shop 하위 메뉴 자동 제안 · 링크 검사 · 저장 전 체크리스트 · 테마 적용 안내")
                ui.button("현재 메뉴 검사",on_click=inspect)
                ui.button("Shop 하위 메뉴 자동 제안",on_click=design)
                checklist
            tree;action_area;theme_area

    def _homepage_automation(self):
        ui = self.ui
        self._heading("홈페이지 자동 완성", "배너, 카테고리, 추천 컬렉션과 링크를 한 번에 준비하고 실제 적용 직전에만 멈춥니다.")
        ui.label("Preview is read-only. Shopify theme changes require a separate explicit confirmation; paid image generation stays off until opted in.").classes("text-sm text-amber-800")
        state = {"plan": None, "snapshot": None, "preview": None, "assignment_preview": None,
                 "collection_plan": None, "brand": None, "prompt_set": None, "backup_id": None,
                 "local_candidates": []}
        with ui.card().classes("w-full border-2 border-sky-200 bg-sky-50"):
            brand_name = next((row["store_name"] for row in self.stores if row["store_id"] == self.current_store), self.current_store)
            ui.label(f"Store: {self.current_store} | Brand: {brand_name}").classes("text-xl font-bold")
            summary = ui.label("Hero: not planned · Categories: not planned · Theme: not checked").classes("font-medium")
            prereq_status = ui.label("○ Brand Profile   ○ Collection Plan   ○ Homepage Plan   ○ Prompt Set").classes("ss-help")
            actions = ui.column().classes("w-full gap-2")
            preview_area = ui.column().classes("w-full gap-2")
            hero_url = ui.input("Shopify Files hero image URL (optional)").classes("w-full")
            hero_approved = ui.checkbox("선택한 Hero 이미지가 승인된 자산임을 확인", value=False)
            theme_image_ref = ui.input("Theme image_picker reference (확인된 shopify://shop_images/... 값)").classes("w-full")
            theme_ref_confirmed = ui.checkbox("Theme image_picker reference를 Theme Editor에서 확인", value=False)
            with ui.expansion("고급 설정", icon="tune"):
                max_categories = ui.number("카테고리 수 (4–8)", value=8, min=4, max=8).classes("w-48")
            local_image = ui.input("배너 이미지 검사 경로").classes("w-full")
            paid_opt_in = ui.checkbox("유료 이미지 자동 생성 사용", value=False)

            with ui.card().classes("w-full border border-indigo-200 bg-white"):
                ui.label("추천 상품(신상품)").classes("text-lg font-semibold")
                featured_status = ui.label("선택된 상품: 0 / 4 · 상태: REVIEW REQUIRED")
                featured_table = ui.column().classes("w-full gap-1")

                def render_featured(plan=None):
                    plan = plan or state.get("featured_products_plan")
                    featured_table.clear()
                    count = len((plan or {}).get("items", []))
                    featured_status.set_text(f"선택된 상품: {count} / 4 · 상태: {(plan or {}).get('status', 'REVIEW REQUIRED')}")
                    with featured_table:
                        for item in (plan or {}).get("items", []):
                            ui.label(f"{item['position']}. {item['title']} · {item.get('category_key') or '-'} · ${item.get('price') or 0:.2f} · {item['remote_status']} · /products/{item['shopify_handle']} · {item['selection_reason']} · {item['verification_status']}")
                        if not count:
                            ui.label("실제 Shopify mapping, 판매가, 이미지, ACTIVE 상태가 검증된 상품이 필요합니다.").classes("ss-help")

                def select_featured_products():
                    try:
                        service = FeaturedProductAssignmentService()
                        plan = service.create_plan(self.current_store, mode="BALANCED_CATEGORIES", requested_count=4,
                            heading="New Arrivals", subheading="Fresh picks to keep your car clean, organized, and ready to go." if self.current_store == "001" else "")
                        state["featured_products_plan"] = plan
                        render_featured(plan)
                        ui.notify("추천 상품 계획을 로컬에 저장했습니다. Shopify write는 실행하지 않았습니다.", type="positive" if plan["status"] == "READY" else "warning")
                    except Exception as exc: ui.notify(_safe_error(exc), type="negative")

                def featured_preview():
                    plan = state.get("featured_products_plan")
                    if not plan: ui.notify("먼저 추천 상품 자동 구성을 실행하세요.", type="warning"); return
                    preview = FeaturedProductAssignmentService().build_theme_preview(plan, state.get("snapshot") or {})
                    state["featured_products_preview"] = preview
                    ui.notify(f"추천 상품 미리보기: {preview['status']} · Shopify write 없음", type="positive" if preview["status"] == "PREVIEW" else "warning")

                def featured_apply_gate():
                    ui.notify("FEATURED PRODUCTS ASSIGNMENT UI READY · 실제 적용은 검토된 preview에서 명시적으로 확인해야 합니다.", type="warning")

                with ui.row().classes("flex-wrap gap-2"):
                    ui.button("추천 상품 자동 구성", on_click=select_featured_products, icon="auto_awesome").props("color=primary")
                    ui.button("상품 4개 보기", on_click=lambda: render_featured(), icon="view_list").props("outline")
                    ui.button("다시 선택", on_click=select_featured_products, icon="refresh").props("outline")
                    ui.button("추천 상품 미리보기", on_click=featured_preview, icon="preview").props("outline")
                    ui.button("Shopify 적용", on_click=featured_apply_gate, icon="publish").props("outline")
                    ui.button("과제 제출용 확인", on_click=lambda: ui.notify("원격 section 검증 후 ASSIGNMENT_READY가 됩니다.", type="info"), icon="checklist").props("outline")
                render_featured()

            async def save_hero_upload(event):
                try:
                    plan = state.get("plan")
                    if not plan: raise ValueError("이미 홈페이지 자동 설계를 먼저 실행하세요.")
                    content = await event.file.read()
                    asset = register_manual_hero_asset(self.current_store, plan["plan_id"], event.file.name, content)
                    ui.notify(f"Hero asset 저장 · {asset['approval_status']} · {asset['validation']['width']}×{asset['validation']['height']}", type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            hero_upload = ui.upload(label="배너 이미지 파일 선택", auto_upload=True, on_upload=save_hero_upload).props("accept=.png,.jpg,.jpeg,.webp").classes("w-full")

            def approve_latest_hero():
                plan = state.get("plan")
                if not plan: ui.notify("홈페이지 계획이 필요합니다.", type="warning"); return
                asset = latest_hero_asset(self.current_store, plan["plan_id"])
                if not asset: ui.notify("등록된 배너 이미지가 없습니다.", type="warning"); return
                approve_hero_asset(asset["asset_id"])
                ui.notify("배너 이미지 로컬 승인 완료. Shopify Files 업로드를 이어서 진행하세요.", type="positive")

            def upload_latest_hero():
                plan = state.get("plan")
                if not plan: ui.notify("홈페이지 계획이 필요합니다.", type="warning"); return
                asset = latest_hero_asset(self.current_store, plan["plan_id"])
                if not asset: ui.notify("등록된 배너 이미지가 없습니다.", type="warning"); return
                result = upload_approved_hero_asset(self.current_store, asset["asset_id"], alt_text=plan["hero"].get("alt_text") or "Homepage hero")
                if result.get("status") == "READY":
                    hero_url.set_value(result["shopify_url"])
                    hero_approved.set_value(True)
                    theme_image_ref.set_value(suggested_theme_image_ref(asset))
                    state["plan"]["hero"].update(image_url=result["shopify_url"], image_asset_id=asset["asset_id"], asset_sha256=asset["sha256"], asset_approved=True, image_status="READY")
                    state["preview"] = None
                ui.notify("Shopify Files: " + str(result), type="positive" if result.get("status") == "READY" else "warning")

            def resolve_homepage_collection_plan():
                automation = _workflow_service()
                active = next((item for item in automation.interrupted(self.current_store)
                    if item.get("workflow_key") == "HOMEPAGE_COLLECTION_PREREQUISITE"), None)
                if active and active.get("status") == "WAITING_FOR_INPUT":
                    status = automation.confirm(active["run_id"], "ENSURE_COLLECTION_PLAN", user_input={"continue": True})
                    run_id = active["run_id"]
                elif active:
                    run_id = active["run_id"]
                    status = automation.run(run_id)
                else:
                    run = automation.create_run(self.current_store, "HOMEPAGE_COLLECTION_PREREQUISITE", collection_prerequisite_workflow())
                    run_id = run["run_id"]
                    status = automation.run(run_id)
                state["collection_prerequisite_run_id"] = run_id
                if status.get("status") in {"WAITING_FOR_INPUT", "FAILED"}:
                    return {"status": "WAITING_FOR_INPUT", "reason": (status.get("current_task") or {}).get("error_message")}
                result = automation.task_result(run_id, "ENSURE_COLLECTION_PLAN").get("result") or {}
                plan_id = result.get("plan_id")
                plan = CollectionPlanner().get_plan(plan_id) if plan_id else None
                return {"status": "READY" if plan else "WAITING_FOR_INPUT", "plan": plan,
                        "source": result.get("source"), "created_at": result.get("created_at")}

            def update_homepage_prerequisite_status(collection_ready=False):
                collection_status = "✓ Collection Plan" if collection_ready else "○ Collection Plan"
                later = "✓ Homepage Plan   ✓ Prompt Set" if state.get("plan") and state.get("prompt_set") else "○ Homepage Plan   ○ Prompt Set"
                prereq_status.set_text(f"✓ Brand Profile   {collection_status}   {later}")

            def design(discover_theme=True):
                try:
                    from ..brand_automation import brand_profile_from_store, get_brand_profile
                    brand = get_brand_profile(self.current_store) or brand_profile_from_store(self.current_store)
                    prereq_status.set_text("✓ Brand Profile   ⟳ Collection Plan 자동 준비 중   ○ Homepage Plan   ○ Prompt Set")
                    prereq_status.update()
                    prerequisite = resolve_homepage_collection_plan()
                    if prerequisite["status"] != "READY":
                        store_row = next((item for item in self.stores if item["store_id"] == self.current_store), {"store_name": self.current_store})
                        prepared = prepare_homepage_prerequisites(self.current_store, store=store_row,
                            brand_profile=brand, maximum_categories=int(max_categories.value or 8))
                        state.update(plan=prepared["homepage_plan"], snapshot={"status": "NOT_CHECKED", "theme_files": {}, "template": {"sections": {}}},
                            collection_plan=None, brand=prepared["brand"], prompt_set=prepared["prompt_set"],
                            preview=None, assignment_preview=None)
                        update_homepage_prerequisite_status(False)
                        render()
                        ui.notify("배너/헤더 프롬프트는 준비했습니다. 컬렉션/카테고리 프롬프트는 컬렉션 설계 후 자동 추가됩니다.", type="warning", multi_line=True)
                        return
                    with connect() as con:
                        row = con.execute("SELECT plan_id FROM store_collection_plans WHERE store_id=? ORDER BY version DESC LIMIT 1", (self.current_store,)).fetchone()
                    if not row: raise ValueError("먼저 컬렉션 자동 설계를 완료하세요.")
                    collection_plan = prerequisite["plan"]
                    brand = get_brand_profile(self.current_store) or brand_profile_from_store(self.current_store)
                    with connect() as con:
                        has_map = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shopify_collection_mappings'").fetchone()
                        mapping_rows = [dict(item) for item in con.execute("SELECT collection_key,handle,shopify_collection_id,image_url FROM shopify_collection_mappings WHERE store_id=?", (self.current_store,))] if has_map else []
                        mappings = {item["collection_key"]: item["handle"] for item in mapping_rows}
                        assignment_mappings = {item["collection_key"]: {**item, "remote_id": item["shopify_collection_id"], "status": "VERIFIED"} for item in mapping_rows}
                    images = {key: {**asset, "approval_status": "APPROVED"} for key, asset in approved_collection_images(self.current_store).items()}
                    plan = build_storefront_homepage_plan(store_id=self.current_store, brand=brand, collection_plan=collection_plan,
                        collection_handles=mappings, collection_assets=images, maximum_categories=int(max_categories.value or 8))
                    saved_hero = latest_hero_asset(self.current_store, plan["plan_id"])
                    if saved_hero and saved_hero.get("approval_status") == "APPROVED" and saved_hero.get("shopify_url"):
                        plan["hero"].update(image_url=saved_hero["shopify_url"], image_asset_id=saved_hero["asset_id"],
                                            asset_sha256=saved_hero["sha256"], asset_approved=True, image_status="READY",
                                            theme_image_ref=suggested_theme_image_ref(saved_hero),
                                            theme_image_ref_confirmed=bool(theme_ref_confirmed.value))
                        hero_url.set_value(saved_hero["shopify_url"])
                        hero_approved.set_value(True)
                        theme_image_ref.set_value(plan["hero"]["theme_image_ref"])
                    saved_hero = latest_hero_asset(self.current_store, plan["plan_id"])
                    if saved_hero and saved_hero.get("approval_status") == "APPROVED" and saved_hero.get("shopify_url"):
                        plan["hero"].update(image_url=saved_hero["shopify_url"], image_asset_id=saved_hero["asset_id"], asset_approved=True, image_status="READY")
                        hero_url.set_value(saved_hero["shopify_url"])
                        hero_approved.set_value(True)
                    snapshot = ShopifyThemeReader().discover(self.current_store) if discover_theme else {"status": "NOT_CHECKED", "theme_files": {}, "template": {"sections": {}}}
                    plan["hero"]["image_url"] = (hero_url.value or "").strip() or None
                    plan["hero"]["asset_approved"] = bool(hero_approved.value)
                    plan["hero"]["theme_image_ref"] = (theme_image_ref.value or "").strip() or None
                    plan["hero"]["theme_image_ref_confirmed"] = bool(theme_ref_confirmed.value)
                    if plan["hero"]["image_url"]: plan["hero"]["image_asset_id"] = "SHOPIFY_FILES_URL"
                    state["plan"], state["snapshot"], state["collection_plan"] = plan, snapshot, collection_plan
                    state["brand"] = brand
                    state["prompt_set"] = PromptAssetService().build(
                        store=next((item for item in self.stores if item["store_id"] == self.current_store), {"store_name": self.current_store}),
                        brand=brand, collection_plan=collection_plan, homepage_plan=plan)
                    state["preview"] = compose_homepage_preview(plan, snapshot, collection_plan, collection_handles=mappings) if snapshot.get("status") == "CONNECTED" else None
                    discovered = discover_homepage_sections(snapshot.get("theme_files") or {})
                    category_schema = discovered.get("category") or {}
                    theme_assignment = {"sections": ([{"mode": category_schema.get("mode")}] if category_schema else []),
                                        "builder": snapshot.get("page_builder") or ""}
                    hero_asset = saved_hero if saved_hero and saved_hero.get("approval_status") == "APPROVED" else None
                    if hero_asset:
                        hero_asset = {**hero_asset, "asset_id": hero_asset.get("asset_id"), "path": hero_asset.get("local_path"),
                                      "url": hero_asset.get("shopify_url"), "provider": hero_asset.get("provider") or "MANUAL"}
                    state["assignment_preview"] = HomepageAssignmentService().build(
                        homepage={"hero": plan["hero"], "unrelated_sections": (snapshot.get("template") or {}).get("sections", {})},
                        collection_plan=collection_plan, mappings=assignment_mappings,
                        approved_images=images, theme=theme_assignment, hero_asset=hero_asset,
                        featured_products=state.get("featured_products_plan"))
                    HomepageAutomationService().export_report(plan, preview=state["preview"])
                    update_homepage_prerequisite_status(True)
                    summary.set_text(f"Hero: NEEDS IMAGE · Categories: {plan['category_summary']['ready_count']} READY / {plan['category_summary']['skipped_count']} SKIPPED · Theme: {snapshot.get('status', 'MANUAL ACTION')}")
                    render()
                    ui.notify("홈페이지 계획/preview를 저장했습니다. Shopify write는 실행하지 않았습니다.", type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            def auto_complete():
                """One beginner action: finish safe preview work, then wait at the write gate."""
                design()
                if not state.get("collection_plan"):
                    ui.notify("배너/헤더 프롬프트는 준비했습니다. 컬렉션/카테고리 프롬프트는 컬렉션 계획이 준비된 뒤 이어집니다.", type="warning")
                    return
                if not state.get("plan"):
                    return
                tasks = homepage_assignment_workflow(state["assignment_preview"],
                    preview_id=(state.get("preview") or {}).get("preview_id"),
                    assets_approved=bool(hero_approved.value))
                automation = _workflow_service()
                run = automation.create_run(self.current_store, "HOMEPAGE_AUTO_COMPLETE", tasks)
                status = automation.run(run["run_id"])
                ui.notify(f"안전한 단계 완료 · {status['progress_percent']}% · 실제 적용 승인 대기", type="positive")

            def assignment_ready_check():
                plan = state.get("plan")
                if not plan:
                    ui.notify("먼저 홈페이지 자동 완성을 실행하세요.", type="warning")
                    return
                hero = assignment_banner_check(plan)
                categories = assignment_category_check(plan)
                items = categories["items"]
                duplicate = len({x.get("target") for x in items if x.get("target")}) != len([x for x in items if x.get("target")])
                checks = [
                    ("Hero 표시", bool(plan.get("hero", {}).get("enabled", True))),
                    ("제목/설명", bool(hero.get("headline") and hero.get("body"))),
                    ("CTA와 실제 링크", bool(hero.get("cta") and hero.get("cta_link") not in {None, "", "#"})),
                    ("승인된 배너 이미지", bool(hero.get("image") and plan["hero"].get("asset_approved"))),
                    ("Category shortcut 4개 이상", len([x for x in items if x.get("target")]) >= 4),
                    ("각 shortcut 이미지", bool(items) and all(x.get("image_asset") for x in items)),
                    ("정확한 컬렉션 연결", not categories["warnings"]),
                    ("# / blank 링크 없음", all(x.get("target") not in {None, "", "#"} for x in items)),
                    ("잘못된 중복 target 없음", not duplicate),
                    ("Desktop / mobile 확인", False),
                ]
                with ui.dialog() as dialog, ui.card().classes("w-[680px] max-w-full"):
                    ui.label("과제 제출용 확인").classes("ss-card-title")
                    for label, passed in checks:
                        ui.label(("✓ " if passed else "확인 필요 · ") + label).classes("text-green-700" if passed else "text-amber-800")
                    ui.label("Desktop/mobile 화면은 실제 Theme 미리보기에서 사람이 마지막으로 확인해야 합니다.").classes("ss-help")
                    ui.button("닫기", on_click=dialog.close).props("outline")
                dialog.open()

            def render():
                plan = state.get("plan")
                if not plan: return
                hero = plan["hero"]
                hero["image_url"] = (hero_url.value or "").strip() or None
                hero["asset_approved"] = bool(hero_approved.value)
                hero["theme_image_ref"] = (theme_image_ref.value or "").strip() or None
                hero["theme_image_ref_confirmed"] = bool(theme_ref_confirmed.value)
                if hero["image_url"] and not hero.get("image_asset_id"): hero["image_asset_id"] = "SHOPIFY_FILES_URL"
                checks = assignment_banner_check(plan)
                cats = assignment_category_check(plan)
                preview_area.clear()
                with preview_area:
                    with ui.card().classes("w-full border border-slate-200"):
                        ui.label("Hero banner · assignment mode").classes("font-semibold")
                        ui.label(f"Headline: {checks['headline']}\nBody: {checks['body']}\nCTA: {checks['cta']} → {checks['cta_link']}\nImage: {checks['image'] or 'NEEDS IMAGE'}\nAlt: {checks['alt_text']}").classes("whitespace-pre-line")
                        ui.label("Image prompt: " + hero["image_prompt"]).classes("text-xs whitespace-pre-line")
                        if checks["warnings"]: ui.label("Warnings: " + ", ".join(checks["warnings"])).classes("text-sm text-amber-800")
                    with ui.card().classes("w-full border border-slate-200"):
                        ui.label("Category shortcuts · assignment mode").classes("font-semibold")
                        for item in cats["items"]:
                            ui.label(f"{item['position']}. {item['title']} → {item['target'] or 'SKIP_REMOTE'} · {item['image_source']} · alt: {item['alt_text']}")
                        for warning in cats["warnings"]: ui.label(str(warning)).classes("text-sm text-amber-800")
                    preview = state.get("preview")
                    if preview:
                        ui.label(f"Theme actions: {preview['status']} · hero {preview['discovery']['hero']} · categories {preview['discovery']['category']}").classes("font-medium")
                        for action in preview["actions"]: ui.label(str(action)).classes("text-sm")

            def refresh_preview():
                try:
                    if not state.get("plan") or not state.get("snapshot"): raise ValueError("홈페이지 자동 설계를 먼저 실행하세요.")
                    state["plan"]["hero"]["image_url"] = (hero_url.value or "").strip() or None
                    state["plan"]["hero"]["asset_approved"] = bool(hero_approved.value)
                    state["plan"]["hero"]["theme_image_ref"] = (theme_image_ref.value or "").strip() or None
                    state["plan"]["hero"]["theme_image_ref_confirmed"] = bool(theme_ref_confirmed.value)
                    if hero_url.value and not state["plan"]["hero"].get("image_asset_id"):
                        state["plan"]["hero"]["image_asset_id"] = "SHOPIFY_FILES_URL"
                    state["preview"] = compose_homepage_preview(state["plan"], state["snapshot"], state["collection_plan"])
                    HomepageAutomationService().export_report(state["plan"], preview=state["preview"])
                    render()
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            hero_url.on_value_change(lambda _: state.update(preview=None))
            hero_approved.on_value_change(lambda _: state.update(preview=None))
            theme_image_ref.on_value_change(lambda _: state.update(preview=None))
            theme_ref_confirmed.on_value_change(lambda _: state.update(preview=None))

            def inspect_image():
                from ..image_validation import inspect_image as inspect_asset
                result = inspect_asset((local_image.value or "").strip(), asset_type="HERO_BANNER")
                if result.get("status") == "DEPENDENCY_MISSING":
                    check_image_engine()
                else:
                    ui.notify(str(result.get("message_ko") or result.get("status")), type="positive" if result["valid"] and result["status"] == "FIT" else "warning")

            def generate_hero():
                try:
                    if not state.get("plan"): raise ValueError("홈페이지 자동 설계를 먼저 실행하세요.")
                    result = generate_hero_image(self.current_store, state["plan"], provider=OpenAIImagesProvider(), enabled=bool(paid_opt_in.value))
                    ui.notify("Generated asset ready for review: " + result["path"], type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            def continue_after_collection_design():
                design(discover_theme=False)
                if state.get("prompt_set"):
                    build_prompts()

            def build_prompts():
                try:
                    if not state.get("prompt_set"):
                        design(discover_theme=False)
                    if not state.get("prompt_set"):
                        prepared = prepare_homepage_prerequisites(self.current_store,
                            store=next((item for item in self.stores if item["store_id"] == self.current_store), {"store_name": self.current_store}),
                            maximum_categories=int(max_categories.value or 8))
                        state["plan"], state["collection_plan"], state["brand"], state["prompt_set"] = (
                            prepared["homepage_plan"], prepared["collection_plan"], prepared["brand"], prepared["prompt_set"])
                    prompt_set = state["prompt_set"]
                    prompt_area.clear()
                    with prompt_area:
                        if prompt_set.get("groups", {}).get("collection", {}).get("status") == "WAITING_FOR_COLLECTION_PLAN":
                            ui.label("배너/헤더 프롬프트는 준비했습니다.").classes("ss-card-title")
                            ui.label("컬렉션/카테고리 프롬프트는 컬렉션 설계 후 자동 추가됩니다.").classes("ss-help")
                            ui.button("현재 프롬프트 보기", on_click=lambda: ui.notify("아래 Hero·Header 프롬프트를 확인하세요.", type="info")).props("outline")
                            ui.button("컬렉션 자동 설계 후 이어서", on_click=continue_after_collection_design, icon="auto_awesome").props("color=primary")
                        ui.label(f"복사해 외부 이미지 도구에서 사용할 수 있는 prompt {len(prompt_set['assets'])}개").classes("ss-help")
                        for item in prompt_set["assets"]:
                            with ui.expansion(f"{item['title']} · {item['suggested_size']}", icon="image"):
                                ui.textarea("복사할 prompt", value=item["prompt_main"]).props("readonly autogrow").classes("w-full")
                                ui.label("Negative prompt: " + item["negative_prompt"]).classes("ss-help")
                                ui.label("Alt 제안: " + item["alt_text_suggestion"]).classes("ss-help")
                                async def copy_prompt(text=item["prompt_main"]):
                                    await ui.run_javascript("navigator.clipboard.writeText(" + json.dumps(text, ensure_ascii=False) + ")")
                                    ui.notify("Prompt를 클립보드에 복사했습니다.", type="positive")
                                ui.button("복사", on_click=copy_prompt, icon="content_copy").props("outline")
                                if item["asset_type"] in {"HERO_BANNER", "COLLECTION_IMAGE", "CATEGORY_SHORTCUT"}:
                                    ui.button("로컬 생성기로 후보 만들기", on_click=lambda asset=item: generate_with_local_studio(asset), icon="image_search").props("outline")
                        ui.button("전체 프롬프트 파일 내보내기", on_click=export_prompts, icon="download").props("outline")
                        ui.button("이미지 엔진 연결 확인", on_click=check_image_engine, icon="hub").props("outline")
                    ui.notify("프롬프트를 준비했습니다. 유료 API는 호출하지 않았습니다.", type="positive")
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")

            def export_prompts():
                if not state.get("prompt_set"):
                    ui.notify("먼저 프롬프트 자동 생성을 누르세요.", type="warning"); return
                result = PromptAssetService().export(state["prompt_set"], store_id=self.current_store)
                ui.notify("복붙 묶음 저장: " + result["files"]["copy_paste_bundle.txt"], type="positive")

            def check_image_engine():
                provider = LocalImageStudioProvider()
                health = provider.health()
                capabilities = provider.capabilities() if health.get("available") else {}
                doctor = dependency_doctor()
                with ui.dialog() as dialog, ui.card().classes("w-[680px] max-w-full"):
                    ui.label("이미지 엔진 연결 확인").classes("ss-card-title")
                    ui.label(("✓" if doctor["checks"]["PIL"] else "✕") + " ShopSource Pillow · " + ("준비됨" if doctor["checks"]["PIL"] else PIL_MISSING_KO))
                    ui.label(("✓" if health.get("available") else "✕") + " YouTubeSum 브리지 · " + str(health.get("message") or health.get("status")))
                    ui.label("생성기 상태 · " + str(capabilities.get("generator", {}).get("status", "PROMPT_ONLY_FALLBACK")))
                    ui.label("로컬 생성기가 없으면 prompt 복사와 파일 업로드 흐름을 계속 사용할 수 있습니다.").classes("ss-help")
                    with ui.row():
                        ui.button("닫기", on_click=dialog.close).props("outline")
                        if not doctor["checks"]["PIL"]:
                            async def repair_environment():
                                dialog.close()
                                result = await asyncio.to_thread(repair_runtime_dependencies)
                                ui.notify(result["message"], type="positive" if result["status"] == "RESTART_REQUIRED" else "negative")
                                if result.get("detail"):
                                    with ui.dialog() as detail_dialog, ui.card():
                                        ui.label("고급 설치 정보")
                                        ui.label(result["detail"])
                                        ui.button("닫기", on_click=detail_dialog.close)
                                    detail_dialog.open()
                            ui.button("환경 자동 복구", on_click=repair_environment, icon="build").props("color=primary")
                dialog.open()

            async def generate_with_local_studio(asset):
                provider = LocalImageStudioProvider()
                health = await asyncio.to_thread(provider.health)
                caps = await asyncio.to_thread(provider.capabilities) if health.get("available") else {}
                if not health.get("available") or (caps.get("generator") or {}).get("status") != "READY":
                    ui.notify("로컬 이미지 생성기가 준비되지 않았습니다. prompt를 복사해 외부 도구에서 만든 뒤 파일을 업로드하세요.", type="warning", multi_line=True)
                    return
                store_row = next((x for x in self.stores if x["store_id"] == self.current_store), {"store_name": self.current_store})
                job = provider.create_job(store_id=self.current_store, store_name=store_row.get("store_name", self.current_store),
                    asset=asset, context=state.get("brand", {}), output_count=3)
                from ..local_image_studio import image_generation_workflow
                if asset["asset_type"] == "HERO_BANNER" and state.get("plan"):
                    asset = {**asset, "plan_id": state["plan"]["plan_id"]}
                queue = _workflow_service()
                run = queue.create_run(self.current_store, "LOCAL_IMAGE_ASSET", image_generation_workflow(job, asset))
                run = await asyncio.to_thread(queue.run, run["run_id"])
                if run.get("status") != "WAITING_FOR_CONFIRMATION":
                    ui.notify("후보 생성에 실패했습니다. prompt 복사와 수동 업로드를 이용하세요.", type="warning", multi_line=True)
                    return
                state["local_image_run_id"] = run["run_id"]
                result = queue.task_result(run["run_id"], "LOCAL_IMAGE_GENERATE").get("result") or {}
                result["status"] = "SUCCEEDED"
                if result.get("status") not in {"SUCCEEDED", "PARTIAL"}:
                    error = result.get("error") or {}
                    ui.notify(error.get("message_ko") or result.get("message") or "생성을 마치지 못했습니다. prompt-only 방식으로 계속하세요.", type="warning", multi_line=True)
                    return
                state["local_candidates"] = result.get("candidates", [])
                candidate_area.clear()
                with candidate_area:
                    ui.label("이미지 후보 · 미리보기 후 직접 가져와 승인할 수 있습니다.").classes("ss-card-title")
                    for candidate in state["local_candidates"]:
                        ui.label(f"{candidate.get('candidate_id')} · {candidate.get('width')}×{candidate.get('height')} · 기술 점수 {candidate.get('technical_score')}").classes("ss-help")
                        ui.button("후보 파일 가져오기", on_click=lambda c=candidate, a=asset: import_local_candidate(c, a), icon="download").props("outline")
                ui.notify("후보 생성 완료 · 승인 전에는 Shopify 적용에 사용할 수 없습니다.", type="positive")

            async def confirm_local_candidate(run_id, candidate_id):
                try:
                    result = await asyncio.to_thread(_workflow_service().confirm, run_id, "LOCAL_IMAGE_APPROVAL",
                        user_input={"candidate_id": candidate_id})
                    if result.get("status") not in {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"}:
                        raise ValueError("승인 queue가 완료되지 않았습니다.")
                    ui.notify("선택 이미지가 검사·승인되어 ShopSource에 저장됐습니다. Shopify 적용은 하지 않았습니다.", type="positive")
                    candidate_area.clear()
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")

            def import_local_candidate(candidate, asset):
                if state.get("local_image_run_id"):
                    try:
                        result = _workflow_service().confirm(state["local_image_run_id"], "LOCAL_IMAGE_APPROVAL",
                            user_input={"candidate_id": candidate.get("candidate_id")})
                        if result.get("status") not in {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"}:
                            raise ValueError("승인 queue가 완료되지 않았습니다.")
                        ui.notify("선택 이미지가 검사·승인되어 ShopSource에 저장됐습니다. Shopify 적용은 하지 않았습니다.", type="positive")
                        candidate_area.clear()
                    except Exception as exc:
                        ui.notify(_safe_error(exc), type="negative")
                    return
                try:
                    from ..paths import EXPORT_DIR
                    provider = LocalImageStudioProvider()
                    if asset["asset_type"] == "HERO_BANNER":
                        if not state.get("plan"): raise ValueError("홈페이지 설계를 먼저 완료하세요.")
                        destination = EXPORT_DIR / "homepage_assets" / self.current_store / state["plan"]["plan_id"] / "hero" / "local_studio"
                    else:
                        destination = EXPORT_DIR / "collection_images" / self.current_store / str(asset.get("collection_key") or "misc") / "local_studio"
                    imported = provider.import_candidate(candidate, destination)
                    from ..image_validation import inspect_image
                    inspection = inspect_image(imported["path"], asset_type=asset["asset_type"])
                    if not inspection["valid"]: raise ValueError(inspection.get("message_ko") or "이미지 검사를 통과하지 못했습니다.")
                    if asset["asset_type"] == "HERO_BANNER":
                        register_manual_hero_asset(self.current_store, state["plan"]["plan_id"],
                            Path(imported["path"]).name, Path(imported["path"]).read_bytes(), provider="LOCAL_IMAGE_STUDIO")
                    else:
                        from ..collection_images import register_image_asset
                        register_image_asset(self.current_store, asset["collection_key"], imported["path"],
                            provider="LOCAL_IMAGE_STUDIO", model="headless-bridge", alt_text=asset.get("alt_text_suggestion", ""),
                            metadata={"sha256": imported["sha256"], "validation": inspection, "approval_status": "NEEDS_REVIEW"})
                    ui.notify("이미지 검사 완료 · 검수 대기 상태로 저장했습니다.", type="positive")
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")

            def show_manual():
                ui.notify("Online Store → Themes → Customize → Hero/Image Banner: image, heading, text, button, link. Add Collection list (or equivalent), select only mapped collections, review cards and order, then Save.", type="info", multi_line=True)

            def apply_confirm():
                preview = state.get("preview")
                if not preview: ui.notify("적용 전 최신 미리보기가 필요합니다.", type="warning"); return
                with ui.dialog() as dialog, ui.card():
                    ui.label("현재 미리보기의 최소 homepage JSON 변경을 Shopify theme에 적용합니다. 계속할까요?")
                    with ui.row():
                        ui.button("취소", on_click=dialog.close).props("outline")
                        def perform():
                            dialog.close()
                            try:
                                result = HomepageAutomationService().apply(preview["preview_id"], confirmed=True,
                                    approved_assets=bool(hero_approved.value))
                                state["backup_id"] = result.get("backup_id")
                                ui.notify("Homepage apply: " + str(result), type="positive" if result.get("status") == "VERIFIED" else "warning")
                            except Exception as exc: ui.notify(_safe_error(exc), type="negative")
                        ui.button("확인 후 적용", on_click=perform).props("color=primary")
                dialog.open()

            def rollback_homepage():
                backup_id = state.get("backup_id")
                if not backup_id:
                    ui.notify("이 화면에서 확인된 적용 백업이 없습니다.", type="warning")
                    return
                with ui.dialog() as dialog, ui.card():
                    ui.label("이 적용 전 homepage JSON을 Shopify에 복원합니다. 원격 drift가 있으면 복원을 거부합니다.")
                    with ui.row():
                        ui.button("취소", on_click=dialog.close).props("outline")
                        def perform_rollback():
                            dialog.close()
                            try:
                                result = HomepageAutomationService().rollback(backup_id, confirmed=True)
                                ui.notify("Homepage rollback: " + str(result), type="positive" if result.get("status") == "VERIFIED" else "warning")
                            except Exception as exc: ui.notify(_safe_error(exc), type="negative")
                        ui.button("확인 후 롤백", on_click=perform_rollback).props("color=negative")
                dialog.open()

            with ui.card().classes("w-full border-2 border-indigo-200 bg-indigo-50"):
                ui.label("초보자 자동화").classes("ss-card-title")
                ui.label("안전한 설계와 검사는 연속 실행하고, 실제 Theme write는 한 번만 승인받습니다.").classes("ss-help")
                ui.button("홈페이지 자동 완성", on_click=auto_complete, icon="auto_awesome").props("color=primary size=lg")
            with ui.card().classes("w-full border border-violet-200"):
                ui.label("이미지 준비 · prompt 복사 → 외부 생성 → 파일 업로드 → 검사 → 승인").classes("ss-card-title")
                ui.button("프롬프트 자동 생성", on_click=build_prompts, icon="prompt_suggestion").props("color=primary")
                prompt_area = ui.column().classes("w-full gap-2")
                candidate_area = ui.column().classes("w-full gap-2")
            with ui.row():
                ui.button("홈페이지 자동 설계", on_click=design, icon="auto_awesome").props("color=positive")
                ui.button("메인 배너 자동 만들기", on_click=build_prompts, icon="image")
                ui.button("카테고리 바로가기 자동 만들기", on_click=build_prompts, icon="category")
                ui.button("홈페이지 미리보기", on_click=refresh_preview, icon="visibility").props("outline")
                ui.button("과제 제출용 확인", on_click=assignment_ready_check, icon="checklist").props("outline")
                ui.button("Shopify 적용", on_click=apply_confirm, icon="publish").props("color=primary")
                ui.button("롤백", on_click=rollback_homepage, icon="undo").props("outline")
                ui.button("수동 적용 안내", on_click=show_manual, icon="help").props("outline")
            ui.button("Check banner image", on_click=inspect_image, icon="fact_check").props("outline")
            ui.button("Generate banner image (paid opt-in)", on_click=generate_hero, icon="auto_awesome").props("outline")
            ui.button("Approve selected banner", on_click=approve_latest_hero, icon="verified").props("outline")
            ui.button("Upload approved banner to Shopify Files", on_click=upload_latest_hero, icon="cloud_upload").props("outline")
            ui.label("Paid image generation is not triggered by planning. Set opt-in in advanced settings and use the separate asset-generation workflow after review.").classes("text-xs text-slate-600")

        if state.get("plan"): render()

    def _collections(self):
        ui = self.ui
        self._heading("컬렉션 자동화", "Store Profile, 소싱 카테고리와 MASTER 상품을 분석해 Shopify 컬렉션 초안을 로컬에서 설계합니다.")
        ui.label("이 화면은 계획/미리보기만 수행합니다. Shopify, Spark, Amazon에는 쓰거나 연결하지 않습니다.").classes("text-sm text-amber-800")
        with ui.card().classes("w-full border-2 border-violet-200 bg-violet-50"):
            ui.label(f"{self.current_store} | {next((row['store_name'] for row in self.stores if row['store_id'] == self.current_store), '')}").classes("text-xl font-bold")
            with ui.expansion("고급 설정", icon="tune"):
                desired_count = ui.number("컬렉션 개수 (비우면 자동)", value=None, min=1, max=15).classes("w-64")
                rule_strategy = ui.select({"TITLE_FALLBACK":"상품 제목 조건", "TAG_PREFERRED":"StoreSource 태그 우선", "MIXED":"태그 + 제목"}, value="TITLE_FALLBACK", label="조건 전략").classes("w-72")
                min_products = ui.number("최소 예상 상품 수", value=3, min=0, max=100000).classes("w-64")
                overlap_limit = ui.number("중복 경고 기준", value=80, min=0, max=100, suffix="%").classes("w-64")
                include_empty = ui.checkbox("상품이 없어도 미래용 컬렉션 포함", value=False)
                language = ui.select({"en":"English"}, value="en", label="설명 언어").classes("w-48")
            summary = ui.label("아직 컬렉션 계획이 없습니다.").classes("text-sm font-medium")
            plan_area = ui.column().classes("w-full gap-3")
            plan_ref = {"plan": None}

            def render_plan(plan):
                plan_ref["plan"] = plan
                summary.set_text(
                    f"컬렉션 {plan['collection_count']}개 · 예상 포함 상품 {plan['included_product_count']:,}개 · "
                    f"미분류 {plan['unmatched_product_count']:,}개 ({plan['unmatched_percentage']:.1f}%) · 중복 포함 허용"
                )
                plan_area.clear()
                with plan_area:
                    diff = plan.get("diff") or {}
                    ui.label("버전 차이: " + " · ".join(f"{label} {len(diff.get(key, []))}" for key, label in
                             (("new", "신규"), ("changed", "변경"), ("unchanged", "유지"), ("removed_or_disabled", "제외/비활성")))).classes("text-xs text-slate-600")
                    if plan.get("warnings"):
                        with ui.expansion(f"계획 경고 {len(plan['warnings'])}건", icon="warning").classes("w-full"):
                            for warning in plan["warnings"]:
                                ui.label(str(warning)).classes("text-sm text-amber-800")
                    for collection in plan["collections"]:
                        with ui.card().classes("w-full border border-slate-200"):
                            ui.label(f"{collection['priority']}. {collection['title']} · 예상 {collection['estimated_product_count']:,}개 · {collection['shopify_sync_status']}").classes("font-semibold")
                            ui.label(f"Rule: " + " OR ".join(f"{row['field']} {row['relation']} ‘{row['value']}’" for row in collection["conditions"])).classes("text-sm")
                            ui.label(f"Title-rule specificity estimate: {collection.get('title_rule_specificity_estimate', 0):.0%} (multi-word phrase heuristic)").classes("text-xs text-slate-600")
                            ui.label("Store status: " + ", ".join(f"{key} {value}" for key, value in collection.get("store_status_breakdown", {}).items())).classes("text-xs text-slate-600")
                            ui.label("이미지 프롬프트: 생성됨 · 이미지 API 호출 안 함").classes("text-xs text-emerald-700")
                            if collection["warnings"]:
                                ui.label("경고: " + ", ".join(collection["warnings"])).classes("text-sm text-amber-800")
                            with ui.expansion("설명 · 이미지 프롬프트 · 대표 상품 5개", icon="visibility"):
                                ui.label(collection["description_html"]).classes("text-sm")
                                ui.label(collection["image_prompt"]).classes("text-xs whitespace-pre-line")
                                ui.label("Alt text: " + collection["image_alt_text"]).classes("text-xs")
                                for sample in collection["sample_products"]:
                                    ui.label(f"{sample['asin']} · {sample['title']} · {sample.get('brand') or '브랜드 미상'} · {sample.get('final_status') or '미분류'}").classes("text-xs")
                    overlap = [warning for row in plan["collections"] for warning in row["warnings"] if warning.startswith("EXTREME_OVERLAP")]
                    if overlap:
                        ui.label(f"컬렉션 간 중복 경고 {len(overlap)}건 (중복 포함 자체는 허용)").classes("text-sm text-amber-800")

            def design():
                try:
                    options = {"desired_collection_count": None if desired_count.value in (None, "") else int(desired_count.value),
                               "rule_strategy": rule_strategy.value or "TITLE_FALLBACK",
                               "min_products": int(min_products.value or 0),
                               "max_overlap_warning": float(overlap_limit.value or 80) / 100,
                               "include_empty": bool(include_empty.value), "language": language.value or "en"}
                    result = CollectionPlanner().create_plan(self.current_store, settings=options)
                    preview_ref["value"] = None
                    render_plan(result)
                    render_image_actions(result)
                    ui.notify(f"컬렉션 계획 v{result['version']}을 저장했습니다. Shopify에는 쓰지 않았습니다.", type="positive")
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")

            def export_plan():
                try:
                    plan = plan_ref.get("plan")
                    if not plan: raise ValueError("먼저 컬렉션 자동 설계를 실행하세요.")
                    result = CollectionPlanner().export(plan["plan_id"])
                    ui.notify("JSON/Markdown 계획 내보내기 완료: " + result["folder"], type="positive")
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")

            def preview_saved_plan():
                try:
                    plan = plan_ref.get("plan")
                    if not plan: raise ValueError("미리보기할 계획이 없습니다. 먼저 자동 설계를 실행하세요.")
                    saved = CollectionPlanner().get_plan(plan["plan_id"])
                    preview_ref["value"] = None
                    render_plan(saved)
                    render_image_actions(saved)
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            with ui.row():
                ui.button("컬렉션 자동 설계", on_click=design, icon="auto_awesome").props("color=positive")
                ui.button("다시 설계", on_click=design, icon="refresh").props("outline")
                ui.button("미리보기", on_click=preview_saved_plan, icon="visibility").props("outline")
                ui.button("JSON/MD 내보내기", on_click=export_plan, icon="download").props("outline")
                ui.button("Shopify에 컬렉션 생성 (Phase 3.3)", on_click=None).props("disable outline")
                ui.button("이미지 자동 생성 (다음 단계)", on_click=None).props("disable outline")

        ui.separator()
        with ui.card().classes("w-full border border-emerald-200"):
            ui.label("Shopify 연결 / 컬렉션 게시").classes("text-lg font-semibold")
            config = get_shopify_connection(self.current_store)
            connection_label = ui.label(
                f"Shopify: {config['shop_domain']} · {config['status']} · API {config['api_version']}" if config else "Shopify: NOT CONFIGURED"
            ).classes("font-medium")
            with ui.expansion("Shopify 연결 설정 (토큰은 Windows 자격 증명에만 저장)", icon="lock"):
                shop_domain = ui.input("Shopify shop domain (*.myshopify.com)", value=config.get("shop_domain", "") if config else "").classes("w-96")
                shop_auth_mode=ui.select({DEV_DASHBOARD_CLIENT_CREDENTIALS:"Dev Dashboard 앱 (권장)",
                                          LEGACY_ADMIN_TOKEN:"기존 Legacy 토큰"},
                    value=(config or {}).get("auth_mode") or DEV_DASHBOARD_CLIENT_CREDENTIALS,
                    label="Shopify 연결 방식").classes("w-96")
                shop_client_id=ui.input("Dev Dashboard Client ID",value="").classes("w-96")
                shop_client_secret=ui.input("Dev Dashboard Client Secret",value="").props("type=password autocomplete=new-password").classes("w-96")
                app_profiles=list_app_profiles()
                shop_app_profile=ui.select({p["profile_id"]:f"{p['display_name']} · {p['expected_app_gid']}" for p in app_profiles},
                    value=(config or {}).get("app_profile_id"),label="기존 Production App 프로필 재사용").classes("w-96")
                ui.label("Client ID/Secret은 Shopify의 app.apiKey·App GID·Shop GID 확인 후에만 공유 App Profile로 저장합니다. 기존 per-store credentials는 변경하지 않습니다.").classes("ss-help")
                shop_token = ui.input("Admin API access token (저장 후 화면에서 지워짐)").props("type=password autocomplete=new-password").classes("w-96")
                def update_shopify_auth_fields():
                    dev=shop_auth_mode.value==DEV_DASHBOARD_CLIENT_CREDENTIALS
                    shop_client_id.set_visibility(dev); shop_client_secret.set_visibility(dev); shop_token.set_visibility(not dev)
                shop_auth_mode.on_value_change(lambda _:update_shopify_auth_fields()); update_shopify_auth_fields()
                ui.label("Scopes: read_products, write_products, read_publications; homepage theme read: read_themes. Shopify theme write needs write_themes plus Shopify exemption; this phase never performs theme writes.").classes("text-xs text-slate-600")
                def save_shopify_config():
                    try:
                        mode=shop_auth_mode.value
                        if mode==DEV_DASHBOARD_CLIENT_CREDENTIALS and bool(shop_client_id.value)!=bool(shop_client_secret.value):
                            raise ValueError("Client ID와 Client Secret을 모두 입력하세요.")
                        if mode==DEV_DASHBOARD_CLIENT_CREDENTIALS and shop_client_id.value and shop_client_secret.value:
                            bound=verify_and_bind_app_profile(self.current_store,shop_domain.value or "",shop_client_id.value,
                                shop_client_secret.value)
                            shop_client_id.value=""; shop_client_secret.value=""
                            ui.notify(f"Shopify app identity verified: {bound['app_title']} · {bound['app_id']}",type="positive")
                        elif mode==DEV_DASHBOARD_CLIENT_CREDENTIALS and shop_app_profile.value:
                            bound=bind_existing_app_profile(self.current_store,shop_domain.value or "",str(shop_app_profile.value))
                            ui.notify(f"Production App을 이 스토어에 연결했습니다: {bound['app_title']} · {bound['app_id']}",type="positive")
                        elif mode==DEV_DASHBOARD_CLIENT_CREDENTIALS:
                            raise ValueError("Client ID/Secret을 입력해 앱 신원을 확인하거나 기존 Production App 프로필을 선택하세요.")
                        else:
                            save_shopify_connection(self.current_store, shop_domain.value or "",auth_mode=mode)
                        if mode==LEGACY_ADMIN_TOKEN and shop_token.value:
                            save_shopify_token(self.current_store, shop_token.value)
                            shop_token.value = ""
                        connection_label.set_text(f"Shopify: {shop_domain.value} · CONFIGURED · API 2026-07")
                        refresh_auth_health()
                        ui.notify("앱과 스토어 identity 확인 후 연결했습니다. token/secret은 화면이나 SQLite에 저장되지 않습니다.", type="positive")
                    except Exception as exc: ui.notify(_safe_error(exc), type="negative")
                ui.button("연결 설정/토큰 저장", on_click=save_shopify_config, icon="save")
            connection_detail = ui.label("권한 및 publication 미확인").classes("text-xs text-slate-600")
            auth_health=ui.label("").classes("ss-help")
            def remove_auth_credentials():
                from ..shopify_collections import delete_shopify_token
                if shop_auth_mode.value==DEV_DASHBOARD_CLIENT_CREDENTIALS: delete_dev_credentials(self.current_store)
                else: delete_shopify_token(self.current_store)
                refresh_auth_health(); ui.notify("선택한 인증정보를 Windows Credential Manager에서 삭제했습니다.",type="warning")
            if not (config or {}).get("app_profile_id"):
                ui.button("저장된 인증정보 삭제",on_click=remove_auth_credentials).props("outline color=negative")
            def refresh_auth_health():
                status=ShopifyAuthService().status(self.current_store)
                target=status.get("expected_app_title") or "NOT BOUND"
                current=status.get("authenticated_app_title") or "identity 확인 전"
                fp=(status.get("client_id_fingerprint") or "")[:12]
                auth_health.set_text(f"Production App: {target} · Authenticated App: {current} · App GID: {status.get('authenticated_app_gid') or '—'} · Client ID fingerprint: {fp or '—'} · Shop GID: {status.get('shopify_shop_gid') or '—'} · Installation GID: {status.get('shopify_installation_gid') or '—'} · Auth: {status['auth_mode']} · Credential {'YES' if status['credential_present'] else 'NO'} · token {status['token_state']} · 만료 약 {status.get('expires_in_minutes') if status.get('expires_in_minutes') is not None else '확인 전'}분 · 최근 확인 {status.get('last_verified_at') or '없음'}")
            refresh_auth_health()
            def verify_shopify():
                try:
                    from ..shopify_collections import ShopifyReadOnlyVerificationService
                    result = ShopifyReadOnlyVerificationService().verify(self.current_store)
                    connection_label.set_text(f"Shopify G0: {result['status']}")
                    granted = result.get("granted_scopes", [])
                    pubs = ", ".join(f"{p.get('name')} [{p.get('id')}]" for p in result.get("online_store_publications", []))
                    connection_detail.set_text(
                        f"Auth: {result.get('auth_mode') or '미설정'} · 자격 증명: {'YES' if result.get('credential_present') else 'NO'} · "
                        f"Target Production App: {result.get('expected_app_title') or 'NOT BOUND'} [{result.get('expected_app_gid') or '—'}] · "
                        f"Authenticated App: {result.get('authenticated_app_title') or '미확인'} [{result.get('authenticated_app_gid') or result.get('authenticated_app_id') or '—'}] · "
                        f"App binding: {result.get('app_binding_status') or result.get('status')} · "
                        f"스토어 ID: {result.get('shop_id') or '—'} · "
                        f"도메인 확인: {'PASS' if result.get('shop_domain_verified') else 'FAIL'} "
                        f"(설정 {result.get('shop_domain') or '—'} / primary {result.get('primary_domain_host') or '—'} / "
                        f"myshopify {result.get('actual_shop_domain') or '—'}) · "
                        f"Declared required: {', '.join(result.get('declared_required_scopes',[])) or 'none'} · "
                        f"Declared optional: {', '.join(result.get('declared_optional_scopes',[])) or 'none'} · "
                        f"Authenticated app requested: {', '.join(result.get('authenticated_requested_scopes',[])) or 'none'} · "
                        f"Authenticated app optional: {', '.join(result.get('authenticated_optional_scopes',[])) or 'none'} · "
                        f"Granted scopes: {', '.join(granted) or 'none'} · "
                        f"Missing for G0: {', '.join(result.get('missing_for_current_gate',[])) or 'none'} · "
                        f"Missing read scopes: {', '.join(result.get('missing_read_scopes', [])) or 'none'} · "
                        f"Future/write scopes missing: {', '.join(result.get('missing_future_write_scopes', [])) or 'none'} · "
                        f"Restricted/approval: {', '.join(result.get('restricted_or_approval_required_scopes',[])) or 'none'} · "
                        f"MAIN theme read: {result.get('theme_status', 'NOT_CHECKED')} · "
                        f"Publications read: {result.get('publications_status', 'NOT_CHECKED')} "
                        f"({pubs or 'not read / none'}) · Last verified: {result.get('last_verified_at') or '없음'}")
                    refresh_auth_health()
                    ui.notify("Shopify read-only 확인을 완료했습니다.",
                              type="positive" if result.get("status") == "VERIFIED" else "warning")
                except Exception as exc:
                    connection_detail.set_text(f"확인 실패: {_safe_error(exc)}")
                    ui.notify(_safe_error(exc), type="negative")
            ui.button("Shopify 연결 확인 / 권한 확인", on_click=verify_shopify, icon="verified_user").props("outline")
            generate_opt_in = ui.checkbox("이미지 자동 생성 사용 (체크해야 유료 API 호출 가능)", value=False)
            publish_opt_in = ui.checkbox("온라인 스토어에 공개", value=False)
            image_area = ui.column().classes("w-full")
            image_status = ui.label("이미지 준비 상태: 계획 생성 후 확인").classes("text-sm")
            preview_label = ui.label("Shopify plan 상태: 미리보기 전").classes("text-sm")
            preview_ref = {"value": None}
            def render_image_actions(plan):
                image_area.clear()
                ready = 0
                publisher = ShopifyCollectionPublisher()
                with image_area:
                    for definition in plan.get("collections", []):
                        key = definition["collection_key"]
                        with ui.row().classes("w-full items-center"):
                            ui.label(definition["title"]).classes("w-48 font-medium")
                            path_input = ui.input("Existing image path").classes("w-96")
                            def attach(k=key, d=definition, path_control=path_input):
                                try:
                                    ManualImageProvider().register(self.current_store, k, path_control.value or "", alt_text=d.get("image_alt_text", ""))
                                    ui.notify(f"{d['title']}: image ready", type="positive")
                                    render_image_actions(plan)
                                except Exception as exc: ui.notify(_safe_error(exc), type="negative")
                            def generate(d=definition):
                                try:
                                    if not generate_opt_in.value:
                                        raise RuntimeError("먼저 '이미지 자동 생성 사용'을 체크해야 유료 호출이 허용됩니다.")
                                    output = generate_collection_image(self.current_store, d, provider=OpenAIImagesProvider(), enabled=True)
                                    ui.notify(f"Generated image: {output['path']}", type="positive")
                                    render_image_actions(plan)
                                except Exception as exc: ui.notify(_safe_error(exc), type="negative")
                            ui.button("직접 파일 연결", on_click=attach, icon="image").props("outline dense")
                            ui.button("이미지 생성", on_click=generate, icon="auto_awesome").props("outline dense")
                            asset = publisher._image_asset(self.current_store, key)
                            if asset:
                                ready += 1
                                def approve_collection(k=key, title=definition["title"]):
                                    if approve_collection_image(self.current_store, k):
                                        ui.notify(f"{title}: image approved for homepage reuse", type="positive")
                                    else: ui.notify(f"{title}: image asset missing", type="warning")
                                ui.button("Approve for homepage", on_click=approve_collection, icon="verified").props("outline dense")
                                try:
                                    relative = Path(asset["path"]).resolve().relative_to((EXPORT_DIR / "collection_images").resolve())
                                    ui.image("/collection_images/" + relative.as_posix()).classes("w-24 h-24 object-cover rounded border")
                                except ValueError:
                                    pass
                                ui.label(f"READY · {asset['path']}").classes("text-xs text-emerald-700")
                            else:
                                ui.label("MISSING").classes("text-xs text-amber-700")
                image_status.set_text(f"이미지 준비: {ready}/{len(plan.get('collections', []))} ready · 모두 생성 시 예상 호출 {len(plan.get('collections', []))}회")
            def preview_shopify():
                try:
                    plan = plan_ref.get("plan")
                    if not plan: raise ValueError("먼저 컬렉션 자동 설계를 실행하세요.")
                    result = ShopifyCollectionPublisher().dry_run(plan, publish_online_store=bool(publish_opt_in.value))
                    preview_ref["value"] = result
                    preview_label.set_text("미리보기: " + " · ".join(f"{k} {v}" for k, v in result["counts"].items()))
                    with ui.dialog() as dialog, ui.card().classes("w-[900px] max-w-[95vw]"):
                        ui.label("Shopify 변경 미리보기 · 현재 변경 없음").classes("text-lg font-bold")
                        for item in result["items"]:
                            ui.label(f"{item['action']} · {item['title']} · 예상 상품 {item['estimated_product_count']} · image {item['image_status']}" + (f" · {item['reason']}" if item.get("reason") else ""))
                        ui.button("닫기", on_click=dialog.close).props("flat")
                    dialog.open()
                except Exception as exc:
                    preview_label.set_text(f"미리보기 실패: {_safe_error(exc)}")
                    ui.notify(_safe_error(exc), type="negative")
            def confirm_shopify_sync():
                plan = plan_ref.get("plan")
                if not plan: ui.notify("먼저 컬렉션 계획을 생성하세요.", type="warning"); return
                if not preview_ref["value"]: ui.notify("먼저 동기화 미리보기를 실행하세요.", type="warning"); return
                current_connection = get_shopify_connection(self.current_store)
                if (preview_ref["value"].get("plan_id") != plan.get("plan_id")
                        or preview_ref["value"].get("store_id") != self.current_store
                        or preview_ref["value"].get("shop_domain") != (current_connection or {}).get("shop_domain")
                        or preview_ref["value"].get("publish_online_store") != bool(publish_opt_in.value)):
                    ui.notify("계획/연결/공개 옵션이 미리보기 이후 바뀌었습니다. dry-run을 다시 실행하세요.", type="warning"); return
                with ui.dialog() as dialog, ui.card():
                    ui.label("명시적 확인: Shopify 컬렉션 쓰기를 실행합니다.").classes("font-bold text-amber-800")
                    ui.label("CREATE/UPDATE만 수행합니다. Shopify에서 컬렉션을 삭제하지 않습니다.")
                    def do_sync():
                        try:
                            result = ShopifyCollectionPublisher().sync(plan, confirmed=True, publish_online_store=bool(publish_opt_in.value), expected_preview=preview_ref["value"])
                            dialog.close()
                            preview_label.set_text("결과: " + " · ".join(f"{k} {v}" for k, v in result["summary"].items()))
                            ui.notify(f"Shopify sync complete · {result['run_id']}", type="positive")
                        except Exception as exc: ui.notify(_safe_error(exc), type="negative")
                    with ui.row():
                        ui.button("취소", on_click=dialog.close).props("flat")
                        ui.button("확인 — Shopify 쓰기", on_click=do_sync, icon="cloud_upload").props("color=negative")
                dialog.open()
            with ui.row():
                ui.button("동기화 미리보기", on_click=preview_shopify, icon="preview").props("outline")
                def generate_all_images():
                    plan = plan_ref.get("plan")
                    if not plan: ui.notify("먼저 컬렉션 계획을 생성하세요.", type="warning"); return
                    if not generate_opt_in.value: ui.notify("유료 API 사용 opt-in을 켜야 합니다.", type="warning"); return
                    failures=[]
                    for definition in plan.get("collections", []):
                        try:
                            generate_collection_image(self.current_store, definition, provider=OpenAIImagesProvider(), enabled=True)
                        except Exception as exc: failures.append(f"{definition['title']}: {_safe_error(exc)}")
                    render_image_actions(plan)
                    ui.notify(f"이미지 생성 완료. 성공 {len(plan['collections'])-len(failures)} · 실패 {len(failures)}", type="positive" if not failures else "warning")
                    if failures: preview_label.set_text("이미지 실패: " + " | ".join(failures[:5]))
                ui.button("컬렉션 이미지 모두 생성", on_click=generate_all_images, icon="collections").props("outline")
                ui.button("Shopify에 컬렉션 생성/동기화", on_click=confirm_shopify_sync, icon="cloud_upload").props("color=primary")
            ui.label("Collection sync report에는 조건, 결과, Shopify ID가 기록됩니다. secrets는 포함되지 않습니다.").classes("text-xs text-slate-500")

        ui.separator()
        with ui.card().classes("w-full border border-indigo-200 bg-indigo-50"):
            ui.label("홈페이지 컬렉션 배치").classes("text-lg font-semibold")
            ui.label("테마 구성은 read_themes 권한으로만 읽습니다. 적용 버튼은 SAFE / DRY-RUN이며 Shopify theme API를 호출하지 않습니다.").classes("text-sm text-amber-800")
            theme_status = ui.label("Theme: 확인 전 · 현재 published theme").classes("font-medium")
            homepage_status = ui.label("홈페이지 계획: 아직 미리보기 전").classes("text-sm")
            homepage_result = {"value": None}
            homepage_area = ui.column().classes("w-full gap-1")

            def homepage_preview():
                try:
                    plan = plan_ref.get("plan")
                    if not plan:
                        raise ValueError("먼저 컬렉션 자동 설계를 실행하세요.")
                    snapshot = ShopifyThemeReader().discover(self.current_store)
                    theme = snapshot.get("theme") or {}
                    theme_status.set_text(f"Theme: {theme.get('name', snapshot['status'])} · {theme.get('role', '')} · {theme.get('id', '')}")
                    if snapshot.get("status") != "CONNECTED":
                        homepage_status.set_text("MANUAL PATCH MODE · " + str(snapshot.get("warning") or "Theme read scope unavailable"))
                        homepage_result["value"] = None
                        ui.notify("read_themes 권한 또는 published theme 확인이 필요합니다. live write는 수행하지 않습니다.", type="warning")
                        return
                    publisher = ShopifyCollectionPublisher()
                    image_ready = {row["collection_key"] for row in plan.get("collections", [])
                                   if publisher._image_asset(self.current_store, row["collection_key"])}
                    with connect() as con:
                        mappings = {row["collection_key"]: row["handle"] for row in con.execute(
                            "SELECT collection_key,handle FROM shopify_collection_mappings WHERE store_id=?", (self.current_store,))}
                    result = build_homepage_plan(snapshot, plan, collection_handles=mappings, image_ready=image_ready)
                    homepage_result["value"] = result
                    homepage_status.set_text(f"{result['status']} · 제안 {sum(op.get('action') == 'CREATE SECTION' for op in result.get('operations', []))}개 section · ratio {result.get('image_ratio', 'Theme default')}")
                    homepage_area.clear()
                    with homepage_area:
                        for operation in result.get("operations", []):
                            ui.label(f"{operation.get('action')} · {operation.get('collection') or operation.get('reason', '')}").classes(
                                "text-sm text-amber-800" if operation.get("action") == "CONFLICT" else "text-sm")
                        for warning in result.get("warnings", []):
                            ui.label("경고: " + warning).classes("text-sm text-amber-800")
                        ui.label("추천 컬렉션: " + ", ".join(op.get("collection", "") for op in result.get("operations", []) if op.get("collection"))).classes("text-sm")
                        ui.label(f"상품 표시 수: {result.get('products_per_section', '테마 기본값')} · 적용 위치: {result.get('template_filename', '수동 계획')}").classes("text-xs text-slate-600")
                    ui.notify("홈페이지 JSON 미리보기를 만들었습니다. 테마에는 변경을 쓰지 않았습니다.", type="positive")
                except Exception as exc:
                    homepage_status.set_text("미리보기 실패: " + _safe_error(exc))
                    ui.notify(_safe_error(exc), type="negative")

            def homepage_diff():
                result = homepage_result.get("value")
                if not result:
                    ui.notify("먼저 홈페이지 미리보기를 실행하세요.", type="warning")
                    return
                with ui.dialog() as dialog, ui.card().classes("w-[1000px] max-w-[95vw] max-h-[85vh] overflow-auto"):
                    ui.label("Homepage JSON 변경사항 · SAFE DRY RUN").classes("text-lg font-bold")
                    ui.label(json.dumps(result.get("operations", []), ensure_ascii=False, indent=2)).classes("whitespace-pre-wrap text-xs")
                    ui.label("제안 JSON").classes("font-semibold")
                    ui.label(json.dumps(result.get("proposed"), ensure_ascii=False, indent=2)).classes("whitespace-pre-wrap text-xs")
                    ui.button("닫기", on_click=dialog.close).props("flat")
                dialog.open()

            def safe_apply_homepage():
                result = homepage_result.get("value")
                if not result:
                    ui.notify("먼저 홈페이지 미리보기를 실행하세요.", type="warning")
                    return
                try:
                    saved = HomepageCollectionService().save_safe_patch(result, store_id=self.current_store)
                    ui.notify(f"SAFE/DRY-RUN 완료 · 로컬 백업/수동 패치 저장: {saved['folder']} · Shopify write 0", type="positive")
                    homepage_status.set_text("MANUAL PATCH MODE · 로컬 백업 생성 · 실제 theme write 없음")
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")

            def preview_homepage_rollback():
                try:
                    rollback = HomepageCollectionService().latest_backup(self.current_store)
                    if not rollback:
                        ui.notify("롤백 계획을 만들 백업이 없습니다.", type="warning")
                        return
                    with ui.dialog() as dialog, ui.card().classes("w-[900px] max-w-[95vw]"):
                        ui.label("롤백 계획 · 수동 적용만 가능 (theme write 없음)").classes("font-bold")
                        ui.label(f"Backup {rollback['backup_id']} · before.json을 Shopify Theme Editor에서 검토하세요.")
                        ui.label(json.dumps(rollback["before"], ensure_ascii=False, indent=2)).classes("whitespace-pre-wrap text-xs max-h-[65vh] overflow-auto")
                        ui.button("닫기", on_click=dialog.close).props("flat")
                    dialog.open()
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")

            with ui.row().classes("flex-wrap"):
                ui.button("홈페이지 미리보기", on_click=homepage_preview, icon="visibility").props("outline")
                ui.button("변경사항 보기", on_click=homepage_diff, icon="difference").props("outline")
                ui.button("홈페이지 추천 컬렉션 적용 (SAFE/DRY-RUN)", on_click=safe_apply_homepage, icon="shield").props("color=primary")
                ui.button("롤백", on_click=preview_homepage_rollback, icon="undo").props("outline")

    def _heading(self, title: str, subtitle: str | None = None):
        self.ui.label(title).classes("ss-page-title text-slate-900")
        if subtitle:
            self.ui.label(subtitle).classes("ss-help max-w-4xl")

    def _card(self, title: str, value, detail=""):
        ui = self.ui
        with ui.card().classes("min-w-40 flex-1 rounded-xl border border-slate-200 shadow-sm"):
            ui.label(title).classes("ss-help")
            ui.label(str(value)).classes("ss-kpi text-slate-900")
            if detail:
                ui.label(detail).classes("ss-help")

    def _dashboard(self):
        ui = self.ui
        from ..automation import WorkflowAutomationService
        data = dashboard_data(self.current_store)
        store = next((item for item in self.stores if item["store_id"] == self.current_store), None)
        self._heading("대시보드", f"{self.current_store} | {store['store_name'] if store else ''} 운영 현황")
        auto=_workflow_service(); interrupted=auto.interrupted(self.current_store)
        with ui.row().classes("w-full grid grid-cols-1 lg:grid-cols-3 gap-5"):
            with ui.card().classes("rounded-xl border-2 border-blue-200 p-5"):
                ui.label("오늘 할 일").classes("ss-card-title")
                ui.label("Cabin Tidy 실전 스토어 만들기").classes("ss-card-title")
                ui.button("실전 스토어 자동 구축 시작",on_click=lambda:ui.navigate.to("/production"),icon="rocket_launch").props("color=primary size=lg")
                ui.label("Source 안전검사를 먼저 완료하세요." if not interrupted else "중단된 자동 작업을 이어서 확인하세요.")
                ui.button("자동으로 진행",on_click=lambda:ui.navigate.to("/source-safety"),icon="auto_awesome").props("color=primary")
                ui.button("홈페이지 자동 완성",on_click=lambda:ui.navigate.to("/homepage"),icon="web").props("outline")
            with ui.card().classes("rounded-xl border border-slate-200 p-5"):
                ui.label("자동 작업").classes("ss-card-title")
                if interrupted:
                    active=interrupted[0]; ui.label(f"{active['workflow_key']} · {active['completed_tasks']} / {active['total_tasks']}").classes("font-semibold")
                    ui.label(f"현재 상태: {active['status']}").classes("ss-help")
                    with ui.row():
                        ui.button("이어서 실행",on_click=lambda:ui.navigate.to("/source-safety"),icon="play_arrow").props("outline")
                        ui.button("종료 상태로 두기",on_click=lambda rid=active["run_id"]:auto.stop(rid),icon="stop").props("flat color=negative")
                else: ui.label("진행 중인 자동 작업이 없습니다.").classes("ss-help")
            with ui.card().classes("rounded-xl border border-amber-200 p-5"):
                ui.label("확인 필요").classes("ss-card-title")
                waiting=sum(1 for row in interrupted if str(row["status"]).startswith("WAITING"))
                ui.label(f"{waiting}건").classes("ss-kpi")
                ui.button("보기",on_click=lambda:ui.navigate.to("/source-safety")).props("outline")
        if data["master"]["unique_products"] == 0:
            with ui.card().classes("w-full bg-blue-50 border border-blue-100 p-6"):
                ui.label("MASTER 상품이 아직 없습니다").classes("text-xl font-semibold")
                ui.label("소싱 페이지에서 5개 후보를 미리 보고, Source Inbox를 가져오거나 자동 소싱을 시작하세요.")
                ui.button("소싱 열기", on_click=lambda: ui.navigate.to("/sourcing"), icon="travel_explore")
        counts = data["store"].get("counts", {})
        with ui.row().classes("w-full flex-wrap"):
            self._card("MASTER 상품", f"{data['master']['unique_products']:,}")
            self._card("PRIMARY", f"{counts.get('PRIMARY', 0):,}")
            self._card("REVIEW", f"{counts.get('REVIEW', 0):,}")
            self._card("RESTRICTED", f"{counts.get('RESTRICTED', 0):,}")
            run = data["sourcing"] or {}
            self._card("최근 소싱", run.get("status", "없음"), run.get("started_at") or "")
            package = data["package"] or {}
            self._card("최근 package", package.get("package_status", "없음"), package.get("package_id", ""))
        with ui.row().classes("w-full"):
            ui.button("자동 소싱", on_click=lambda: ui.navigate.to("/sourcing"), icon="play_arrow")
            ui.button("상품 보기", on_click=lambda: ui.navigate.to("/products"), icon="inventory_2").props("outline")
            ui.button("Spark Center package 만들기", on_click=lambda: ui.navigate.to("/packages"), icon="outbox").props("outline")
        with ui.card().classes("w-full"):
            ui.label("최근 작업").classes("font-semibold")
            recent = []
            if data["sourcing"]:
                recent.append({"작업": "소싱", "상태": data["sourcing"]["status"], "시각": data["sourcing"]["started_at"]})
            if data["package"]:
                recent.append({"작업": "Spark Center package", "상태": data["package"]["package_status"], "시각": data["package"]["created_at"]})
            if data["errors"]:
                recent.extend({"작업": "가져오기 오류", "상태": row["error_code"], "시각": row["created_at"]} for row in data["errors"])
            if recent:
                ui.table(columns=[{"name": key, "label": key, "field": key} for key in ("작업", "상태", "시각")], rows=recent).classes("w-full")
            else:
                ui.label("표시할 최근 작업이 없습니다.").classes("text-slate-500")

    def _stores(self):
        ui = self.ui
        profile = get_store(self.current_store)
        self._heading("Store Workbench", "Store Profile, 가격 정책, 검색 주제어를 관리합니다.")
        with ui.row().classes("w-full items-start"):
            with ui.card().classes("w-full md:w-1/3"):
                ui.label(f"{profile['store_id']} | {profile['store_name']}").classes("text-xl font-bold")
                ui.label(f"Category: {profile.get('category', '')}")
                ui.label("가격 구간").classes("font-semibold mt-3")
                for band in profile.get("price_bands", []):
                    ui.label(f"{band.get('status')} · ${band.get('min')}–${band.get('max') or '∞'}")
                ui.label("Include keywords").classes("font-semibold mt-3")
                ui.label(", ".join(profile.get("include_keywords", []))).classes("text-sm")
                ui.label("Exclude keywords").classes("font-semibold mt-3")
                ui.label(", ".join(profile.get("exclude_keywords", []))).classes("text-sm")
                ui.label("Risk rules").classes("font-semibold mt-3")
                for rule in profile.get("risk_rules", []):
                    ui.label(f"{rule.get('status')}: {rule.get('code')} · {', '.join(rule.get('terms', []))}").classes("text-sm")
                sourcing = profile.get("sourcing") or {}
                ui.label("Sourcing settings").classes("font-semibold mt-3")
                ui.label(f"{sourcing.get('provider', 'keepa')} / {sourcing.get('marketplace', 'US')} · target {sourcing.get('target_candidates', 5)}")
                ui.label(f"Rating {sourcing.get('min_rating', 4.0)} · reviews {sourcing.get('min_reviews', 30)} · images {sourcing.get('min_images', 2)}")
                ui.label(f"Token budget {sourcing.get('max_tokens_per_run', 1000)}")
            with ui.card().classes("w-full md:flex-1"):
                ui.label("Keyword Studio").classes("text-xl font-bold")
                recommendation_grid = {"element": None}
                table_slot = ui.column().classes("w-full")
                async def render_recommendations():
                    try:
                        result = await asyncio.to_thread(KeywordEngine().recommend, self.current_store, 80)
                    except Exception as exc:
                        ui.notify(_safe_error(exc), type="negative"); return
                    table_slot.clear()
                    with table_slot:
                        recommendation_grid["element"] = ui.aggrid({
                            "columnDefs": [
                                {"headerName": "Keyword", "field": "keyword", "checkboxSelection": True, "headerCheckboxSelection": True, "filter": True},
                                {"headerName": "Source", "field": "source", "filter": True},
                                {"headerName": "Semantic", "field": "semantic_fit", "sortable": True},
                                {"headerName": "Yield", "field": "candidate_yield", "sortable": True},
                                {"headerName": "Price Fit", "field": "price_fit", "sortable": True},
                                {"headerName": "Risk", "field": "risk_rate", "sortable": True},
                                {"headerName": "Score", "field": "score", "sortable": True},
                                {"headerName": "Status", "field": "status", "filter": True},
                                {"headerName": "Reason", "field": "reason"},
                            ],
                            "rowData": result,
                            "rowSelection": {"mode": "multiRow"},
                            "pagination": True,
                            "paginationPageSize": 20,
                            "defaultColDef": {"sortable": True, "filter": True, "resizable": True},
                        }, modules="community").classes("w-full h-[520px]")
                async def open_amazon_selected():
                    grid = recommendation_grid["element"]
                    rows = await grid.get_selected_rows() if grid else []
                    from urllib.parse import quote_plus
                    urls = ["https://www.amazon.com/s?k=" + quote_plus(str(row.get("keyword", ""))) for row in rows[:5]]
                    if not urls:
                        ui.notify("Amazon 검색 주제어를 선택하세요.", type="warning"); return
                    script = "(urls => urls.forEach((url, i) => setTimeout(() => window.open(url, '_blank', 'noopener'), i * 350)))"
                    ui.run_javascript(script + "(" + json.dumps(urls) + ")")
                async def add_selected():
                    grid = recommendation_grid["element"]
                    if not grid:
                        ui.notify("먼저 추천 검색 주제어를 생성하세요.", type="warning"); return
                    rows = await grid.get_selected_rows()
                    outcome = KeywordEngine().add_recipes(self.current_store, [row["keyword"] for row in rows])
                    ui.notify(f"recipe {len(outcome['added'])}개 추가")
                async def exclude_selected():
                    grid = recommendation_grid["element"]
                    if not grid:
                        ui.notify("먼저 추천 검색 주제어를 생성하세요.", type="warning"); return
                    rows = await grid.get_selected_rows()
                    outcome = KeywordEngine().exclude_recommendations(self.current_store, [row["keyword"] for row in rows])
                    ui.notify(f"추천 제외 {len(outcome['excluded'])}개 저장")
                async def validate_selected():
                    grid = recommendation_grid["element"]
                    if not grid:
                        ui.notify("먼저 추천 검색 주제어를 생성하세요.", type="warning"); return
                    rows = await grid.get_selected_rows()
                    keywords = [row["keyword"] for row in rows]
                    if not keywords:
                        ui.notify("검증할 항목을 선택하세요.", type="warning"); return
                    if len(keywords) > 10:
                        ui.notify("한 번에 최대 10개를 검증할 수 있습니다.", type="warning"); return
                    api_key, _source = get_api_key(self.session_api_key)
                    if not api_key:
                        ui.notify("Keepa API key를 먼저 설정하세요.", type="warning"); return
                    try:
                        validation_provider = KeepaProvider(api_key=api_key)
                    except Exception as exc:
                        ui.notify(_safe_error(exc), type="negative"); return
                    from ..sourcing.models import Recipe
                    settings = get_store(self.current_store).get("sourcing") or {}
                    estimates = validation_provider.estimate([
                        Recipe(recipe_id=f"keyword-validation-{index}", keyword=keyword,
                               price_min=float(settings.get("price_min", 30)),
                               price_max=float(settings.get("price_max", 120)),
                               min_rating=float(settings.get("min_rating", 4)),
                               min_reviews=int(settings.get("min_reviews", 30)),
                               min_images=int(settings.get("min_images", 2)))
                        for index, keyword in enumerate(keywords)
                    ], len(keywords) * 100)
                    with ui.dialog() as dialog, ui.card():
                        ui.label(f"예상 token 약 {estimates['estimated_token_min']}–{estimates['estimated_token_max']} · "
                                 f"최대 Finder {estimates['finder_requests_max']}회 / Product Request "
                                 f"{estimates['product_requests_max']}회. 실제 사용량은 Keepa 응답 telemetry를 따릅니다.")
                        ui.button("검증", on_click=lambda: (dialog.submit(True), dialog.close()))
                        ui.button("취소", on_click=dialog.close).props("flat")
                    confirmed = await dialog
                    if not confirmed:
                        return
                    try:
                        results = await asyncio.to_thread(
                            KeywordEngine().validate, self.current_store, keywords,
                            validation_provider, max_keywords=10,
                        )
                        with ui.dialog() as report_dialog, ui.card().classes("min-w-[680px]"):
                            ui.label("Keepa 주제어 검증 결과").classes("text-lg font-bold")
                            ui.table(columns=[{"name": key, "label": key, "field": key} for key in
                                              ("keyword", "candidate_yield", "price_fit", "quality_fit", "risk_rate", "master_duplicate_rate", "tokens_consumed")],
                                     rows=results).classes("w-full")
                            ui.button("닫기", on_click=report_dialog.close)
                        report_dialog.open()
                    except Exception as exc:
                        ui.notify(_safe_error(exc), type="negative")
                with ui.row():
                    ui.button("추천 검색 주제어 만들기", on_click=render_recommendations, icon="auto_awesome")
                    ui.button("Amazon 검색 열기 (선택 최대 5)", on_click=open_amazon_selected, icon="open_in_new").props("outline")
                ui.button("선택 주제어 검증", on_click=validate_selected, icon="fact_check").props("outline")
                ui.button("선택 항목 recipe에 추가", on_click=add_selected, icon="add").props("outline")
                ui.button("제외", on_click=exclude_selected, icon="block").props("flat")
                manual_keyword = ui.input("직접 keyword 추가", placeholder="예: trunk organizer").classes("w-56")
                ui.button("수동 recipe 추가", on_click=lambda: ui.notify(
                    f"recipe {len(KeywordEngine().add_recipes(self.current_store, [manual_keyword.value])['added'])}개 추가"), icon="edit").props("outline")
                ui.button("새 Store 만들기", on_click=self._store_wizard, icon="add_business").props("outline")

    def _live_2000_panel(self):
        ui = self.ui
        service = CampaignService()
        active = {"campaign": service.active(self.current_store)}
        with ui.card().classes("w-full border-2 border-blue-200 bg-blue-50"):
            ui.label("LIVE 과제 2,000개").classes("text-xl font-bold")
            ui.label("실제 실행은 아래 시작/계속 버튼을 눌렀을 때만 시작됩니다. 검색 탭 1개와 상세 탭 1개를 재사용합니다.").classes("text-sm")
            with ui.row().classes("items-end"):
                target = ui.number("고유 후보/상세 목표", value=2000, min=1, max=10000).classes("w-48")
                search_delay = ui.number("검색 페이지 간격(초)", value=8, min=6, max=30).classes("w-48")
            status = ui.label("캠페인 없음").classes("font-medium")
            counts = ui.label("").classes("text-sm")
            estimate = ui.label("").classes("text-sm text-amber-800")
            search_health = ui.label("검색 Worker: 연결 대기").classes("text-sm")
            detail_health = ui.label("").classes("text-sm")
            search_warning = ui.label("").classes("text-sm text-red-700 font-medium")
            package_summary_label = ui.label("").classes("text-sm text-amber-900 whitespace-pre-line")

            def render(campaign):
                if not campaign: return
                active["campaign"] = campaign
                status.set_text(f"{campaign['campaign_id']} · {campaign['status']}")
                counts.set_text(f"후보 목표 {campaign['candidate_target']:,} · 고유 후보 {campaign['unique_candidates']:,} · 중복 {campaign['duplicates']:,} · 검색 페이지 {campaign['search_pages']:,} · 상세 완료 {campaign['detail_complete']:,} · MASTER {campaign['master_imported']:,} · Store 분류 {campaign['classified']:,} · 실패 {campaign['failed']:,} · Spark 준비 {campaign['classified']:,}")
                package_summary = campaign.get("campaign_package_summary")
                if package_summary:
                    breakdown = ", ".join(f"{key}: {value}" for key, value in package_summary["campaign_excluded_by_status"].items()) or "상태 제외 없음"
                    package_summary_label.set_text(
                        f"Campaign 상품 {package_summary['campaign_total']:,} · Spark exportable {package_summary['campaign_exportable']:,} · 제외 {package_summary['campaign_excluded']:,}\n"
                        f"상태별 제외: {breakdown} · Store decision 누락 {package_summary['campaign_missing_decision']:,} · MASTER 누락 {package_summary['campaign_missing_product']:,}"
                    )
                else:
                    package_summary_label.set_text("")
                if campaign.get("search_stage_complete"):
                    search_health.set_text(f"검색 단계: COMPLETE · 후보 {campaign['unique_candidates']:,} / {campaign['candidate_target']:,}")
                    detail_health.set_text(f"상세 Worker: {campaign.get('detail_worker_status', 'UNKNOWN')} · 현재 ASIN {campaign.get('detail_current_asin') or '대기'} · 마지막 성공 {campaign.get('detail_last_success_at') or '없음'} · 상세 완료 {campaign['detail_complete']:,} / {campaign['detail_target']:,} · 실패 {campaign.get('detail_failure_count', 0)}")
                    search_warning.set_text("")
                else:
                    worker_state = campaign.get("search_worker_status", "NOT_CONNECTED")
                    search_health.set_text(
                        f"검색 Worker: {worker_state} · keyword {campaign.get('current_keyword') or '대기'} · page {campaign.get('current_page') or 0} · "
                        f"마지막 캡처 {campaign.get('last_search_capture_at') or '없음'} · 오류 {campaign.get('last_search_error') or '없음'}"
                    )
                    detail_health.set_text("")
                seconds = int(campaign["minimum_remaining_seconds"])
                estimate.set_text(f"최소 예상시간 {seconds // 3600}시간 {(seconds % 3600) // 60}분 · 실제 Amazon 로딩/확인 화면 때문에 더 길어질 수 있습니다.")
                warning = ""
                if not campaign.get("search_stage_complete") and campaign["status"] == "RUNNING" and campaign["search_pages"] == 0 and campaign.get("started_at"):
                    try:
                        from datetime import datetime, timezone
                        started = datetime.fromisoformat(campaign["started_at"].replace("Z", "+00:00"))
                        if (datetime.now(timezone.utc) - started).total_seconds() >= 15:
                            warning = "검색 탭은 열렸지만 자동 캡처 응답이 없습니다. 기존 캠페인에서 '과제 2,000 계속'을 눌러 검색 Worker를 다시 연결하세요."
                    except (TypeError, ValueError): pass
                if not campaign.get("search_stage_complete") and campaign.get("last_search_error"):
                    warning = f"마지막 검색 오류: {campaign['last_search_error']}"
                search_warning.set_text(warning)

            def command(action):
                try:
                    campaign = active.get("campaign")
                    if not campaign:
                        campaign = service.create_live_2000(self.current_store, int(target.value), int(search_delay.value), 4)
                    if action in {"START", "RESUME"}:
                        preflight = service.preflight(campaign["campaign_id"])
                        if not preflight["database_backup_created"]: raise RuntimeError("Preflight DB backup failed")
                    campaign = service.action(campaign["campaign_id"], action)
                    render(campaign)
                    if action in {"START", "RESUME", "RETRY"}:
                        message = {"source":"shopsource-studio-ui", "type":"campaign-command", "campaignId":campaign["campaign_id"], "action":"RESUME"}
                        ui.run_javascript("window.postMessage(" + json.dumps(message) + ", window.location.origin)")
                    ui.notify("캠페인 상태를 갱신했습니다.", type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            def make_package():
                try:
                    result = service.create_package(active["campaign"]["campaign_id"])
                    ui.notify(f"Spark Package {result['package_id']} · exportable {result['campaign_exportable']:,}개 · 제외 {result['campaign_excluded']:,}개", type="positive")
                    updated = dict(active["campaign"])
                    updated["spark_package_id"] = result["package_id"]
                    updated["campaign_package_summary"] = {key: result[key] for key in (
                        "campaign_total", "campaign_exportable", "campaign_excluded",
                        "campaign_excluded_by_status", "campaign_missing_decision", "campaign_missing_product")}
                    render(updated)
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            def make_report():
                try:
                    result = service.report(active["campaign"]["campaign_id"])
                    ui.notify("과제 summary report 생성: " + result["folder"], type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            with ui.row():
                ui.button("LIVE 과제 2,000 시작", on_click=lambda: command("START"), icon="play_arrow")
                ui.button("과제 2,000 계속", on_click=lambda: command("RESUME"), icon="resume").props("outline")
                ui.button("검색 Worker 다시 연결", on_click=lambda: command("RESUME"), icon="sync").props("outline")
                ui.button("일시정지", on_click=lambda: command("PAUSE"), icon="pause").props("outline")
                ui.button("실패만 재시도", on_click=lambda: command("RETRY"), icon="replay").props("outline")
                ui.button("취소", on_click=lambda: command("CANCEL"), icon="stop").props("outline color=negative")
            with ui.row():
                ui.button("이 캠페인 상품으로 Spark Package 만들기", on_click=make_package, icon="outbox").props("outline")
                ui.button("과제 summary report 생성", on_click=make_report, icon="description").props("outline")
            with ui.expansion("Spark / Shopify 결과 기록", icon="fact_check").classes("w-full"):
                ui.label("Spark UI에서 직접 확인한 값만 입력하세요. 포함 + 제외는 전체와 같아야 합니다.").classes("text-xs")
                with ui.row():
                    dataset_id = ui.input("Spark dataset ID").classes("w-48")
                    spark_total = ui.number("Spark 전체", min=0).classes("w-36")
                    spark_included = ui.number("Spark 포함", min=0).classes("w-36")
                    spark_excluded = ui.number("Spark 제외", min=0).classes("w-36")
                    verified_count = ui.number("검증 상품 수", min=0).classes("w-36")
                with ui.row():
                    upload_result = ui.select(["SUCCESS", "PARTIAL", "FAILED"], label="Shopify 업로드 결과").classes("w-48")
                    uploaded_count = ui.number("Shopify 업로드 수(선택)", min=0).classes("w-48")
                    outcome_notes = ui.input("메모").classes("w-72")
                def save_outcome():
                    try:
                        campaign = active.get("campaign")
                        if not campaign: raise ValueError("먼저 캠페인을 만드세요.")
                        result = service.record_outcome(campaign["campaign_id"], spark_dataset_id=dataset_id.value or None,
                            spark_total=None if spark_total.value is None else int(spark_total.value),
                            spark_included=None if spark_included.value is None else int(spark_included.value),
                            spark_excluded=None if spark_excluded.value is None else int(spark_excluded.value),
                            verified_product_count=None if verified_count.value is None else int(verified_count.value),
                            shopify_upload_result=upload_result.value or None,
                            shopify_uploaded_count=None if uploaded_count.value is None else int(uploaded_count.value), notes=outcome_notes.value or "")
                        render(result); ui.notify("Spark/Shopify 결과를 기록했습니다.", type="positive")
                    except Exception as exc: ui.notify(_safe_error(exc), type="negative")
                ui.button("확인 결과 저장", on_click=save_outcome, icon="save").props("outline")
            ui.label("CAPTCHA/확인 화면, 확장 연결 끊김 또는 반복 준비 실패 시 자동 일시정지하며 우회하지 않습니다. Spark에서 전체/포함/제외와 Shopify 업로드 결과를 확인 후 기록하세요.").classes("text-xs text-slate-600")
            ui.label("ShopSource Capture v0.1.7을 다시 로드한 뒤 이 기존 캠페인에서 '과제 2,000 계속'을 누르세요.").classes("text-xs font-medium text-amber-800")
            if active["campaign"]: render(active["campaign"])
            def poll():
                if active.get("campaign"):
                    try: render(service.get(active["campaign"]["campaign_id"]))
                    except Exception: pass
            ui.timer(3.0, poll)

    def _sourcing(self):
        ui = self.ui
        profile = get_store(self.current_store)
        config = profile.get("sourcing") or {}
        self._auto_sourcing_planner_panel()
        ui.separator()
        self._live_2000_panel()
        ui.separator()
        ui.label("무료 브라우저 소싱 · API 필요 없음").classes("text-lg font-bold")
        ui.label("Amazon 검색과 상품 페이지를 직접 열고, 현재 화면에 보이는 자료만 확장으로 가져옵니다.")
        self._browser_capture_panel()
        ui.separator()
        ui.label("Keepa 자동 소싱 · 선택 유료 API").classes("text-lg font-semibold")
        self._heading("자동 소싱", "Recipe별 후보 탐색과 실행 상태를 관리합니다.")
        target = ui.number("Target candidates", value=int(get_app_setting("default_target", config.get("target_candidates", 5))), min=1, max=500)
        token_budget = ui.number("최대 token / 실행", value=int(get_app_setting("default_token_budget", config.get("max_tokens_per_run", 1000))), min=1)
        token_reserve = ui.number("남길 token", value=config.get("min_tokens_reserve", 100), min=0)
        recipes = config.get("recipes") or []
        ui.table(columns=[{"name": key, "label": key, "field": key} for key in ("keyword", "enabled", "verified", "last_yield", "last_run")],
                 rows=[{"keyword": row.get("keyword", "") if isinstance(row, dict) else row,
                        "enabled": True, "verified": False, "last_yield": "—", "last_run": "—"} for row in recipes]).classes("w-full")
        status = ui.label("Status: IDLE")
        progress = ui.linear_progress(value=0, show_value=False).classes("w-full")
        async def preview():
            result = await asyncio.to_thread(SourcingEngine().preview, self.current_store, int(target.value))
            with ui.dialog() as dialog, ui.card().classes("min-w-[560px]"):
                ui.label("미리보기 · network 요청 없음").classes("font-bold")
                ui.json_editor({"content": {"json": result}}).classes("w-full h-96")
                ui.button("닫기", on_click=dialog.close)
            dialog.open()
        async def run_sourcing():
            api_key, _source = get_api_key(self.session_api_key)
            try:
                provider = KeepaProvider(api_key=api_key)
            except Exception as exc:
                ui.notify(_safe_error(exc), type="negative"); return
            self.active_run_id = new_run_id(self.current_store)
            run_id = self.active_run_id
            status.set_text("연결 및 소싱 실행 중…")
            progress.value = 0.1
            def poll_run():
                try:
                    current = SourcingEngine.status(run_id)
                except KeyError:
                    return
                status.set_text(self._run_status_text(current))
                progress.value = min(0.95, current.get("hydrated_products", 0) / max(1, int(target.value)))
                if current.get("status") in {"DONE", "FAILED", "CANCELLED", "PAUSED", "PAUSED_TOKEN_BUDGET"}:
                    poll_timer.deactivate()
            poll_timer = ui.timer(1.0, poll_run)
            try:
                result = await asyncio.to_thread(
                    SourcingEngine(provider).run, self.current_store, int(target.value),
                    max_tokens_per_run=int(token_budget.value), min_tokens_reserve=int(token_reserve.value),
                    run_id=run_id,
                )
                status.set_text(self._run_status_text(result))
                progress.value = 1
                if result.get("status") == "DONE":
                    ui.notify("소싱 후 현재 Store를 재분류했습니다.", type="positive")
                else:
                    ui.notify(f"Run 상태: {result.get('status')}", type="warning")
            except Exception as exc:
                status.set_text(f"오류: {_safe_error(exc)}"); progress.value = 0
                ui.notify(_safe_error(exc), type="negative")
            finally:
                poll_timer.deactivate()
        def pause_run():
            if not self.active_run_id:
                ui.notify("일시정지 가능한 실행이 없습니다.", type="warning"); return
            result = SourcingEngine.pause(self.active_run_id)
            status.set_text(self._run_status_text(result))
        def cancel_run():
            if not self.active_run_id:
                ui.notify("취소 가능한 실행이 없습니다.", type="warning"); return
            status.set_text(self._run_status_text(SourcingEngine.cancel(self.active_run_id)))
        async def resume_run():
            if not self.active_run_id:
                ui.notify("계속할 run을 기록에서 선택하세요.", type="warning"); return
            key, _ = get_api_key(self.session_api_key)
            try:
                result = await asyncio.to_thread(SourcingEngine(KeepaProvider(api_key=key)).resume, self.active_run_id)
                status.set_text(self._run_status_text(result))
            except Exception as exc:
                ui.notify(_safe_error(exc), type="negative")
        with ui.row():
            ui.button("Keyword Studio", on_click=lambda: ui.navigate.to("/stores"), icon="manage_search").props("outline")
            ui.button("Dry Run", on_click=preview, icon="preview").props("outline")
            ui.button("자동 소싱 시작", on_click=run_sourcing, icon="play_arrow")
            ui.button("일시정지", on_click=pause_run, icon="pause").props("outline")
            ui.button("계속", on_click=resume_run, icon="resume").props("outline")
            ui.button("취소", on_click=cancel_run, icon="cancel").props("flat color=negative")
        ui.separator()
        with ui.card().classes("w-full border border-slate-200"):
            ui.label("소싱 파일 가져오기 · API 필요 없음").classes("text-lg font-semibold")
            ui.label(f"상품 JSON 폴더: {AMAZON_INBOX_DIR}")
            async def import_source_files():
                try:
                    imported = await asyncio.to_thread(import_amazon_source)
                    classified = await asyncio.to_thread(classify_store, self.current_store)
                    ui.notify(f"MASTER import 완료: {imported['inserted']} 신규 · {imported['updated']} 갱신 · 분류 {classified['processed']}개", type="positive")
                    ui.navigate.to("/products")
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")
            with ui.row():
                ui.button("소싱 폴더 열기", on_click=lambda: self._open_folder(str(AMAZON_INBOX_DIR)), icon="folder_open").props("outline")
                ui.button("소싱 파일 가져오기", on_click=import_source_files, icon="drive_folder_upload")
        ui.label("상품 이미지 사용권/재사용 조건은 별도 확인이 필요합니다.").classes("text-xs text-amber-700")
        ui.separator()
        ui.label("최근 run history").classes("text-lg font-semibold")
        self._render_runs(list_sourcing_runs(self.current_store, 30))

    def _auto_sourcing_planner_panel(self):
        ui = self.ui
        with ui.card().classes("w-full border-2 border-emerald-200 bg-emerald-50"):
            ui.label("스토어 자동 소싱 계획").classes("text-xl font-bold")
            ui.label("목표 수량을 바탕으로 카테고리와 quota를 계획합니다. 실제 Amazon 수집은 아래 [자동 소싱 시작]을 직접 눌렀을 때만 시작됩니다.").classes("text-sm")
            with ui.row().classes("items-end"):
                target = ui.number("후보 목표", value=10000, min=1, max=50000).classes("w-48")
                mode = ui.select({"fast": "빠르게", "balanced": "균형", "deep": "깊게"}, value="balanced", label="모드").classes("w-40")
            with ui.expansion("고급 설정", icon="tune"):
                detail_ratio = ui.number("상세 보강 비율", value=100, min=1, max=100, suffix="%").classes("w-48")
                keyword_cap = ui.number("카테고리별 활성 keyword 상한", value=15, min=3, max=30).classes("w-64")
                page_cap = ui.number("keyword별 페이지 상한", value=5, min=1, max=20).classes("w-64")
                stale_pages = ui.number("연속 무성과 페이지", value=2, min=1, max=10).classes("w-64")
                unique_cap = ui.number("keyword별 고유 후보 상한", value=300, min=1, max=5000).classes("w-64")
            summary = ui.label("계획을 만들면 축약 미리보기가 표시됩니다.").classes("text-sm")
            preview = ui.column().classes("w-full gap-1")
            plan_ref = {"plan": None, "campaign": None}
            execution_status = ui.label("실행 대기 · Amazon 검색은 시작되지 않았습니다.").classes("text-sm font-medium")
            persisted_auto = CampaignService().active(self.current_store, "AUTO_STORE")
            if persisted_auto:
                plan_ref["campaign"] = persisted_auto
                execution_status.set_text(
                    f"{persisted_auto['campaign_id']} · {persisted_auto['status']} · 후보 {persisted_auto['unique_candidates']:,}/{persisted_auto['candidate_target']:,} · "
                    f"중복 {persisted_auto['duplicates']:,} · 검색 {persisted_auto['search_pages']:,}p · 상세 {persisted_auto['detail_complete']:,} · "
                    f"MASTER {persisted_auto['master_imported']:,} · 분류 {persisted_auto['classified']:,} · 실패 {persisted_auto['failed']:,} · "
                    f"현재 {persisted_auto.get('current_keyword') or '대기'} / p{persisted_auto.get('current_page') or 0} · "
                    f"Worker {persisted_auto.get('search_worker_status')} · 오류 {persisted_auto.get('last_search_error') or '없음'}"
                )

            def create_plan():
                try:
                    plan = CategoryPlanner().create_plan(
                        self.current_store, int(target.value), mode=mode.value or "balanced",
                        detail_ratio=float(detail_ratio.value or 100) / 100,
                        advanced={"max_active_keywords_per_category": int(keyword_cap.value),
                                  "max_pages_per_keyword": int(page_cap.value),
                                  "stale_pages": int(stale_pages.value),
                                  "max_unique_candidates_per_keyword": int(unique_cap.value)},
                    )
                    plan_ref["plan"] = plan
                    summary.set_text(f"v{plan['version']} · 목표 {plan['total_candidate_target']:,} · 상세 {plan['detail_target']:,} · 카테고리 {len(plan['categories'])} · keyword pool {plan['keyword_pool_total']:,} · 기본 활성 {plan['active_keyword_count']:,}")
                    preview.clear()
                    with preview:
                        for category in plan["categories"]:
                            active = min(plan["settings"]["max_active_keywords_per_category"], sum(k["enabled"] for k in category["keywords"]))
                            ui.label(f"{category['category_name']} · quota {category['quota']:,} · 활성 {active} / pool {len(category['keywords'])}").classes("text-sm")
                    ui.notify("자동 소싱 계획을 저장했습니다. 실제 Amazon 실행은 시작하지 않았습니다.", type="positive")
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")

            def render_execution(campaign):
                plan_ref["campaign"] = campaign
                execution_status.set_text(
                    f"{campaign['campaign_id']} · {campaign['status']} · 후보 {campaign['unique_candidates']:,}/{campaign['candidate_target']:,} · "
                    f"중복 {campaign['duplicates']:,} · 검색 {campaign['search_pages']:,}p · 상세 {campaign['detail_complete']:,} · "
                    f"MASTER {campaign['master_imported']:,} · 분류 {campaign['classified']:,} · 실패 {campaign['failed']:,} · "
                    f"현재 {campaign.get('current_keyword') or '대기'} / p{campaign.get('current_page') or 0} · "
                    f"Worker {campaign.get('search_worker_status')} · 오류 {campaign.get('last_search_error') or '없음'}"
                )

            def run_auto(action="START"):
                try:
                    campaign = plan_ref.get("campaign")
                    if campaign is None:
                        plan = plan_ref.get("plan")
                        if plan is None: raise ValueError("먼저 자동 소싱 계획을 만드세요.")
                        campaign = CampaignService().create_auto_store(plan["plan_id"])
                    campaign = CampaignService().action(campaign["campaign_id"], action)
                    render_execution(campaign)
                    if action in {"START", "RESUME", "RETRY"}:
                        message = {"source":"shopsource-studio-ui", "type":"campaign-command",
                                   "campaignId":campaign["campaign_id"], "action":"RESUME"}
                        ui.run_javascript("window.postMessage(" + json.dumps(message) + ", window.location.origin)")
                    ui.notify("검색 Worker에 명시적으로 시작 명령을 보냈습니다." if action in {"START", "RESUME", "RETRY"} else "캠페인을 취소했습니다.", type="positive")
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")

            def pause_auto():
                try:
                    campaign = plan_ref.get("campaign")
                    if not campaign: raise ValueError("실행 중인 자동 소싱 캠페인이 없습니다.")
                    render_execution(CampaignService().action(campaign["campaign_id"], "PAUSE"))
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            def auto_cancel():
                try:
                    campaign = plan_ref.get("campaign")
                    if not campaign: raise ValueError("취소할 자동 소싱 캠페인이 없습니다.")
                    render_execution(CampaignService().action(campaign["campaign_id"], "CANCEL"))
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            def auto_package():
                try:
                    campaign = plan_ref.get("campaign")
                    if not campaign: raise ValueError("먼저 자동 소싱을 시작하세요.")
                    result = CampaignService().create_package(campaign["campaign_id"])
                    ui.notify(f"Spark package 생성 완료 · 안전 포함 {result['campaign_exportable']:,} · 제외 {result['campaign_excluded']:,}. Desktop staging은 실행하지 않았습니다.", type="positive")
                    render_execution(CampaignService().get(campaign["campaign_id"]))
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            def auto_report():
                try:
                    campaign = plan_ref.get("campaign")
                    if not campaign: raise ValueError("먼저 자동 소싱을 시작하세요.")
                    result = CampaignService().report(campaign["campaign_id"])
                    ui.notify("소싱 summary report 생성: " + result["folder"], type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            ui.button("자동 소싱 계획 만들기", on_click=create_plan, icon="auto_awesome")
            with ui.row():
                ui.button("자동 소싱 시작", on_click=lambda: run_auto("START"), icon="play_arrow").props("color=positive")
                ui.button("일시정지", on_click=pause_auto, icon="pause").props("outline")
                ui.button("재개 / Worker 연결", on_click=lambda: run_auto("RESUME"), icon="resume").props("outline")
                ui.button("실패 상세 재시도", on_click=lambda: run_auto("RETRY"), icon="refresh").props("outline")
                ui.button("캠페인 취소", on_click=auto_cancel, icon="cancel").props("outline color=negative")
                ui.button("Spark Package 생성", on_click=auto_package, icon="inventory_2").props("outline")
                ui.button("Summary report", on_click=auto_report, icon="description").props("outline")

    @staticmethod
    def _run_status_text(run):
        checkpoint = run.get("checkpoint", {})
        return (f"{run['run_id']} · {run['status']} · recipes {checkpoint.get('recipe_index', 0)} · "
                f"candidates {run.get('discovered_asins', 0)} · hydrated {run.get('hydrated_products', 0)} · "
                f"MASTER +{run.get('inserted', 0)} / updated {run.get('updated', 0)} · "
                f"tokens {run.get('tokens_consumed', 0)} / left {run.get('tokens_left')}")

    def _render_runs(self, rows):
        if not rows:
            self.ui.label("아직 sourcing run이 없습니다.").classes("text-slate-500"); return
        self.ui.table(columns=[{"name": key, "label": key, "field": key, "sortable": True} for key in
                               ("run_id", "provider", "status", "target_candidates", "discovered_asins", "hydrated_products", "inserted", "updated", "tokens_consumed", "started_at")],
                      rows=rows, row_key="run_id", pagination=20).classes("w-full")

    def _products(self):
        ui = self.ui
        self._heading("상품", "검색과 필터를 SQLite에 적용하고 100행 단위로 불러옵니다.")
        state = {"page": 0, "page_size": 100, "sort": "asin", "descending": False, "rows": []}
        search = ui.input("ASIN / 상품명 / 브랜드 검색").classes("w-96")
        status_filter = ui.select(STATUS_OPTIONS, value="ALL", label="Status").classes("w-44")
        source_filter = ui.select(PRODUCT_SOURCE_OPTIONS, value="ALL", label="Source").classes("w-52")
        selected_asins = ui.label("선택 0개")
        products_status = ui.label("상품 목록을 불러오는 중입니다.").classes("text-sm text-slate-600")
        grid_container = ui.column().classes("w-full")
        grid_ref = {"grid": None}
        with grid_container:
            grid_ref["grid"] = ui.aggrid(product_grid_options(), modules="community").classes("w-full h-[620px]")
            def on_product_row_clicked(event):
                row = product_row_from_event_args(event.args)
                product_id = row.get("id")
                if product_id is not None:
                    self._show_product_detail(int(product_id))
            grid_ref["grid"].on("rowClicked", on_product_row_clicked, ["data"])
        footer = ui.label("")
        def load_page():
            try:
                data = product_page(store_id=self.current_store, page=state["page"], page_size=state["page_size"],
                                    search=search.value or "", status=status_filter.value or "ALL",
                                    source=source_filter.value or "ALL", sort=state["sort"],
                                    descending=state["descending"])
                state["rows"] = data["rows"]
                rows = [{**row, "thumbnail": (row["images"] or [""])[0],
                         "first_seen": row["first_seen_at"], "last_seen": row["last_seen_at"]} for row in data["rows"]]
                # Keep one mounted grid and explicitly send its initial and later data.
                update_product_grid(grid_ref["grid"], rows)
                pages = max(1, (data["total"] + data["page_size"] - 1) // data["page_size"])
                footer.set_text(f"총 {data['total']:,}개 · 페이지 {data['page'] + 1}/{pages} · 한 페이지 {data['page_size']}개")
                if data["total"]:
                    products_status.set_text(f"상품 {data['total']}개 불러옴 · Grid 전달 {len(rows)}개 · Source {source_filter.value or 'ALL'} · Status {status_filter.value or 'ALL'}")
                else:
                    products_status.set_text("현재 필터에 해당하는 상품이 없습니다.")
            except Exception as exc:
                products_status.set_text(f"상품 목록을 불러오지 못했습니다: {_safe_error(exc)}")
                ui.notify(_safe_error(exc), type="negative")
        async def get_selected():
            grid = grid_ref["grid"]
            rows = await grid.get_selected_rows() if grid else []
            selected_asins.set_text(f"선택 {len(rows)}개")
            return rows
        async def apply_status(status):
            rows = await get_selected()
            if not rows:
                ui.notify("상품을 선택하세요.", type="warning"); return
            changed = bulk_override(self.current_store, [int(row["id"]) for row in rows], status,
                                    "Operator UI V2 bulk action")
            ui.notify(f"{changed}개 상품을 {status}로 수동 지정했습니다.", type="positive")
            load_page()
        async def clear_selected():
            rows = await get_selected()
            changed = clear_bulk_override(self.current_store, [int(row["id"]) for row in rows])
            ui.notify(f"{changed}개 수동 override를 해제했습니다.")
            load_page()
        async def package_selected():
            rows = await get_selected()
            if not rows:
                ui.notify("Spark Center package로 보낼 상품을 선택하세요.", type="warning"); return
            self.package_selected_asins = selected_product_asins(rows)
            self.package_selected_statuses = sorted({row.get("final_status") for row in rows if row.get("final_status")})
            ui.navigate.to("/packages")
        with ui.row().classes("items-center flex-wrap"):
            ui.button("검색/필터 적용", on_click=load_page, icon="search")
            ui.select(["asin", "title", "price", "status", "fit_score", "first_seen", "last_seen"], value="asin", label="정렬")\
                .classes("w-40").on_value_change(lambda event: (state.update(sort=event.value), load_page()))
            ui.select([25, 50, 100, 200], value=100, label="행 수").classes("w-32").on_value_change(
                lambda event: (state.update(page=0, page_size=int(event.value)), load_page()))
            ui.button("PRIMARY 승격", on_click=lambda: apply_status("PRIMARY"))
            ui.button("RESERVE 이동", on_click=lambda: apply_status("RESERVE_B")).props("outline")
            ui.button("REVIEW", on_click=lambda: apply_status("REVIEW")).props("outline")
            ui.button("ARCHIVE", on_click=lambda: apply_status("ARCHIVED")).props("outline")
            ui.button("Override 해제", on_click=clear_selected).props("flat")
            ui.button("선택 상품 package", on_click=package_selected, icon="outbox").props("outline")
            selected_asins
        with ui.row().classes("items-center"):
            ui.button("이전", on_click=lambda: (state.update(page=max(0, state['page'] - 1)), load_page())).props("outline")
            footer
            ui.button("다음", on_click=lambda: (state.update(page=state["page"] + 1), load_page())).props("outline")
        load_page()

    def _show_product_detail(self, product_id: int):
        ui = self.ui
        detail = product_detail(product_id, self.current_store)
        with ui.dialog() as dialog, ui.card().classes("w-[760px] max-w-[95vw] max-h-[90vh] overflow-auto"):
            ui.label(detail["title"]).classes("text-xl font-bold")
            ui.label(f"ASIN {detail['asin']} · {detail['source_kind']} · ${detail['price']}")
            images = detail.get("images") or []
            if images:
                with ui.row().classes("overflow-auto"):
                    for url in images[:8]: ui.image(url).classes("w-32 h-32 object-contain")
            decision = detail.get("decision")
            if decision:
                ui.label(f"Fit {decision['fit_score']} · {decision['price_status']} · {decision['risk_status']} · {decision['final_status']}")
                ui.label("Risk / classify reason: " + ", ".join(decision.get("reasons", [])))
                with ui.row():
                    for label, status in (("PRIMARY 승격", "PRIMARY"), ("RESERVE 강등", "RESERVE_B"),
                                          ("REVIEW", "REVIEW"), ("RESTRICTED 해제", "PRIMARY")):
                        ui.button(label, on_click=lambda status=status: (bulk_override(self.current_store, [product_id], status), ui.notify("수동 override 저장"), dialog.close()))
                    ui.button("Override 해제", on_click=lambda: (clear_bulk_override(self.current_store, [product_id]), ui.notify("override 해제"), dialog.close())).props("outline")
            ui.label("Occurrence history").classes("font-semibold mt-3")
            ui.table(columns=[{"name": key, "label": key, "field": key} for key in
                              ("job_id", "source_file", "collected_at", "source_url", "source_kind")],
                     rows=detail["occurrences"], pagination=10).classes("w-full")
            with ui.expansion("Raw payload (고급)"):
                ui.json_editor({"content": {"json": json.loads(detail["raw_json"]) if detail.get("raw_json") else {}}}).classes("w-full h-80")
            ui.button("닫기", on_click=dialog.close)
        dialog.open()

    def _packages(self):
        ui = self.ui
        ui.label("Spark Desktop v1.0.3 — 안전한 로컬 dataset staging").classes("text-lg font-semibold")
        ui.label(
            "Spark Desktop의 데이터 불러오기는 외부 폴더를 복사하지 않습니다. "
            "이 기능은 선택 Package의 상품 JSON만 Spark datasets 폴더에 새 dataset으로 원자적으로 복사하며, 기존 dataset은 덮어쓰지 않습니다."
        ).classes("text-amber-800")
        desktop_stage_status = ui.label("Spark Desktop staging: 준비됨 · 실제 Spark load: NOT VERIFIED").classes("text-sm text-slate-700")
        ui.label("Browser Capture to Spark Center mapping is not portal-verified. Start with a 5-product round-trip; packages over 100 require confirmation.").classes("text-amber-800")
        self._heading("Packages", "프로젝트-local package, Spark Desktop staging, Spark Center Portal handoff는 서로 다른 상태입니다.")
        status = ui.select(STATUS_OPTIONS, value=self.package_selected_statuses or ["PRIMARY"],
                           multiple=True, label="Status").classes("w-64")
        limit = ui.number("상품 수", value=50, min=1, max=5000).classes("w-32")
        ui.label("업로드 대상은 exports/spark_center/<store>/ready/<package_id> 폴더 하나입니다.").classes("text-amber-800")
        package_validation_status = ui.label(
            "Local JSON validation: 대기 · Observed Spark schema compatibility: 대기 · Actual Spark load: NOT VERIFIED"
        ).classes("text-sm text-slate-700")
        ui.label("로컬 JSON/schema 검사는 Spark Desktop의 실제 dataset load 성공을 의미하지 않습니다.").classes("text-xs text-amber-800")
        ui.label(
            "현재 Spark Desktop 폴더 선택은 선택한 경로의 폴더명만 자체 storage/datasets에서 엽니다. 외부 ready 폴더의 JSON을 가져오지는 않습니다."
        ).classes("text-xs text-amber-800")
        if self.package_selected_asins:
            ui.label(f"상품 페이지에서 선택한 {len(self.package_selected_asins)}개 ASIN 사용 예정")
        table_slot = ui.column().classes("w-full")
        def stage_desktop(package_id):
            try:
                result = stage_package_for_spark_desktop(package_id)
                desktop_stage_status.set_text(
                    f"Spark Desktop staged: {result['dataset_id']} · {result['product_count']}개 · SHA-256 PASS · "
                    f"{result['destination_path']} · 다음 단계: Spark > 데이터 불러오기에서 이 dataset 폴더 선택 "
                    "(실제 load는 사용자가 확인하기 전까지 NOT VERIFIED)"
                )
                ui.notify(
                    f"Staged {result['dataset_id']} · {result['product_count']} products · SHA-256 PASS",
                    type="positive",
                )
                refresh_packages()
            except Exception as exc:
                message = (
                    "동일한 Spark Dataset ID가 이미 존재합니다. 기존 데이터는 덮어쓰지 않았습니다. 새 package ID를 사용하세요."
                    if isinstance(exc, DatasetAlreadyExists) else _safe_error(exc)
                )
                desktop_stage_status.set_text(f"Spark Desktop staging 실패: {message}")
                ui.notify(message, type="negative")

        def confirm_desktop(package_id, product_count):
            with ui.dialog() as dialog, ui.card():
                ui.label(
                    f"Spark Desktop에서 이 Dataset이 전체 {product_count}개로 표시되는 것을 직접 확인했습니까? "
                    "확인 버튼은 Spark Desktop load만 기록하며 Spark Center Portal/Shopify 상태는 변경하지 않습니다."
                )
                with ui.row():
                    ui.button("취소", on_click=dialog.close).props("flat")
                    def save_confirmation():
                        try:
                            confirm_spark_desktop_roundtrip(package_id, confirmed=True)
                            ui.notify("Spark Desktop load 확인을 기록했습니다.", type="positive")
                        except Exception as exc:
                            ui.notify(_safe_error(exc), type="negative")
                            return
                        dialog.close()
                        refresh_packages()
                    ui.button("직접 확인했습니다", on_click=save_confirmation).props("color=positive")
            dialog.open()

        def refresh_packages():
            table_slot.clear()
            rows = list_packages(self.current_store, 50)
            with table_slot:
                ui.table(columns=[{"name": key, "label": key, "field": key, "sortable": True} for key in
                                  ("created_at", "store_name", "package_id", "product_count", "validation_status", "package_status", "uploaded_at")],
                         rows=rows, row_key="package_id", pagination=20).classes("w-full")
                for row in rows[:20]:
                    with ui.row().classes("w-full items-center border-b py-2"):
                        ui.label(f"{row['package_id']} · {row['package_status']} · {row['product_count']}개").classes("flex-1")
                        ui.label(
                            f"Desktop: {'LOAD VERIFIED' if row['spark_desktop_roundtrip_verified'] else ('STAGED / HASH PASS' if row['desktop_hash_verified'] else 'NOT STAGED')}"
                        ).classes("text-xs text-slate-600")
                        if row["desktop_destination_path"]:
                            ui.label(f"Dataset {row['desktop_dataset_id']} · {row['desktop_destination_path']}").classes("text-xs text-slate-500 break-all")
                        ui.button("폴더 열기", on_click=lambda path=row["output_path"]: self._open_folder(path)).props("outline dense")
                        if row["package_status"] == "CREATED" and row["validation_status"] == "PASS" and not row["desktop_dataset_id"]:
                            ui.button("Spark Desktop에 설치", on_click=lambda pid=row["package_id"]: stage_desktop(pid)).props("outline dense")
                        if row["desktop_hash_verified"] and not row["spark_desktop_roundtrip_verified"]:
                            ui.button(
                                "Desktop 로드 확인",
                                on_click=lambda pid=row["package_id"], count=row["desktop_product_count"]: confirm_desktop(pid, count),
                            ).props("color=positive dense")
                        ui.button("경로 복사", on_click=lambda path=row["output_path"]: ui.run_javascript(
                            f"navigator.clipboard.writeText({json.dumps(path)})")).props("flat dense")
                        if row["package_status"] == "CREATED":
                            def mark_uploaded(pid=row["package_id"]):
                                mark_package(pid, "UPLOADED", "Marked in UI V2")
                                ui.notify("사용자 수동 업로드 기록 저장", type="positive")
                                refresh_packages()
                            ui.button("업로드 완료 표시", on_click=mark_uploaded).props("dense")
                        ui.button("보관", on_click=lambda pid=row["package_id"]: (mark_package(pid, "ARCHIVED", "Archived in UI V2"), refresh_packages())).props("flat dense")
        def generate_package():
            try:
                selected_statuses = status.value or ["PRIMARY"]
                if "ALL" in selected_statuses:
                    selected_statuses = ["PRIMARY"]
                result = create_spark_package(self.current_store, selected_statuses,
                                              int(limit.value), self.package_selected_asins or None)
                compatibility = "PASS" if result.observed_spark_schema_compatible else "FAIL"
                package_validation_status.set_text(
                    f"Local JSON validation: {result.validation_status} · "
                    f"Observed Spark schema compatibility: {compatibility} · Actual Spark load: NOT VERIFIED"
                )
                self.package_selected_asins = []
                self.package_selected_statuses = []
                ui.notify(f"{result.package_id} · {result.product_count}개 · {result.validation_status}", type="positive")
                refresh_packages()
            except Exception as exc:
                ui.notify(_safe_error(exc), type="negative")
        def make_package():
            if int(limit.value) > 100:
                with ui.dialog() as confirm_dialog, ui.card():
                    ui.label("Browser Capture Spark Center portal round-trip is not verified. Continue with a package over 100 products?")
                    with ui.row():
                        ui.button("Cancel", on_click=confirm_dialog.close).props("flat")
                        def confirm_generate_package():
                            generate_package()
                            confirm_dialog.close()
                        ui.button("Continue", on_click=confirm_generate_package).props("color=warning")
                confirm_dialog.open()
                return
            generate_package()
        with ui.row():
            ui.button("Spark Center 폴더 만들기", on_click=make_package, icon="create_new_folder")
            ui.button("목록 새로고침", on_click=refresh_packages, icon="refresh").props("outline")
        refresh_packages()

    def _open_folder(self, path):
        try:
            open_package(path)
        except Exception:
            self.ui.notify(f"로컬 폴더 경로: {path}", type="info")

    def _history(self):
        ui = self.ui
        self._heading("작업 기록", "소싱 실행과 가져오기 오류를 확인합니다.")
        ui.label("Sourcing runs").classes("text-lg font-semibold")
        self._render_runs(list_sourcing_runs(self.current_store, 100))
        ui.label("Import errors").classes("text-lg font-semibold")
        errors = list_recent_errors(100)
        if errors:
            ui.table(columns=[{"name": key, "label": key, "field": key, "sortable": True} for key in
                              ("created_at", "job_id", "source_file", "error_code", "error_message")],
                     rows=errors, pagination=20).classes("w-full")
        else:
            ui.label("가져오기 오류가 없습니다.").classes("text-slate-500")

    def _browser_capture_panel(self):
        ui = self.ui
        service = CaptureService()
        profile = get_store(self.current_store)
        recipes = profile.get("sourcing", {}).get("recipes", [])
        keywords = [row.get("keyword", "") if isinstance(row, dict) else str(row) for row in recipes]
        if keywords:
            selected_keyword = ui.select(keywords, value=keywords[0], label="Amazon 검색 주제어").classes("w-72")
            def open_search():
                from urllib.parse import quote_plus
                url = "https://www.amazon.com/s?k=" + quote_plus(selected_keyword.value or "")
                ui.run_javascript("window.open(" + json.dumps(url) + ", '_blank', 'noopener')")
            ui.button("Amazon 검색 열기", on_click=open_search, icon="open_in_new")
        rows = service.list_candidates(self.current_store, 200)
        batch_service = BatchSourcingService()
        if keywords:
            target_count = ui.number("Batch target candidates", value=5, min=1, max=10000).classes("w-48")
            auto_import = ui.checkbox("DETAIL_COMPLETE 후 MASTER 자동 반영", value=True)
            batch_status_label = ui.label("Batch: IDLE").classes("font-medium")
            batch_progress = ui.linear_progress(value=0, show_value=False).classes("w-full")
            batch_details = ui.label("").classes("text-sm text-slate-600")
            candidate_counts_label = ui.label("").classes("text-sm font-medium")
            batch_item_counts_label = ui.label("").classes("text-sm font-medium")
            pipeline_label = ui.label("").classes("text-sm text-slate-600")
            active_batch = {"run_id": None}
            continue_button = {"button": None}
            recovery_button = {"button": None}
            handshake_label = ui.label("").classes("text-xs text-amber-800")
            active_runs = batch_service.active(self.current_store)
            if active_runs: active_batch["run_id"] = active_runs[0]["run_id"]

            def render_capture_summary():
                summary = batch_service.pipeline_summary(self.current_store)
                candidate_counts_label.set_text(
                    f"수집된 후보 {summary['candidates']} · MASTER {summary['master_count']} · 분류 완료 {summary['classified_count']}"
                )
                total = int(summary["candidates"])
                run_id = active_batch.get("run_id")
                if run_id:
                    run = batch_service.get(run_id)
                    batch_item_counts_label.set_text(f"현재 Batch 상품 {run['item_count']} · Batch 상세 대기 {run['detail_pending']} · 상태 {run['status']}")
                    if run["status"] == "RUNNING" and run["item_count"] == 0 and summary["needs_detail"]:
                        candidate_counts_label.set_text(f"수집된 후보 {total} · MASTER {summary['master_count']} · 기존 후보 {summary['needs_detail']}개가 현재 Batch에 연결되지 않았습니다")
                else:
                    batch_item_counts_label.set_text("현재 Batch 상품 0")
                pipeline_label.set_text(
                    f"Spark Center 실전 준비 — 후보 수집 {'완료' if total else '대기'} · "
                    f"상품 상세 {summary['detail_complete']}/{total} · MASTER 반영 {summary['master_count']}/{total} · "
                    f"Store 분류 {summary['classified_count']}/{total} · 실패 {summary['failed_count']} · Spark Package {summary['package_count']}개 · "
                    "실제 Spark Center 업로드: 사용자 확인 필요"
                )

            def render_batch(result):
                failure_hint = f" · 실패 {result['failed_count']}개 — 실패만 재시도 가능" if result["status"] == "DONE_WITH_ERRORS" else ""
                batch_status_label.set_text(f"Batch {result['run_id']} · {result['status']}{failure_hint}")
                if continue_button["button"]:
                    is_running = result["status"] == "RUNNING"
                    continue_button["button"].set_text("다음 처리" if is_running else "계속")
                    continue_button["button"].set_enabled(result["status"] not in {"DONE", "DONE_WITH_ERRORS", "CANCELLED"})
                opened = result.get("opened_item")
                if recovery_button["button"]:
                    recovery_button["button"].set_enabled(bool(opened) and result["status"] == "RUNNING")
                if opened:
                    age = int(result.get("opened_age_seconds", 0))
                    text = "Amazon 상세 탭은 열렸지만 ShopSource 상세 응답을 기다리는 중입니다."
                    if age >= 15:
                        text += " 상세 캡처 응답이 없습니다. '현재 상품 다시 시도'를 누를 수 있습니다."
                    handshake_label.set_text(text)
                else:
                    handshake_label.set_text("")
                target = max(1, int(result["target_candidates"]))
                completed = result["master_imported"] + result["detail_complete"]
                batch_progress.value = min(1, completed / target)
                batch_details.set_text(f"목표 {target} · 후보 {result['total_seen']} · 중복 {result['deduped']} · 선필터 제외 {result['prefiltered']} · 상세 대기 {result['detail_pending']} · 상세 완료 {result['detail_complete']} · MASTER {result['master_imported']} · PRIMARY {result['primary_count']} · RESERVE {result['reserve_count']} · REVIEW {result['review_count']} · RESTRICTED {result['restricted_count']} · 실패 {result['failed_count']}" + (f" · {result['error']}" if result.get("error") else ""))
                failures = result.get("failed_items", [])
                batch_failures_label.set_text("최근 실패: " + (" | ".join(f"{item['asin']} · {item['state']} · 재시도 {item['retry_count']} · {item['last_error']}" for item in failures) if failures else "없음"))
                batch_events_label.set_text("최근 이벤트: " + (" | ".join(f"{event['created_at']} {event['event_type']}" for event in result.get("events", [])[:10]) if result.get("events") else "없음"))

                render_capture_summary()

            def kick_batch(run_id):
                run = batch_service.get(run_id)
                if run["status"] != "RUNNING":
                    return {"state": "NOT_RUNNING", "run": run}
                if run.get("opened_count", 0):
                    return {"state": "IN_PROGRESS", "run": run}
                if not run.get("detail_pending", 0):
                    return {"state": "NO_PENDING", "run": run}
                # The localhost extension content bridge relays this user action to
                # the MV3 worker, which reserves NEXT_ITEM and creates/tracks the tab.
                message = {"source": "shopsource-studio-ui", "type": "batch-open-next", "runId": run_id}
                ui.run_javascript("window.postMessage(" + json.dumps(message) + ", window.location.origin)")
                return {"state": "REQUESTED", "run": run}

            def continue_batch():
                run_id = active_batch.get("run_id")
                if not run_id:
                    return
                try:
                    run = batch_service.get(run_id)
                    if run["status"] == "RUNNING":
                        outcome = kick_batch(run_id)
                    else:
                        run = batch_service.action(run_id, "RESUME")
                        outcome = kick_batch(run_id)
                    render_batch(batch_service.get(run_id))
                    if outcome["state"] == "IN_PROGRESS":
                        ui.notify("현재 상품 상세 처리가 진행 중입니다.", type="info")
                    elif outcome["state"] == "REQUESTED":
                        ui.notify("브라우저 확장에 다음 상품 상세 열기를 요청했습니다.", type="positive")
                    elif outcome["state"] == "WAITING":
                        ui.notify("다음 상품 처리 간격을 기다리고 있습니다. 잠시 후 '다음 처리'를 눌러주세요.", type="info")
                    elif outcome["state"] == "NO_PENDING":
                        ui.notify("현재 처리할 미완료 상품이 없습니다.", type="info")
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")

            def recover_open_item():
                run_id = active_batch.get("run_id")
                if not run_id:
                    return
                try:
                    result = batch_service.recover_open_item(run_id)
                    render_batch(result)
                    outcome = kick_batch(run_id)
                    if outcome["state"] == "OPENED":
                        ui.notify("현재 열린 상품을 다시 예약하고 상세 탭을 열었습니다.", type="positive")
                    elif outcome["state"] == "REQUESTED":
                        ui.notify("현재 상품을 다시 Queue에 넣고 브라우저 확장에 재시도를 요청했습니다.", type="positive")
                    else:
                        ui.notify("현재 상품 다시 시도를 시작하지 못했습니다. Batch 상태를 확인하세요.", type="warning")
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")

            def poll_batch():
                run_id = active_batch.get("run_id")
                if run_id:
                    try: render_batch(batch_service.get(run_id))
                    except Exception: pass
                else:
                    try: render_capture_summary()
                    except Exception: pass

            def worker_action(action, force=False):
                run_id = active_batch.get("run_id")
                if not run_id:
                    ui.notify("활성 Batch가 없습니다.", type="warning")
                    return
                message = {"source": "shopsource-studio-ui", "type": f"worker-{action}", "runId": run_id, "force": force}
                ui.run_javascript("window.postMessage(" + json.dumps(message) + ", window.location.origin)")
                if action == "show":
                    ui.notify("ShopSource Amazon 작업 탭 보기를 요청했습니다.", type="info")
                else:
                    ui.notify("캡처 진행 중이면 작업 탭을 닫지 않습니다.", type="info")

            def start_batch():
                try:
                    run = batch_service.ensure_active_batch(self.current_store, selected_keyword.value or "", int(target_count.value), bool(auto_import.value))
                    active_batch["run_id"] = run["run_id"]
                    if run["status"] in {"PENDING", "PAUSED", "PAUSED_NEEDS_USER"} and "confirmation" not in run.get("error", "").lower():
                        batch_service.action(run["run_id"], "RESUME")
                    from urllib.parse import quote_plus
                    url = "https://www.amazon.com/s?k=" + quote_plus(selected_keyword.value or "")
                    ui.run_javascript("window.open(" + json.dumps(url) + ", '_blank', 'noopener')")
                    ui.notify("Batch를 시작했습니다. 검색 페이지에서 현재 페이지 후보 가져오기를 누르세요.", type="positive")
                    poll_batch()
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            def process_unfinished():
                try:
                    summary = batch_service.pipeline_summary(self.current_store)
                    if not summary["candidates"]:
                        run = batch_service.ensure_active_batch(self.current_store, selected_keyword.value or "", int(target_count.value), bool(auto_import.value))
                        active_batch["run_id"] = run["run_id"]
                        if run["status"] in {"PENDING", "PAUSED", "PAUSED_NEEDS_USER"} and "confirmation" not in run.get("error", "").lower():
                            run = batch_service.action(run["run_id"], "RESUME")
                        from urllib.parse import quote_plus
                        url = "https://www.amazon.com/s?k=" + quote_plus(selected_keyword.value or "")
                        ui.run_javascript("window.open(" + json.dumps(url) + ", '_blank', 'noopener')")
                        ui.notify("기존 후보가 없어 Amazon 검색을 열었습니다. 검색 결과에서 '현재 검색결과 전체 가져오기'를 눌러주세요.", type="info")
                        render_batch(run)
                        return
                    result = batch_service.process_existing_candidates(
                        self.current_store, selected_keyword.value or "", int(target_count.value), bool(auto_import.value)
                    )
                    active_batch["run_id"] = result["run_id"]
                    render_batch(result)
                    if result.get("queued"):
                        ui.notify(f"미완료 {result['queued']}개를 Batch에 연결하고 자동 처리를 시작합니다.", type="positive")
                        outcome = kick_batch(result["run_id"])
                        if outcome["state"] == "IN_PROGRESS":
                            ui.notify("현재 상품 상세 처리가 진행 중입니다.", type="info")
                        elif outcome["state"] == "REQUESTED":
                            ui.notify("이미 Queue에 있는 미완료 상품 처리를 브라우저 확장에 요청했습니다.", type="positive")
                    elif result.get("detail_pending", 0) > 0:
                        outcome = kick_batch(result["run_id"])
                        if outcome["state"] == "OPENED":
                            ui.notify(f"이미 Queue에 있는 미완료 {result['detail_pending']}개 처리를 시작합니다.", type="positive")
                        elif outcome["state"] == "REQUESTED":
                            ui.notify(f"이미 Queue에 있는 미완료 {result['detail_pending']}개 처리를 브라우저 확장에 요청했습니다.", type="positive")
                        elif outcome["state"] == "IN_PROGRESS":
                            ui.notify("현재 상품 상세 처리가 진행 중입니다.", type="info")
                        elif outcome["state"] == "WAITING":
                            ui.notify("다음 상품 처리 간격을 기다리고 있습니다. 잠시 후 '다음 처리'를 눌러주세요.", type="info")
                        else:
                            ui.notify("Batch가 실행 중이 아닙니다. 상태를 확인한 뒤 '계속'을 눌러주세요.", type="warning")
                    elif result.get("existing_imported"):
                        ui.notify(f"상세 완료 {result['existing_imported']}개를 MASTER에 반영했습니다. 상세 대기 상품은 없습니다.", type="positive")
                    else:
                        ui.notify("현재 처리할 미완료 상품이 없습니다.", type="info")
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")

            def batch_action(action):
                run_id = active_batch.get("run_id")
                if not run_id: return
                try:
                    if action == "RESUME" and batch_service.get(run_id)["status"] == "RUNNING":
                        outcome = kick_batch(run_id)
                        render_batch(batch_service.get(run_id))
                        if outcome["state"] == "IN_PROGRESS":
                            ui.notify("현재 상품 상세 처리가 진행 중입니다.", type="info")
                        elif outcome["state"] == "REQUESTED":
                            ui.notify("브라우저 확장에 다음 상품 상세 열기를 요청했습니다.", type="positive")
                        elif outcome["state"] == "NO_PENDING":
                            ui.notify("현재 처리할 미완료 상품이 없습니다.", type="info")
                        return
                    result = batch_service.action(run_id, action)
                    render_batch(result)
                    if action == "CANCEL":
                        worker_action("close", force=True)
                    if action in {"RESUME", "RETRY"}:
                        outcome = kick_batch(run_id)
                        if outcome["state"] == "IN_PROGRESS":
                            ui.notify("현재 상품 상세 처리가 진행 중입니다.", type="info")
                        elif outcome["state"] == "REQUESTED":
                            ui.notify("브라우저 확장에 다음 상품 상세 열기를 요청했습니다.", type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            def queue_existing():
                pending = [r for r in rows if r["capture_status"] == "NEEDS_DETAIL"]
                if not pending:
                    ui.notify("상세 보강이 필요한 후보가 없습니다.", type="warning"); return
                try:
                    run = batch_service.ensure_active_batch(self.current_store, pending[0].get("keyword") or selected_keyword.value or "existing candidates", int(target_count.value), bool(auto_import.value))
                    active_batch["run_id"] = run["run_id"]
                    if run["status"] in {"PENDING", "PAUSED", "PAUSED_NEEDS_USER"} and "confirmation" not in run.get("error", "").lower():
                        batch_service.action(run["run_id"], "RESUME")
                    result = batch_service.queue_existing_candidates(run["run_id"], [r["asin"] for r in pending])
                    render_batch(result)
                    if result.get("queued"):
                        ui.notify(f"미완료 {result['queued']}개를 현재 Batch에 연결하고 처리를 시작합니다.", type="positive")
                    elif result.get("detail_pending", 0) > 0:
                        ui.notify("이미 Queue에 있는 상품 처리를 시작합니다.", type="info")
                    outcome = kick_batch(run["run_id"])
                    if outcome["state"] == "IN_PROGRESS":
                        ui.notify("현재 상품 상세 처리가 진행 중입니다.", type="info")
                    elif outcome["state"] == "REQUESTED":
                        ui.notify("브라우저 확장에 Queue 처리를 요청했습니다.", type="positive")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            with ui.row():
                ui.button("미완료 상품 자동 처리", on_click=process_unfinished, icon="auto_awesome").props("color=primary")
                ui.button("고급: Amazon 검색 Batch 시작", on_click=start_batch, icon="playlist_add").props("outline")
                ui.button("고급: NEEDS_DETAIL 전체 Queue", on_click=queue_existing, icon="queue_play_next").props("outline")
                ui.button("일시정지", on_click=lambda: batch_action("PAUSE"), icon="pause").props("outline")
                continue_button["button"] = ui.button("계속", on_click=continue_batch, icon="play_arrow").props("outline")
                recovery_button["button"] = ui.button("현재 상품 다시 시도", on_click=recover_open_item, icon="refresh").props("outline")
                recovery_button["button"].set_enabled(False)
                ui.button("작업 탭 보기", on_click=lambda: worker_action("show"), icon="visibility").props("outline")
                ui.button("작업 탭 정리", on_click=lambda: worker_action("close"), icon="close").props("outline")
                ui.button("취소", on_click=lambda: batch_action("CANCEL"), icon="stop").props("outline color=negative")
                ui.button("실패만 재시도", on_click=lambda: batch_action("RETRY"), icon="replay").props("outline")
            ui.label("상세 탭은 한 번에 하나씩, 최소 4초 간격으로 엽니다. 검색 페이지 이동은 사용자가 직접 합니다.").classes("text-xs text-amber-800")
            ui.timer(2.0, poll_batch)
            ui.label("상세 완료 상품은 MASTER에 반영하고, 상세 필요 상품은 같은 Batch에 자동 연결합니다.").classes("text-xs text-slate-600")
            ui.label("Amazon 작업 탭: Batch당 최대 1개를 백그라운드에서 재사용합니다.").classes("text-xs text-amber-800")
            ui.label("Phase 2.6.5 적용 후 chrome://extensions에서 ShopSource Capture v0.1.4를 새로고침하고, ShopSource 화면도 한 번 새로고침하세요.").classes("text-xs text-slate-500")
            with ui.expansion("최근 실패 및 Batch 이벤트", icon="bug_report").classes("w-full"):
                batch_failures_label = ui.label("최근 실패: 없음").classes("text-xs text-red-700")
                batch_events_label = ui.label("최근 이벤트: 없음").classes("text-xs text-slate-600")
        summary = BatchSourcingService().pipeline_summary(self.current_store)
        ui.label(f"수집된 후보 {summary['candidates']} · 상품 상세 {summary['detail_complete']}/{summary['candidates']} · 실패 {summary['failed_count']} · 상세 대기 {summary['needs_detail']} · MASTER 반영 {summary['master_count']}/{summary['candidates']}")
        ui.label("Spark Center 실전 준비: Amazon 후보 수집 → 상세 보강 → MASTER → Store 분류 → Package → 실제 업로드(사용자 확인 필요)").classes("text-sm text-slate-600")
        checks = {}
        if rows:
            for row in rows[:50]:
                payload = row.get("detail_payload") or row.get("search_payload") or {}
                with ui.row().classes("w-full items-center border-b py-1"):
                    checks[row["asin"]] = ui.checkbox()
                    ui.label(f"{row['asin']} · {payload.get('title') or ''} · {row['completeness_score']}% · {row['capture_status']}").classes("flex-1")
                    ui.button("상세 열기", on_click=lambda url=payload.get("url"): ui.run_javascript("window.open(" + json.dumps(url or "") + ", '_blank', 'noopener')"), icon="open_in_new").props("flat dense")
        async def refresh_capture():
            ui.navigate.to("/sourcing")
        def create_pairing():
            code = service.create_pairing_code()
            ui.notify("Pairing code (확장 Options에 복사): " + code, type="positive", timeout=30000)
        def import_ready():
            selected = [asin for asin, check in checks.items() if check.value]
            if not selected:
                ui.notify("MASTER로 보낼 상세 완료 상품을 먼저 선택하세요.", type="warning"); return
            complete = [row["asin"] for row in rows if row["capture_status"] == "DETAIL_COMPLETE" and row["asin"] in selected]
            if not complete:
                ui.notify("MASTER로 보낼 DETAIL_COMPLETE 상품이 없습니다.", type="warning"); return
            try:
                result = service.import_candidates(self.current_store, complete[:100])
                ui.notify(f"MASTER 반영 후 Store classify 완료: {result['inserted']} 신규, {result['updated']} 갱신", type="positive")
                ui.navigate.to("/products")
            except Exception as exc:
                ui.notify(_safe_error(exc), type="negative")
        with ui.row():
            ui.button("확장 연결 확인 방법", on_click=lambda: ui.notify("Settings에서 Pairing code를 생성하고 확장 popup의 연결 상태를 확인하세요."), icon="link").props("outline")
            ui.button("Pairing code 만들기", on_click=create_pairing, icon="vpn_key").props("outline")
            ui.button("후보 새로고침", on_click=refresh_capture, icon="refresh").props("outline")
            ui.button("상세 완료 상품 MASTER로 보내기", on_click=import_ready, icon="move_to_inbox")
            def open_selected_detail():
                urls = []
                for row in rows:
                    if row["asin"] in checks and checks[row["asin"]].value:
                        url = (row.get("detail_payload") or row.get("search_payload") or {}).get("url")
                        if url: urls.append(url)
                        if len(urls) >= 5: break
                if not urls:
                    ui.notify("상세 보강할 후보를 선택하세요.", type="warning"); return
                script = "(urls => urls.forEach((url, i) => setTimeout(() => window.open(url, '_blank', 'noopener'), i * 400)))"
                ui.run_javascript(script + "(" + json.dumps(urls) + ")")
            ui.button("선택 상세 보강 (최대 5)", on_click=open_selected_detail, icon="open_in_new").props("outline")
        ui.label("상세 보강은 사용자가 상품 페이지에서 확장 버튼을 누릅니다. 자동 페이지 순회 및 CAPTCHA 우회는 하지 않습니다.").classes("text-xs text-amber-800")

    def _settings(self):
        ui = self.ui
        from ..automation import WorkflowAutomationService
        self._heading("설정", "Keepa 인증과 UI 기본값을 관리합니다.")
        automation=_workflow_service(); auto_values=automation.settings(self.current_store)
        with ui.card().classes("w-full rounded-xl border border-blue-200 p-5"):
            ui.label("Automation Settings").classes("ss-card-title")
            auto_mode=ui.switch("자동 모드",value=bool(auto_values["auto_mode"]))
            auto_retry=ui.switch("자동 재시도",value=bool(auto_values["auto_retry"]))
            max_retries=ui.number("최대 재시도",value=auto_values["max_retries"],min=1,max=10).classes("w-48")
            auto_continue=ui.switch("작업 종료 후 다음 안전 단계 자동 진행",value=bool(auto_values["auto_continue"]))
            human_only=ui.switch("사용자 확인이 필요한 단계에서만 멈춤",value=bool(auto_values["human_gates_only"]))
            restart_notice=ui.switch("앱 재시작 시 중단 작업 안내",value=bool(auto_values["restart_notice"]))
            provider_limit=ui.number("Provider token limit",value=auto_values.get("provider_token_limit"),min=0).classes("w-56")
            def save_automation():
                automation.save_settings(self.current_store,auto_mode=auto_mode.value,auto_retry=auto_retry.value,max_retries=max_retries.value,
                    auto_continue=auto_continue.value,human_gates_only=human_only.value,restart_notice=restart_notice.value,provider_token_limit=provider_limit.value)
                ui.notify("자동화 설정을 저장했습니다.",type="positive")
            ui.button("자동화 설정 저장",on_click=save_automation,icon="save").props("color=primary")
        ui.label("Browser Capture · 확장 연결").classes("text-lg font-semibold")
        pairing_status = ui.label("Pairing code를 생성한 뒤 확장 Options에 입력하세요.")
        def make_capture_pairing():
            pairing_status.set_text("Pairing code (로컬 전용): " + CaptureService().create_pairing_code())
        ui.button("브라우저 캡처 연결 코드 만들기", on_click=make_capture_pairing, icon="link")
        extension_dir = __import__("pathlib").Path(__file__).resolve().parents[3] / "browser_extension" / "shopsource_capture"
        ui.button("확장 폴더 열기", on_click=lambda: self._open_folder(str(extension_dir)), icon="folder_open").props("outline")
        ui.label("Edge: edge://extensions · Chrome: chrome://extensions → 개발자 모드 → 압축해제된 확장 로드 → browser_extension/shopsource_capture")
        _, key_source = get_api_key(self.session_api_key)
        key_status = ui.label(f"Keepa API key 상태: {key_source}")
        key_input = ui.input("Keepa API Key").props("type=password autocomplete=new-password").classes("w-96")
        async def save_key():
            if not key_input.value:
                ui.notify("API key를 입력하세요.", type="warning"); return
            try:
                save_api_key(key_input.value)
                self.session_api_key = key_input.value
                key_input.value = ""
                key_status.set_text("Keepa API key 상태: Windows Credential Manager")
                ui.notify("Windows 자격 증명에 저장했습니다.", type="positive")
            except Exception as exc:
                ui.notify(_safe_error(exc), type="negative")
        async def test_key():
            key, source = get_api_key(self.session_api_key)
            try:
                result = await asyncio.to_thread(KeepaProvider(api_key=key).health)
                ui.notify(f"Keepa 연결 성공 · tokensLeft {result.get('tokensLeft')} · source {source}", type="positive")
            except Exception as exc:
                ui.notify(_safe_error(exc), type="negative")
        def remove_key():
            try:
                delete_api_key(); self.session_api_key = None
                key_status.set_text("Keepa API key 상태: " + get_api_key()[1])
                ui.notify("저장된 Keepa key를 삭제했습니다.")
            except Exception as exc:
                ui.notify(_safe_error(exc), type="negative")
        with ui.row():
            ui.button("저장", on_click=save_key, icon="save")
            ui.button("연결 테스트", on_click=test_key, icon="wifi_tethering").props("outline")
            ui.button("삭제", on_click=remove_key, icon="delete").props("flat color=negative")
        ui.separator()
        defaults = ui.number("기본 Target", value=int(get_app_setting("default_target", 5)), min=1, max=500).classes("w-48")
        budget = ui.number("기본 token budget", value=int(get_app_setting("default_token_budget", 1000)), min=1).classes("w-48")
        theme = ui.select(["Light", "Dark"], value=get_app_setting("theme", "Light"), label="UI theme").classes("w-48")
        log_level = ui.select(["DEBUG", "INFO", "WARNING", "ERROR"],
                              value=get_app_setting("log_level", "INFO"), label="Log level").classes("w-48")
        def save_preferences():
            try:
                set_app_settings({"default_target": int(defaults.value), "default_token_budget": int(budget.value),
                                  "theme": theme.value, "log_level": log_level.value})
                logging.getLogger().setLevel(getattr(logging, log_level.value))
                ui.notify("기본 설정을 저장했습니다. 테마는 다음 화면 이동부터 적용됩니다.", type="positive")
            except Exception as exc:
                ui.notify(_safe_error(exc), type="negative")
        ui.button("기본 설정 저장", on_click=save_preferences, icon="save").props("outline")

    def _store_wizard(self):
        ui = self.ui
        dialog = ui.dialog()
        with dialog, ui.card().classes("w-[720px] max-w-[95vw]"):
            ui.label("새 Store 만들기").classes("text-xl font-bold")
            step_label = ui.label("Step 1 / 6 · 기본 정보").classes("text-slate-500")
            fields = {
                "store_id": ui.input("Store ID"),
                "store_name": ui.input("Store 이름"),
                "category": ui.input("카테고리", value="general"),
                "concept": ui.textarea("판매 콘셉트"),
                "price_min": ui.number("PRIMARY 최저 가격", value=30, min=0),
                "price_max": ui.number("PRIMARY 최고 가격", value=100, min=1),
                "include": ui.input("Include keywords", placeholder="쉼표로 구분"),
                "exclude": ui.input("Exclude keywords", placeholder="쉼표로 구분"),
                "risk": ui.input("위험 범주 keyword", placeholder="battery, chemical, safety"),
            }
            sections = [
                ["store_id", "store_name", "category", "concept"],
                ["price_min", "price_max"], ["risk", "exclude"], ["include"],
                ["include", "price_min", "price_max"], ["store_id", "store_name"],
            ]
            for index, key in enumerate(fields):
                fields[key].visible = key in sections[0]
            wizard_summary = ui.label("").classes("text-sm text-slate-600")
            wizard_summary.visible = False
            wizard_keyword_choices = {"items": []}
            wizard_keyword_box = ui.column().classes("w-full max-h-48 overflow-auto")
            wizard_keyword_box.visible = False
            def create_wizard_keywords():
                profile_for_keywords = KeywordEngine().store_wizard_profile(
                    store_id=fields["store_id"].value or "new",
                    store_name=fields["store_name"].value or "Store",
                    category=fields["category"].value or "general",
                    concept=fields["concept"].value or "",
                    price_min=float(fields["price_min"].value or 30),
                    price_max=float(fields["price_max"].value or 100),
                    include_keywords=[x.strip() for x in (fields["include"].value or "").split(",") if x.strip()],
                    exclude_keywords=[x.strip() for x in (fields["exclude"].value or "").split(",") if x.strip()],
                )
                choices = KeywordEngine()._expand_seeds(profile_for_keywords)[:15]
                wizard_keyword_box.clear()
                wizard_keyword_choices["items"].clear()
                with wizard_keyword_box:
                    for keyword in choices:
                        checkbox = ui.checkbox(keyword, value=True)
                        wizard_keyword_choices["items"].append((keyword, checkbox))
                wizard_keyword_box.set_visibility(True)
            step = {"n": 0}
            def change_step(direction):
                step["n"] = min(5, max(0, step["n"] + direction))
                step_label.set_text(f"Step {step['n'] + 1} / 6 · " + ["기본 정보", "목표 가격대", "위험/제외 범주", "추천 keyword", "Recipe preview", "저장"][step["n"]])
                visible = set(sections[step["n"]])
                for key, element in fields.items(): element.set_visibility(key in visible)
                wizard_summary.set_visibility(step["n"] in {3, 4})
                wizard_keyword_box.set_visibility(step["n"] == 3)
                if step["n"] == 3:
                    seeds = KeywordEngine()._expand_seeds({"store_id": fields["store_id"].value or "new",
                        "store_name": fields["store_name"].value or "Store", "category": fields["category"].value or "general",
                        "include_keywords": [x.strip() for x in (fields["include"].value or "").split(",") if x.strip()]})
                    wizard_summary.set_text("추천 주제어를 만들고 원하는 항목을 선택하세요.")
                    if not wizard_keyword_choices["items"]:
                        create_wizard_keywords()
                if step["n"] == 4:
                    wizard_summary.set_text(f"Keepa US · ${fields['price_min'].value}–${fields['price_max'].value} · target 5 · rating 4.0 · reviews 30")
            def save_profile():
                try:
                    include = [x.strip() for x in (fields["include"].value or "").split(",") if x.strip()]
                    exclude = [x.strip() for x in (fields["exclude"].value or "").split(",") if x.strip()]
                    terms = [x.strip() for x in (fields["risk"].value or "").split(",") if x.strip()]
                    rules = [{"code": term.lower().replace(" ", "_"), "status": "REVIEW", "terms": [term]} for term in terms]
                    profile = KeywordEngine().store_wizard_profile(
                        store_id=fields["store_id"].value, store_name=fields["store_name"].value,
                        category=fields["category"].value, concept=fields["concept"].value or "",
                        price_min=float(fields["price_min"].value), price_max=float(fields["price_max"].value),
                        include_keywords=include, exclude_keywords=exclude, risk_rules=rules,
                    )
                    profile["sourcing"]["recipes"] = [
                        {"keyword": keyword} for keyword, checkbox in wizard_keyword_choices["items"]
                        if checkbox.value
                    ]
                    path = create_store_profile(profile)
                    self.stores = list_stores()
                    self.current_store = profile["store_id"]
                    ui.notify(f"Store Profile 저장: {path}", type="positive")
                    dialog.close()
                    ui.navigate.to("/stores")
                except Exception as exc:
                    ui.notify(_safe_error(exc), type="negative")
            with ui.row().classes("justify-between w-full"):
                ui.button("이전", on_click=lambda: change_step(-1)).props("outline")
                ui.button("다음", on_click=lambda: change_step(1))
                ui.button("Store Profile 저장", on_click=save_profile, icon="save")
                ui.button("취소", on_click=dialog.close).props("flat")
        dialog.open()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ShopSource Studio local operator UI V2")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (loopback by default)")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Capture bridge는 보안상 loopback 주소에만 bind할 수 있습니다.")
    try:
        from nicegui import app, ui
    except ImportError as exc:
        raise RuntimeError('UI V2 needs the optional dependency: pip install -e ".[ui]"') from exc
    from fastapi.middleware.cors import CORSMiddleware
    from ..capture.bridge import install_capture_routes
    image_root = EXPORT_DIR / "collection_images"
    image_root.mkdir(parents=True, exist_ok=True)
    app.add_static_files("/collection_images", str(image_root))
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"chrome-extension://[a-p]{32}",
                       allow_methods=["GET", "POST", "OPTIONS"],
                       allow_headers=["Content-Type", "X-ShopSource-Pairing"])
    install_capture_routes(app)
    OperatorUI(ui)
    ui.run(title="ShopSource Studio", host=args.host, port=args.port, reload=False, show=not args.no_browser)


if __name__ == "__main__":
    main()
