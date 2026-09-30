from __future__ import annotations

import asyncio
import json
import logging

from ..connectors.spark_center_package import list_packages, mark_package
from ..capture.service import CaptureService
from ..capture.batch import BatchSourcingService
from ..classifier import classify_store
from ..db import get_store, init_db, upsert_store
from ..intelligence.keyword_engine import KeywordEngine
from ..paths import AMAZON_INBOX_DIR, STORE_DIR
from ..importer import import_amazon_source
from ..sourcing.credentials import delete_api_key, get_api_key, save_api_key
from ..sourcing.engine import SourcingEngine, new_run_id
from ..sourcing.providers.keepa import KeepaProvider
from .v2_service import (
    bulk_override, clear_bulk_override, create_spark_package, create_store_profile,
    dashboard_data, get_app_setting, list_recent_errors, list_sourcing_runs, list_stores, set_app_settings,
    open_package, product_detail, product_page,
)

NAV_ITEMS = [
    ("/", "dashboard", "대시보드"),
    ("/stores", "storefront", "스토어"),
    ("/sourcing", "travel_explore", "소싱"),
    ("/products", "inventory_2", "상품"),
    ("/packages", "outbox", "패키지"),
    ("/history", "history", "기록"),
    ("/settings", "settings", "설정"),
]
STATUS_OPTIONS = ["ALL", "PRIMARY", "RESERVE_A", "RESERVE_B", "RESERVE_C", "LOW_RESERVE",
                  "HIGH_RESERVE", "REVIEW", "RESTRICTED", "ARCHIVED"]


