"""Beginner-facing labels and next actions for the multi-store dashboard."""

from __future__ import annotations


def status_label(row: dict) -> str:
    """Return a short Korean status without exposing backend state codes."""
    if row.get("check_status") == "ERROR":
        return "확인 실패"
    if row.get("check_status") == "READY":
        return "준비 완료"
    if row.get("shopify_connection") != "CONNECTED":
        return "연결 설정 필요"
    if row.get("theme_access_cli_state") == "CLI_NOT_INSTALLED":
        return "Shopify CLI 설치 확인 필요"
    if row.get("main_theme_role") not in (None, "MAIN") and row.get("theme_access_cli_state") == "READY":
        return "현재 테마 확인 필요"
    if row.get("theme_access_credential") != "YES" and row.get("graphql_state") != "VERIFIED_ACTIVE":
        return "설정 필요"
    if row.get("check_status") == "NOT_CHECKED":
        return "아직 확인 전"
    if row.get("last_checked_at") is None or row.get("cache_stale"):
        return "다시 확인 필요"
    return "설정 필요"


def next_action(row: dict) -> tuple[str, str]:
    """Return (button label, action key); only one primary action is intended."""
    if row.get("check_status") == "READY" and not row.get("cache_stale"):
        return "홈페이지 화면에서 추천 상품 보기", "homepage"
    if row.get("shopify_connection") != "CONNECTED":
        return "Shopify 연결 설정 확인", "connection"
    if row.get("theme_access_cli_state") == "CLI_NOT_INSTALLED":
        return "Shopify CLI 설치 상태 확인", "cli"
    if row.get("theme_access_credential") != "YES" and row.get("graphql_state") != "VERIFIED_ACTIVE":
        return "Theme Access 비밀번호 등록", "credential"
    return "선택한 스토어 읽기 확인", "check"


def next_action_message(row: dict) -> str:
    """A beginner-friendly sentence that explains the selected next step."""
    action = next_action(row)[1]
    return {
        "credential": "Theme Access 비밀번호를 등록하세요.",
        "connection": "먼저 Shopify 연결 설정을 확인하세요.",
        "cli": "Shopify CLI 설치 상태를 확인하세요.",
        "check": "연결 상태를 읽기 확인하세요.",
        "homepage": "준비가 끝났습니다. 홈페이지에서 추천 상품을 확인할 수 있습니다.",
    }[action]


def setup_needed_count(rows: list[dict]) -> int:
    """Count rows shown to beginners as needing setup or attention."""
    setup_statuses = {
        "설정 필요", "연결 설정 필요", "Shopify CLI 설치 확인 필요",
        "현재 테마 확인 필요", "다시 확인 필요", "확인 실패",
    }
    return sum(status_label(row) in setup_statuses for row in rows)


def table_statuses(row: dict) -> dict[str, str]:
    """Human-readable summary values; raw evidence stays in advanced details."""
    shop = {"CONNECTED": "연결됨", "NOT_CONFIGURED": "연결 설정 필요", "CREDENTIAL_MISSING": "인증정보 확인 필요"}
    graphql = {"VERIFIED_ACTIVE": "사용 가능", "SCOPE_MISSING": "승인 또는 사용 불가", "SCOPE_GRANTED_UNVERIFIED": "승인 확인 필요",
               "ACCESS_DENIED_LAST_ATTEMPT": "접근 거부됨", "NOT_CHECKED": "아직 확인 전", "UNKNOWN": "확인 필요"}
    cli = {"READY": "사용 가능", "CREDENTIAL_MISSING": "비밀번호 등록 필요", "CLI_NOT_INSTALLED": "설치 확인 필요", "NOT_CHECKED": "아직 확인 전", "UNKNOWN": "확인 필요"}
    return {"shopify": shop.get(row.get("shopify_connection"), "확인 필요"),
            "graphql": graphql.get(row.get("graphql_state"), "확인 필요"),
            "theme_access": "등록됨 · " + cli.get(row.get("theme_access_cli_state"), "확인 필요") if row.get("theme_access_credential") == "YES" else "비밀번호 미등록"}
