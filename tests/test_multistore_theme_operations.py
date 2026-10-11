from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shopsource.db import connect
from shopsource.multistore_theme_operations import MultiStoreThemeOperationsService


def _store(store_id, name=None):
    return {"store_id": str(store_id), "store_name": name or f"Store {store_id}", "profile": {}}


def _service(tmp_path, stores, *, connections=None, summaries=None, credentials=None, coordinator_factory=None,
             token_provider=None, auth_status_provider=None, **kwargs):
    connections = connections or {}
    summaries = summaries or {}
    credentials = credentials or {}

    class Coordinator:
        def capability_summary(self, store_id, selection="AUTO"):
            return summaries[str(store_id)]

    return MultiStoreThemeOperationsService(
        db=tmp_path / "multistore.sqlite3", store_provider=lambda: stores,
        connection_provider=lambda store_id: connections.get(str(store_id)),
        token_provider=token_provider or (lambda store_id: "mock-token" if connections.get(str(store_id), {}).get("credential_present") else None),
        auth_status_provider=auth_status_provider or (lambda _store_id: {"production_app_bound": False}),
        theme_credential_provider=lambda store_id: credentials.get(str(store_id), False),
        coordinator_factory=coordinator_factory or Coordinator, **kwargs)


def _summary(*, graphql="SCOPE_MISSING", cli="CREDENTIAL_MISSING", selected=None, theme_id=None,
             theme_name=None, role=None, reason="NO_APPROVED_WRITE_PATH"):
    return {"graphql_state": graphql, "theme_access_cli_state": cli,
            "selected_backend": selected, "verified_theme_id": theme_id,
            "verified_theme_name": theme_name, "verified_theme_role": role,
            "selection_reason": reason}


def test_dashboard_uses_existing_store_registry(tmp_path):
    registered = [_store("store-b"), _store("store-a")]
    service = _service(tmp_path, registered)
    rows = service.list_rows()
    assert [row["store_id"] for row in rows] == ["store-a", "store-b"]
    assert {row["store_id"] for row in rows} == {row["store_id"] for row in registered}


def test_initial_render_does_not_remote_check_all_stores(tmp_path):
    calls = []
    service = _service(tmp_path, [_store("a"), _store("b")], coordinator_factory=lambda: calls.append("remote"))
    rows = service.list_rows()
    assert len(rows) == 2 and all(row["check_status"] == "NOT_CHECKED" for row in rows)
    assert calls == []


def test_status_cache_is_non_secret_and_stale_cache_is_not_ready(tmp_path):
    service = _service(tmp_path, [_store("x")],
        connections={"x": {"shop_domain": "x.myshopify.com", "credential_present": True}},
        summaries={"x": _summary(graphql="VERIFIED_ACTIVE", selected="ADMIN_GRAPHQL",
            theme_id="gid://shopify/OnlineStoreTheme/1", role="MAIN")})
    service.check_store("x")
    with connect(service.db) as con:
        columns = {row["name"] for row in con.execute("PRAGMA table_info(multistore_theme_status_cache)")}
        con.execute("UPDATE multistore_theme_status_cache SET checked_at=? WHERE store_id='x'",
                    ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),))
    assert not columns.intersection({"password", "token", "access_token", "client_secret", "credential_value"})
    row = service.list_rows()[0]
    assert row["cache_stale"] is True
    assert row["check_status"] == "NOT_CHECKED"


def test_store_without_shopify_connection_needs_setup(tmp_path):
    service = _service(tmp_path, [_store("a")], token_provider=lambda _store_id: (_ for _ in ()).throw(AssertionError("no token lookup")))
    result = service.check_store("a")
    assert result["check_status"] == "NEEDS_SETUP"
    assert result["diagnostic_code"] == "SHOPIFY_CONNECTION_REQUIRED"


def test_store_without_theme_access_and_graphql_missing_needs_setup(tmp_path):
    store = _store("a")
    service = _service(tmp_path, [store], connections={"a": {"shop_domain": "a.myshopify.com", "credential_present": True}},
                       summaries={"a": _summary()})
    result = service.check_store("a")
    assert result["check_status"] == "NEEDS_SETUP"
    assert result["graphql_state"] == "SCOPE_MISSING"
    assert result["theme_access_credential"] == "NO"


def test_graphql_verified_active_counts_ready(tmp_path):
    service = _service(tmp_path, [_store("shop-x")],
        connections={"shop-x": {"shop_domain": "x.myshopify.com", "credential_present": True}},
        summaries={"shop-x": _summary(graphql="VERIFIED_ACTIVE", selected="ADMIN_GRAPHQL",
            theme_id="gid://shopify/OnlineStoreTheme/1", theme_name="Main", role="MAIN", reason="GRAPHQL_EXEMPTION_VERIFIED")})
    result = service.check_store("shop-x")
    assert result["check_status"] == "READY" and result["auto_backend"] == "ADMIN_GRAPHQL"