def _safe_error(exc: Exception) -> str:
    return str(exc)


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
            elif path == "/stores": self._stores()
            elif path == "/sourcing": self._sourcing()
            elif path == "/products": self._products()
            elif path == "/packages": self._packages()
            elif path == "/history": self._history()
            elif path == "/settings": self._settings()

    def _set_store(self, store_id, path):
        self.current_store = store_id
        self.ui.navigate.to(path)

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

    def _sourcing(self):
        ui = self.ui
        profile = get_store(self.current_store)
        config = profile.get("sourcing") or {}
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
        source_filter = ui.select(["ALL", "SPARK_STORAGE", "AMAZON_SOURCE_FOLDER", "KEEPA"], value="ALL", label="Source").classes("w-52")
        selected_asins = ui.label("선택 0개")
        grid_container = ui.column().classes("w-full")
        grid_ref = {"grid": None}
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
                grid_container.clear()
                with grid_container:
                    grid_ref["grid"] = ui.aggrid({
                        "columnDefs": [
                            {"field": "thumbnail", "headerName": "Thumbnail", "width": 100,
                             "cellRenderer": "params => { if (!params.value || !params.value.startsWith('https://')) return ''; const img=document.createElement('img'); img.src=params.value; img.loading='lazy'; img.style.width='48px'; img.style.height='48px'; img.style.objectFit='contain'; return img; }"},
                            {"field": "asin", "headerName": "ASIN", "checkboxSelection": True, "headerCheckboxSelection": True},
                            {"field": "title", "headerName": "Title", "flex": 2},
                            {"field": "brand", "headerName": "Brand"}, {"field": "price", "headerName": "Price"},
                            {"field": "source_kind", "headerName": "Source"}, {"field": "fit_score", "headerName": "Fit"},
                            {"field": "price_status", "headerName": "Price Status"}, {"field": "risk_status", "headerName": "Risk"},
                            {"field": "final_status", "headerName": "Final"}, {"field": "first_seen", "headerName": "First Seen"},
                            {"field": "last_seen", "headerName": "Last Seen"},
                        ], "rowData": rows, "rowSelection": {"mode": "multiRow"},
                        "pagination": False, "defaultColDef": {"sortable": True, "filter": True, "resizable": True},
                        "getRowId": "params => params.data.asin",
                    }, modules="community").classes("w-full h-[620px]")
                    grid_ref["grid"].on("rowClicked", lambda event: self._show_product_detail(int(event.args["id"])), ["data"])
                pages = max(1, (data["total"] + data["page_size"] - 1) // data["page_size"])
                footer.set_text(f"총 {data['total']:,}개 · 페이지 {data['page'] + 1}/{pages} · 한 페이지 {data['page_size']}개")
            except Exception as exc:
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
            self.package_selected_asins = [row["asin"] for row in rows]
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
                        ui.button(label, on_click=lambda status=status: (bulk_override(self.current_store, [product_id], status), dialog.close(), ui.notify("수동 override 저장")))
                    ui.button("Override 해제", on_click=lambda: (clear_bulk_override(self.current_store, [product_id]), dialog.close(), ui.notify("override 해제"))).props("outline")
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
        ui.label("Browser Capture to Spark Center mapping is not portal-verified. Start with a 5-product round-trip; packages over 100 require confirmation.").classes("text-amber-800")
        self._heading("Spark Center packages", "사용자가 Spark Center에 올릴 ready/package 폴더만 관리합니다.")
        status = ui.select(STATUS_OPTIONS, value=self.package_selected_statuses or ["PRIMARY"],
                           multiple=True, label="Status").classes("w-64")
        limit = ui.number("상품 수", value=50, min=1, max=5000).classes("w-32")
        ui.label("업로드 대상은 exports/spark_center/<store>/ready/<package_id> 폴더 하나입니다.").classes("text-amber-800")
        if self.package_selected_asins:
            ui.label(f"상품 페이지에서 선택한 {len(self.package_selected_asins)}개 ASIN 사용 예정")
        table_slot = ui.column().classes("w-full")
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
                        ui.button("폴더 열기", on_click=lambda path=row["output_path"]: self._open_folder(path)).props("outline dense")
                        ui.button("경로 복사", on_click=lambda path=row["output_path"]: ui.run_javascript(
                            f"navigator.clipboard.writeText({json.dumps(path)})")).props("flat dense")
                        if row["package_status"] == "CREATED":
                            ui.button("업로드 완료 표시", on_click=lambda pid=row["package_id"]: (mark_package(pid, "UPLOADED", "Marked in UI V2"), refresh_packages(), ui.notify("사용자 수동 업로드 기록 저장"))).props("dense")
                        ui.button("보관", on_click=lambda pid=row["package_id"]: (mark_package(pid, "ARCHIVED", "Archived in UI V2"), refresh_packages())).props("flat dense")
        def generate_package():
            try:
                selected_statuses = status.value or ["PRIMARY"]
                if "ALL" in selected_statuses:
                    selected_statuses = ["PRIMARY"]
                result = create_spark_package(self.current_store, selected_statuses,
                                              int(limit.value), self.package_selected_asins or None)
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
                        ui.button("Continue", on_click=lambda: (confirm_dialog.close(), generate_package())).props("color=warning")
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
                target = max(1, int(result["target_candidates"]))
                completed = result["master_imported"] + result["detail_complete"]
                batch_progress.value = min(1, completed / target)
                batch_details.set_text(f"목표 {target} · 후보 {result['total_seen']} · 중복 {result['deduped']} · 선필터 제외 {result['prefiltered']} · 상세 대기 {result['detail_pending']} · 상세 완료 {result['detail_complete']} · MASTER {result['master_imported']} · PRIMARY {result['primary_count']} · RESERVE {result['reserve_count']} · REVIEW {result['review_count']} · RESTRICTED {result['restricted_count']} · 실패 {result['failed_count']}" + (f" · {result['error']}" if result.get("error") else ""))
                failures = result.get("failed_items", [])
                batch_failures_label.set_text("최근 실패: " + (" | ".join(f"{item['asin']} · {item['state']} · 재시도 {item['retry_count']} · {item['last_error']}" for item in failures) if failures else "없음"))
                batch_events_label.set_text("최근 이벤트: " + (" | ".join(f"{event['created_at']} {event['event_type']}" for event in result.get("events", [])[:10]) if result.get("events") else "없음"))

                render_capture_summary()

            def kick_batch(run_id):
                outcome = batch_service.kickoff(run_id)
                item = outcome.get("item")
                if item:
                    url = item["url"] + "#shopsource_capture=" + run_id
                    ui.run_javascript("window.open(" + json.dumps(url) + ", '_blank', 'noopener')")
                return outcome

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
                    elif outcome["state"] == "WAITING":
                        ui.notify("다음 상품 처리 간격을 기다리고 있습니다. 잠시 후 '다음 처리'를 눌러주세요.", type="info")
                    elif outcome["state"] == "NO_PENDING":
                        ui.notify("현재 처리할 미완료 상품이 없습니다.", type="info")
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
                    elif result.get("detail_pending", 0) > 0:
                        outcome = kick_batch(result["run_id"])
                        if outcome["state"] == "OPENED":
                            ui.notify(f"이미 Queue에 있는 미완료 {result['detail_pending']}개 처리를 시작합니다.", type="positive")
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
                        elif outcome["state"] == "NO_PENDING":
                            ui.notify("현재 처리할 미완료 상품이 없습니다.", type="info")
                        return
                    result = batch_service.action(run_id, action)
                    render_batch(result)
                    if action in {"RESUME", "RETRY"}:
                        outcome = kick_batch(run_id)
                        if outcome["state"] == "IN_PROGRESS":
                            ui.notify("현재 상품 상세 처리가 진행 중입니다.", type="info")
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
                except Exception as exc: ui.notify(_safe_error(exc), type="negative")

            with ui.row():
                ui.button("미완료 상품 자동 처리", on_click=process_unfinished, icon="auto_awesome").props("color=primary")
                ui.button("고급: Amazon 검색 Batch 시작", on_click=start_batch, icon="playlist_add").props("outline")
                ui.button("고급: NEEDS_DETAIL 전체 Queue", on_click=queue_existing, icon="queue_play_next").props("outline")
                ui.button("일시정지", on_click=lambda: batch_action("PAUSE"), icon="pause").props("outline")
                continue_button["button"] = ui.button("계속", on_click=continue_batch, icon="play_arrow").props("outline")
                ui.button("취소", on_click=lambda: batch_action("CANCEL"), icon="stop").props("outline color=negative")
                ui.button("실패만 재시도", on_click=lambda: batch_action("RETRY"), icon="replay").props("outline")
            ui.label("상세 탭은 한 번에 하나씩, 최소 4초 간격으로 엽니다. 검색 페이지 이동은 사용자가 직접 합니다.").classes("text-xs text-amber-800")
            ui.timer(2.0, poll_batch)
            ui.label("상세 완료 상품은 MASTER에 반영하고, 상세 필요 상품은 같은 Batch에 자동 연결합니다.").classes("text-xs text-slate-600")
            ui.label("Phase 2.6.2 적용 후 chrome://extensions에서 ShopSource Capture 새로고침이 필요합니다.").classes("text-xs text-slate-500")
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
                    dialog.close(); ui.notify(f"Store Profile 저장: {path}", type="positive")
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
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"chrome-extension://[a-p]{32}",
                       allow_methods=["GET", "POST", "OPTIONS"],
                       allow_headers=["Content-Type", "X-ShopSource-Pairing"])
    install_capture_routes(app)
    OperatorUI(ui)
    ui.run(title="ShopSource Studio", host=args.host, port=args.port, reload=False, show=not args.no_browser)


if __name__ == "__main__":
    main()
