"""Presentation models for the compact production gate control center.

This module is deliberately side-effect free: constructing a board row or
selecting a gate never starts a source check or contacts Shopify.
"""
from __future__ import annotations

from ..production import GATES, GATE_LABELS_KO

STATUS_KO = {
    "VERIFIED": "검증 완료", "READY": "준비됨", "READY_WITH_WARNINGS": "주의와 함께 준비됨",
    "RUNNING": "진행 중", "WAITING_FOR_INPUT": "정보 입력 필요",
    "WAITING_FOR_CONFIRMATION": "사용자 확인 대기", "WAITING_FOR_CREDENTIALS": "연결 정보 필요",
    "REVIEW_REQUIRED": "확인 필요", "BLOCKED": "차단", "FAILED_TRANSIENT": "일시 오류",
    "NOT_STARTED": "아직 확인 전",
}
STATUS_CLASS = {
    "VERIFIED": "ss-gate-verified", "READY": "ss-gate-verified", "READY_WITH_WARNINGS": "ss-gate-review",
    "RUNNING": "ss-gate-running", "WAITING_FOR_INPUT": "ss-gate-waiting",
    "WAITING_FOR_CONFIRMATION": "ss-gate-waiting", "WAITING_FOR_CREDENTIALS": "ss-gate-waiting",
    "REVIEW_REQUIRED": "ss-gate-review", "BLOCKED": "ss-gate-blocked",
    "FAILED_TRANSIENT": "ss-gate-blocked", "NOT_STARTED": "ss-gate-idle",
}
ACTION_LABELS = {
    "ENVIRONMENT_STORE_IDENTITY": "Shopify 연결 확인", "SOURCING_QUALITY": "소싱 품질 보기",
    "SOURCE_SAFETY": "10개 무료 검사 시작", "PRODUCT_CONTENT": "내용 확인·보완",
    "PRODUCT_MEDIA": "이미지 권리 검토", "PRICING_MARGIN": "가격마진 정책 설정",
    "COLLECTION_ARCHITECTURE": "컬렉션 설계 보기", "COLLECTION_CATEGORY_MEDIA": "부족한 이미지 준비",
    "BRAND_HEADER_NAVIGATION": "브랜드·메뉴 확인", "HOMEPAGE": "홈페이지 미리보기 확인",
    "PRODUCT_COLLECTION_TEMPLATES": "화면 구성 확인", "PAGES_POLICIES": "페이지정책 확인",
    "SEO_ACCESSIBILITY_MOBILE": "데스크톱·모바일 확인", "COMMERCE_READINESS": "판매 설정 확인",
    "CONTROLLED_LIVE_PILOT": "파일럿 준비상태 보기", "BATCH_EXPANSION": "확대 조건 보기",
    "FINAL_LAUNCH_READINESS": "최종 출시 조건 보기",
}
PANEL_KIND = {
    "ENVIRONMENT_STORE_IDENTITY": "shopify_connection", "SOURCING_QUALITY": "sourcing_audit",
    "SOURCE_SAFETY": "source_safety", "PRODUCT_CONTENT": "product_content",
    "PRODUCT_MEDIA": "media_rights", "PRICING_MARGIN": "pricing_policy",
    "COLLECTION_ARCHITECTURE": "collection_plan", "COLLECTION_CATEGORY_MEDIA": "collection_media",
    "BRAND_HEADER_NAVIGATION": "brand_navigation", "HOMEPAGE": "homepage",
    "PRODUCT_COLLECTION_TEMPLATES": "templates", "PAGES_POLICIES": "pages_policies",
    "SEO_ACCESSIBILITY_MOBILE": "visual_signoff", "COMMERCE_READINESS": "commerce_readiness",
    "CONTROLLED_LIVE_PILOT": "pilot_gate", "BATCH_EXPANSION": "rollout_gate",
    "FINAL_LAUNCH_READINESS": "final_gate",
}
# Explicit key-to-panel registry makes all 17 rows actionable and testable.
GATE_ACTION_REGISTRY = {key: {"label": ACTION_LABELS[key], "panel": PANEL_KIND[key]} for key in GATES}
PASSED = {"READY", "READY_WITH_WARNINGS", "VERIFIED"}


def resolve_gate_click(run: dict, gate_key_or_number: str | int) -> str:
    """Resolve either a gate key or visible 1-based number without side effects."""
    if isinstance(gate_key_or_number, int) or str(gate_key_or_number).isdigit():
        index = int(gate_key_or_number) - 1
        if not 0 <= index < len(run.get("gates", [])):
            raise KeyError(gate_key_or_number)
        return str(run["gates"][index]["gate_key"])
    key = str(gate_key_or_number)
    if key not in GATE_ACTION_REGISTRY or not any(g.get("gate_key") == key for g in run.get("gates", [])):
        raise KeyError(gate_key_or_number)
    return key


