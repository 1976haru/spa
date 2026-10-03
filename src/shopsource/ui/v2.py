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
from ..shopify_collections import ShopifyCollectionPublisher, get_connection as get_shopify_connection, save_connection as save_shopify_connection, save_shopify_token
from ..collection_images import ManualImageProvider, OpenAIImagesProvider, generate_collection_image
from ..classifier import classify_store
from ..db import connect, get_store, init_db, upsert_store
from ..homepage_collections import HomepageCollectionService, ShopifyThemeReader, build_homepage_plan
from ..store_build import StoreBuildOrchestrator
from ..intelligence.keyword_engine import KeywordEngine
from ..paths import AMAZON_INBOX_DIR, EXPORT_DIR, STORE_DIR
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

NAV_ITEMS = [
    ("/build", "rocket_launch", "스토어 자동 구축"),
    ("/pilot", "science", "Cabin Tidy 파일럿"),
    ("/", "dashboard", "대시보드"),
    ("/stores", "storefront", "스토어"),
    ("/sourcing", "travel_explore", "소싱"),
    ("/collections", "collections_bookmark", "컬렉션 자동화"),
    ("/products", "inventory_2", "상품"),
    ("/packages", "outbox", "패키지"),
    ("/history", "history", "기록"),
    ("/settings", "settings", "설정"),
]
STATUS_OPTIONS = ["ALL", "PRIMARY", "RESERVE_A", "RESERVE_B", "RESERVE_C", "LOW_RESERVE",
                  "HIGH_RESERVE", "REVIEW", "RESTRICTED", "ARCHIVED"]
PRODUCT_SOURCE_OPTIONS = ["ALL", "BROWSER_CAPTURE", "SPARK_STORAGE", "AMAZON_SOURCE_FOLDER", "KEEPA"]