def test_graphql_scope_granted_unverified_not_counted_ready(tmp_path):
    service = _service(tmp_path, [_store("x")],
        connections={"x": {"shop_domain": "x.myshopify.com", "credential_present": True}},
        summaries={"x": _summary(graphql="SCOPE_GRANTED_UNVERIFIED", theme_id="gid://shopify/OnlineStoreTheme/1", role="MAIN")})
    result = service.check_store("x")
    assert result["check_status"] == "NEEDS_SETUP"
    assert result["auto_backend"] == "NO_WRITE_BACKEND"


def test_theme_access_credential_yes_does_not_equal_cli_ready(tmp_path):
    service = _service(tmp_path, [_store("x")],
        connections={"x": {"shop_domain": "x.myshopify.com", "credential_present": True}},
        summaries={"x": _summary(cli="CLI_NOT_INSTALLED")}, credentials={"x": True})
    result = service.check_store("x")
    assert result["theme_access_credential"] == "YES"
    assert result["theme_access_cli_state"] == "CLI_NOT_INSTALLED"
    assert result["check_status"] == "NEEDS_SETUP"


def test_theme_access_ready_requires_verified_main_identity(tmp_path):
    base = {"x": {"shop_domain": "x.myshopify.com", "credential_present": True}}
    bad = _service(tmp_path / "bad.sqlite3", [_store("x")], connections=base,
        summaries={"x": _summary(cli="READY", selected="THEME_ACCESS_CLI", theme_id="1", role="unknown")}, credentials={"x": True})
    good = _service(tmp_path / "good.sqlite3", [_store("x")], connections=base,
        summaries={"x": _summary(cli="READY", selected="THEME_ACCESS_CLI", theme_id="1", theme_name="Live", role="MAIN")}, credentials={"x": True})
    assert bad.check_store("x")["check_status"] == "NEEDS_SETUP"
    assert good.check_store("x")["check_status"] == "READY"


def test_check_all_is_read_only(tmp_path):
    mutations = []
    stores = [_store("x"), _store("y")]
    service = _service(tmp_path, stores,
        connections={key: {"shop_domain": f"{key}.myshopify.com", "credential_present": True} for key in ("x", "y")},
        summaries={key: _summary(graphql="SCOPE_GRANTED_UNVERIFIED") for key in ("x", "y")},
        coordinator_factory=lambda: type("ReadOnlyCoordinator", (), {
            "capability_summary": lambda _self, store_id, selection="AUTO": _summary(graphql="SCOPE_MISSING")})())
    result = service.check_all()
    assert result["write_performed"] is False
    assert all(row["write_performed"] is False for row in result["results"])
    assert mutations == []


def test_check_all_failure_isolated_per_store(tmp_path, monkeypatch):
    service = _service(tmp_path, [_store("a"), _store("b")])
    original = service.check_store
    def check(store_id, **_kwargs):
        if store_id == "a":
            raise RuntimeError("synthetic failure")
        return {"store_id": store_id, "check_status": "READY", "write_performed": False}
    monkeypatch.setattr(service, "check_store", check)
    result = service.check_all()
    assert {row["store_id"] for row in result["results"]} == {"a", "b"}
    assert next(row for row in result["results"] if row["store_id"] == "a")["check_status"] == "ERROR"


def test_check_all_results_sorted_by_store_id(tmp_path, monkeypatch):
    service = _service(tmp_path, [_store("03"), _store("01"), _store("02")])
    monkeypatch.setattr(service, "check_store", lambda store_id, **_kwargs: {"store_id": store_id, "check_status": "READY"})
    assert [row["store_id"] for row in service.check_all()["results"]] == ["01", "02", "03"]


def test_check_all_bounded_concurrency(tmp_path, monkeypatch):
    service = _service(tmp_path, [_store(str(i)) for i in range(12)], max_workers=3)
    lock = threading.Lock()
    state = {"active": 0, "max": 0}
    def check(store_id, **_kwargs):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(.015)
        with lock:
            state["active"] -= 1
        return {"store_id": store_id, "check_status": "READY", "write_performed": False}
    monkeypatch.setattr(service, "check_store", check)
    result = service.check_all()
    assert state["max"] <= 3
    assert result["max_concurrency"] == 3
    assert result["counts"]["total"] == 12