def _summary(gate: dict) -> str:
    key, evidence = gate.get("gate_key"), gate.get("evidence") or {}
    counts = evidence.get("counts") or {}
    if key == "SOURCE_SAFETY":
        checked = counts.get("checked", evidence.get("checked", 0))
        target = counts.get("source_check_eligible", evidence.get("source_check_eligible", 0))
        if not target:
            target = evidence.get("target_count", 0)
        return f"최신 원본 재고·가격 확인 {checked} / {target}" if target else "판매 후보의 최신 원본 확인이 필요합니다."
    if key == "PRODUCT_CONTENT":
        missing = counts.get("content_review", counts.get("incomplete", 0))
        total = counts.get("total", 0)
        return f"{missing}개 상품의 설명·검색 정보 확인 필요 / 전체 {total}개" if total else "상품명·설명·옵션·SEO 근거를 확인해야 합니다."
    if gate.get("status") in PASSED:
        return "필요한 증거가 저장되었습니다. 세부 항목과 확인 시각을 볼 수 있습니다."
    if key == "PRODUCT_MEDIA": return "상품 이미지 사용 권리와 검토 기록이 필요합니다."
    if key == "PRICING_MARGIN": return "승인된 판매가·마진 정책 확인이 필요합니다."
    if key == "PAGES_POLICIES": return "페이지 내용과 실제 사업정보 확인이 필요합니다."
    if key == "SEO_ACCESSIBILITY_MOBILE": return "자동 점검 후 현재 화면의 데스크톱·모바일 확인이 필요합니다."
    if key == "COMMERCE_READINESS": return "배송·세금·결제 등 판매 설정 확인이 필요합니다."
    status = gate.get("status", "NOT_STARTED")
    return {"BLOCKED": "선행 조건 또는 오류를 먼저 해결해야 합니다.",
            "WAITING_FOR_CREDENTIALS": "Shopify 연결 정보를 입력해야 합니다.",
            "WAITING_FOR_CONFIRMATION": "명시적인 사용자 확인이 필요합니다.",
            "WAITING_FOR_INPUT": "확인에 필요한 정보를 입력해야 합니다.",
            "REVIEW_REQUIRED": "근거를 확인하고 검토 결과를 저장해야 합니다.",
            "NOT_STARTED": "아직 근거를 확인하지 않았습니다.",
            "RUNNING": "증거를 확인하고 있습니다."}.get(status, "저장된 증거와 다음 행동을 확인하세요.")


def gate_board_rows(run: dict, progress: dict) -> list[dict]:
    current = progress.get("current_stage")
    rows = []
    for index, gate in enumerate(run.get("gates", []), 1):
        key = gate["gate_key"]
        rows.append({"number": index, "gate_key": key,
                     "label": GATE_LABELS_KO.get(key, key), "status": gate.get("status", "NOT_STARTED"),
                     "status_ko": STATUS_KO.get(gate.get("status"), "상태 확인 필요"),
                     "status_class": STATUS_CLASS.get(gate.get("status"), "ss-gate-idle"),
                     "summary": _summary(gate), "action_label": ACTION_LABELS[key],
                     "panel": PANEL_KIND[key], "current": key == current,
                     "blockers": list(gate.get("blockers") or []), "evidence": gate.get("evidence") or {},
                     "verified": gate.get("status") in PASSED})
    return rows


def production_summary(run: dict, progress: dict, store_profile: dict | None = None) -> dict:
    """Build the summary from the active run/profile, never from a store constant."""
    profile = dict(store_profile or {})
    store_id = str(profile.get("store_id") or run.get("store_id") or "")
    store_name = str(profile.get("store_name") or run.get("store_name") or (f"Store {store_id}" if store_id else "스토어 미선택"))
    return {"store_id": store_id, "store_name": store_name, "percent": progress["production_readiness_percent"],
            "verified": len(progress.get("completed", [])), "denominator": progress["evidence_gate_count"],
            "current_stage": progress.get("current_stage"), "current_label": progress.get("current_stage_label"),
            "status": run.get("status", "NOT_READY"), "next_action": progress.get("next_action"),
            "remaining": len(progress.get("remaining", [])),
            "progress_note": "화면에는 17개 작업 단계를 모두 표시합니다. 진행률은 출시 전 증거 단계 14개만 계산합니다."}