def _safe_error(exc: Exception) -> str:
    return str(exc)


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
        logging.getLogger().setLevel(getattr(logging, str(get_app_setting("log_level", "INFO")).upper(), logging.INFO))
        if get_app_setting("theme", "Light") == "Dark":
            ui.dark_mode().enable()
        else:
            ui.dark_mode().disable()
        ui.colors(primary="#2563eb", secondary="#475569", accent="#14b8a6",
                  positive="#16a34a", negative="#dc2626", warning="#d97706")
        ui.query("body").classes("bg-slate-50")
        with ui.left_drawer(value=True).classes("bg-slate-950 text-white w-64"):
            ui.label("ShopSource Studio").classes("text-xl font-bold px-3 py-5")
            ui.separator().classes("bg-slate-700")
            for href, icon, label in NAV_ITEMS:
                ui.link(label, href).classes(
                    "w-full rounded-lg px-4 py-3 text-slate-200 hover:bg-slate-800"
                    + (" bg-slate-800" if path == href else "")
                ).props(f"icon={icon}")
        with ui.header().classes("bg-white text-slate-900 border-b border-slate-200 items-center"):
            ui.label(title).classes("text-lg font-semibold")
            ui.space()
            options = {store["store_id"]: f"{store['store_id']} | {store['store_name']}" for store in self.stores}
            ui.select(options, value=self.current_store, label="Store").classes("w-64").on_value_change(
                lambda event: self._set_store(event.value, path)
            )
        with ui.column().classes("w-full max-w-screen-2xl mx-auto p-6 gap-5"):
            if path == "/": self._dashboard()
            elif path == "/build": self._store_build()
            elif path == "/pilot": self._shopify_pilot()
            elif path == "/stores": self._stores()
            elif path == "/sourcing": self._sourcing()
            elif path == "/collections": self._collections()
            elif path == "/products": self._products()
            elif path == "/packages": self._packages()
            elif path == "/history": self._history()
            elif path == "/settings": self._settings()

    def _set_store(self, store_id, path):
        self.current_store = store_id
        self.ui.navigate.to(path)

    def _store_build(self):
        ui = self.ui
        self._heading("스토어 자동 구축", "소싱부터 Shopify 상품·컬렉션·홈페이지 계획까지 단계별로 이어갑니다. 실제 실행 전 미리보기와 확인이 필요합니다.")
        ui.label("실제 소싱·Shopify 쓰기·유료 이미지 생성은 [실제 실행]을 선택한 뒤 시작할 때만 가능합니다. Theme 적용 및 Spark 업로드는 수동 확인 단계입니다.").classes("text-sm text-amber-800")
        store = next((row for row in self.stores if row["store_id"] == self.current_store), None)
        ui.label(f"Store: {self.current_store} | {store['store_name'] if store else self.current_store}").classes("text-xl font-semibold")
        with ui.card().classes("w-full border border-sky-200"):
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
                target = ui.number("소싱 목표", value=2000, min=1, max=10000).classes("w-48")
                product_status = ui.select({"DRAFT": "DRAFT (권장)", "ACTIVE": "ACTIVE"}, value="DRAFT", label="Shopify 상품 상태").classes("w-56")
                product_media = ui.select({"MANUAL_MEDIA": "수동 이미지 (권장)", "SOURCE_MEDIA": "SOURCE_MEDIA", "GENERATED_MEDIA": "GENERATED_MEDIA", "MIXED": "MIXED"}, value="MANUAL_MEDIA", label="상품 미디어 정책").classes("w-72")
                source_rights = ui.checkbox("원본 상품 이미지 재사용 권리 확인", value=False)
                confirmation = ui.select({"source_complete": "Amazon 소싱 worker 완료 확인", "amazon_challenge_resolved": "Amazon 확인 화면을 직접 해결 완료", "spark_upload_confirmed": "SparkShopify 업로드 완료 확인", "theme_manual_apply_confirmed": "Theme patch 수동 적용 완료 확인"}, label="수동 단계 확인", value=None).classes("w-96")
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
                               "source_media_rights_confirmed": bool(source_rights.value)}
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
                try:
                    service = StoreBuildOrchestrator()
                    result = await asyncio.to_thread(service.start, current["run_id"], live_confirmed=True)
                    show_run(result)
                    ui.notify("Store Build 단계가 실행/수동 게이트까지 진행되었습니다.", type="positive" if result["status"] != "FAILED" else "negative")
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

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
                shop_token = ui.input("Admin API access token (저장 후 화면에서 지워짐)").props("type=password autocomplete=new-password").classes("w-96")
                ui.label("Scopes: read_products, write_products, read_publications; homepage theme read: read_themes. Shopify theme write needs write_themes plus Shopify exemption; this phase never performs theme writes.").classes("text-xs text-slate-600")
                def save_shopify_config():
                    try:
                        save_shopify_connection(self.current_store, shop_domain.value or "")
                        if shop_token.value:
                            save_shopify_token(self.current_store, shop_token.value)
                            shop_token.value = ""
                        connection_label.set_text(f"Shopify: {shop_domain.value} · CONFIGURED · API 2026-07")
                        ui.notify("연결 설정을 저장했습니다. token은 OS credential store 외부에 저장되지 않습니다.", type="positive")
                    except Exception as exc: ui.notify(_safe_error(exc), type="negative")
                ui.button("연결 설정/토큰 저장", on_click=save_shopify_config, icon="save")
            connection_detail = ui.label("권한 및 publication 미확인").classes("text-xs text-slate-600")
            def verify_shopify():
                try:
                    result = ShopifyCollectionPublisher().verify(self.current_store)
                    connection_label.set_text("Shopify: CONNECTED" if not result["missing_scopes"] else "Shopify: MISSING SCOPES")
                    pubs = ", ".join(f"{p.get('name')} [{p.get('id')}]" for p in result["online_store_publications"])
                    connection_detail.set_text(f"Missing required: {', '.join(result['missing_scopes']) or 'none'} · Optional: {', '.join(result['missing_optional_scopes']) or 'none'} · Online Store: {pubs or 'not found'}")
                    ui.notify("Shopify 연결/권한을 확인했습니다.", type="positive" if not result["missing_scopes"] else "warning")
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
        self.ui.label(title).classes("text-2xl font-bold text-slate-900")
        if subtitle:
            self.ui.label(subtitle).classes("text-sm text-slate-500")

    def _card(self, title: str, value, detail=""):
        ui = self.ui
        with ui.card().classes("min-w-40 flex-1 rounded-xl border border-slate-200 shadow-sm"):
            ui.label(title).classes("text-sm text-slate-500")
            ui.label(str(value)).classes("text-2xl font-bold text-slate-900")
            if detail:
                ui.label(detail).classes("text-xs text-slate-400")

    def _dashboard(self):
        ui = self.ui
        data = dashboard_data(self.current_store)
        store = next((item for item in self.stores if item["store_id"] == self.current_store), None)
        self._heading("대시보드", f"{self.current_store} | {store['store_name'] if store else ''} 운영 현황")
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
        self._heading("설정", "Keepa 인증과 UI 기본값을 관리합니다.")
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