def test_password_save_never_returns_or_displays_secret():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert "password.value}" not in panel
    assert "원격 확인은 실행하지 않았습니다." in panel
    assert "Theme Access 비밀번호" in panel


def test_password_input_clears_after_save():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert 'password.value = ""' in panel
    assert 'props("type=password autocomplete=new-password")' in panel


def test_delete_theme_access_affects_only_selected_store():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert "store_id = str(selected_store.value or \"\")" in panel
    assert "delete_theme_access_password(store_id)" in panel


def test_store_001_not_hardcoded():
    service_source = Path("src/shopsource/multistore_theme_operations.py").read_text(encoding="utf-8")
    assert '"001"' not in service_source and "Cabin Tidy" not in service_source


def test_no_bulk_apply_control():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert "전체 스토어 읽기 확인" in panel
    assert "Bulk apply" not in panel and "bulk apply" not in panel


def test_no_shopify_live_write():
    source = Path("src/shopsource/multistore_theme_operations.py").read_text(encoding="utf-8")
    assert "themeFilesUpsert" not in source
    assert "theme push" not in source
    assert '"write_performed": False' in source


def test_multistore_advanced_details_show_cli_domain_identity_only():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert "Configured domain: {configured_domain}" in panel
    assert "Theme Access CLI domain: {cli_domain}" in panel
    assert "CLI domain source: {cli_domain_source_label}" in panel
    assert '_resolve_cli_store_domain(connection)' in panel


def test_dashboard_has_dedicated_navigation_route():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert '("/theme-operations", "hub", "여러 스토어 테마 관리")' in source
    assert 'path == "/theme-operations"' in source


def test_multistore_nav_is_korean_beginner_label():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert '("/theme-operations", "hub", "여러 스토어 테마 관리")' in source


def test_page_has_beginner_four_step_guide():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert "처음이라면 이렇게 하세요" in panel
    assert "1. 아래 목록에서 스토어를 선택합니다." in panel
    assert "4. 준비 완료가 되면 홈페이지" in panel


def test_user_visible_statuses_are_human_readable():
    from shopsource.multistore_theme_presentation import status_label, table_statuses
    row = {"check_status": "NEEDS_SETUP", "shopify_connection": "CONNECTED",
           "graphql_state": "SCOPE_MISSING", "theme_access_cli_state": "CREDENTIAL_MISSING",
           "theme_access_credential": "NO", "cache_stale": False}
    assert status_label(row) == "설정 필요"
    assert "SCOPE_MISSING" not in str(table_statuses(row))
    assert table_statuses(row)["graphql"] == "승인 또는 사용 불가"


def test_raw_states_remain_available_in_advanced_details():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert 'ui.expansion("고급 정보 보기"' in panel
    assert "GraphQL state: {row['graphql_state']}" in panel
    assert "Selected backend: {row['auto_backend']}" in panel


def test_selected_store_has_single_primary_next_action():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert "선택한 스토어의 다음 할 일" in panel
    assert 'primary_action = ui.button' in panel
    assert 'primary_action.on_click(run_primary_action)' in panel


def test_missing_theme_access_next_action_is_register():
    from shopsource.multistore_theme_presentation import next_action
    row = {"check_status": "NOT_CHECKED", "shopify_connection": "CONNECTED",
           "graphql_state": "SCOPE_MISSING", "theme_access_credential": "NO"}
    assert next_action(row) == ("Theme Access 비밀번호 등록", "credential")


def test_credential_present_next_action_is_read_check():
    from shopsource.multistore_theme_presentation import next_action
    row = {"check_status": "NOT_CHECKED", "shopify_connection": "CONNECTED",
           "graphql_state": "SCOPE_MISSING", "theme_access_credential": "YES",
           "theme_access_cli_state": "NOT_CHECKED"}
    assert next_action(row) == ("선택한 스토어 읽기 확인", "check")


def test_ready_next_action_is_homepage():
    from shopsource.multistore_theme_presentation import next_action
    row = {"check_status": "READY", "cache_stale": False}
    assert next_action(row) == ("홈페이지 화면에서 추천 상품 보기", "homepage")


def test_registration_dialog_explains_where_password_comes_from():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert "Shopify 관리자에서 발급한 Theme Access 비밀번호" in source
    assert "비밀번호 발급 위치 보기" in source


def test_save_and_check_is_explicit_and_read_only():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert 'ui.button("저장 후 읽기 확인", on_click=save_and_check' in panel
    assert "await check_one_for_store(store_id)" in panel
    assert "service.check_store, store_id" in panel


def test_delete_is_secondary_and_confirmed():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert 'ui.expansion("고급 관리"' in panel
    assert "if not confirm_delete.value" in panel


def test_bulk_check_message_says_no_store_change():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert "스토어 변경은 없었습니다." in source


def test_initial_render_still_no_remote_calls():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert "Remote checks are read-only and run only on request." not in panel
    assert "service.check_store" in panel and "service.check_all" in panel
    assert panel.index("service.list_rows()") < panel.index("def check_all")


def test_no_live_write_controls_in_multistore_panel():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert "themeFilesUpsert" not in panel
    assert "theme push" not in panel
    assert "테마나 상품을 Shopify에 적용하거나 변경하지 않습니다." in panel


def test_setup_kpi_matches_beginner_visible_setup_states():
    from shopsource.multistore_theme_presentation import setup_needed_count, status_label
    rows = [
        {"check_status": "NEEDS_SETUP", "shopify_connection": "CONNECTED", "graphql_state": "SCOPE_MISSING",
         "theme_access_cli_state": "CREDENTIAL_MISSING", "theme_access_credential": "NO"},
        {"check_status": "READY", "shopify_connection": "CONNECTED", "graphql_state": "VERIFIED_ACTIVE",
         "theme_access_cli_state": "UNKNOWN", "theme_access_credential": "NO"},
        {"check_status": "ERROR", "shopify_connection": "CONNECTED", "graphql_state": "UNKNOWN",
         "theme_access_cli_state": "UNKNOWN", "theme_access_credential": "NO"},
        {"check_status": "NOT_CHECKED", "shopify_connection": "CONNECTED", "graphql_state": "VERIFIED_ACTIVE",
         "theme_access_cli_state": "NOT_CHECKED", "theme_access_credential": "NO"},
    ]
    assert setup_needed_count(rows) == sum(status_label(row) in {
        "설정 필요", "연결 설정 필요", "Shopify CLI 설치 확인 필요",
        "현재 테마 확인 필요", "다시 확인 필요", "확인 실패",
    } for row in rows)


def test_three_store_fixture_counts_three_setup_needed():
    from shopsource.multistore_theme_presentation import setup_needed_count, status_label
    rows = [
        {"store_id": "001", "check_status": "NOT_CHECKED", "shopify_connection": "CONNECTED",
         "graphql_state": "SCOPE_MISSING", "theme_access_cli_state": "CREDENTIAL_MISSING",
         "theme_access_credential": "NO", "cache_stale": False},
        {"store_id": "002", "check_status": "NOT_CHECKED", "shopify_connection": "NOT_CONFIGURED",
         "graphql_state": "NOT_CHECKED", "theme_access_cli_state": "NOT_CHECKED",
         "theme_access_credential": "NO", "cache_stale": False},
        {"store_id": "003", "check_status": "NOT_CHECKED", "shopify_connection": "NOT_CONFIGURED",
         "graphql_state": "NOT_CHECKED", "theme_access_cli_state": "NOT_CHECKED",
         "theme_access_credential": "NO", "cache_stale": False},
    ]
    assert [status_label(row) for row in rows] == ["설정 필요", "연결 설정 필요", "연결 설정 필요"]
    assert setup_needed_count(rows) == 3


def test_selected_store_components_created_inside_card():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    card_start = panel.index('with ui.card().classes("w-full border-2 border-blue-300"):')
    card_end = panel.index('with ui.row().classes("w-full flex-wrap gap-2")', card_start)
    card_block = panel[card_start:card_end]
    assert 'ui.label("선택한 스토어의 다음 할 일")' in card_block
    assert 'selected_card_title = ui.label' in card_block
    assert 'selected_card_status = ui.label' in card_block
    assert 'selected_card_next = ui.label' in card_block
    assert 'primary_action = ui.button' in card_block


def test_selected_store_next_action_message_is_sentence():
    from shopsource.multistore_theme_presentation import next_action_message
    row = {"check_status": "NOT_CHECKED", "shopify_connection": "CONNECTED",
           "graphql_state": "SCOPE_MISSING", "theme_access_credential": "NO"}
    assert next_action_message(row) == "Theme Access 비밀번호를 등록하세요."
    assert next_action_message({"check_status": "READY", "cache_stale": False}) == "준비가 끝났습니다. 홈페이지에서 추천 상품을 확인할 수 있습니다."


def test_search_placeholder_uses_domain_not_email():
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    panel = source[source.index("def _multistore_theme_operations") : source.index("def _stores(self)", source.index("def _multistore_theme_operations"))]
    assert 'ui.input("스토어 이름, 번호, 도메인 검색")' in panel
    assert "이메일로 검색" not in panel
