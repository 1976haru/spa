"""Shared beginner UI tokens and presentation-only helpers."""
BEGINNER_MODE_DEFAULT = True
MAX_UI_ROWS = 50
GLOBAL_UI_CSS = """
<style>
:root { --ss-body:16px; --ss-help:15px; --ss-button:16px; --ss-card-title:20px; --ss-page-title:28px; --ss-kpi:32px; }
body, .q-field, .q-table { font-size:var(--ss-body) !important; line-height:1.55; }
.q-btn { font-size:var(--ss-button) !important; min-height:42px; }
.q-drawer a { font-size:16px !important; line-height:1.5; }
.ss-help { font-size:var(--ss-help); color:#475569; }
.ss-page-title { font-size:var(--ss-page-title); line-height:1.25; font-weight:700; }
.ss-card-title { font-size:var(--ss-card-title); font-weight:650; }
.ss-kpi { font-size:var(--ss-kpi); line-height:1.15; font-weight:750; }
.ss-comfortable .q-table tbody td { padding-top:14px; padding-bottom:14px; }
.ss-focus:focus-visible, .q-btn:focus-visible, a:focus-visible { outline:3px solid #38bdf8 !important; outline-offset:2px; }
.ss-production-summary { position:sticky; top:0; z-index:30; background:#fff; box-shadow:0 4px 14px rgba(15,23,42,.10); }
.ss-gate-board { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:8px 14px; align-items:start; }
.ss-gate-row { min-height:76px; padding:8px 12px !important; border-left:4px solid #cbd5e1; }
.ss-gate-current { border-left-color:#2563eb; background:#eff6ff; }
.ss-gate-verified { border-left-color:#16a34a; }
.ss-gate-running { border-left-color:#2563eb; }
.ss-gate-waiting { border-left-color:#d97706; }
.ss-gate-review { border-left-color:#ea580c; }
.ss-gate-blocked { border-left-color:#dc2626; }
.ss-gate-idle { border-left-color:#94a3b8; }
.q-badge.ss-gate-verified { background:#dcfce7 !important; color:#166534 !important; }
.q-badge.ss-gate-running { background:#dbeafe !important; color:#1d4ed8 !important; }
.q-badge.ss-gate-waiting { background:#fef3c7 !important; color:#92400e !important; }
.q-badge.ss-gate-review { background:#ffedd5 !important; color:#9a3412 !important; }
.q-badge.ss-gate-blocked { background:#fee2e2 !important; color:#991b1b !important; }
.q-badge.ss-gate-idle { background:#f1f5f9 !important; color:#475569 !important; }
.ss-gate-number { min-width:34px; font-size:18px; font-weight:750; color:#334155; }
@media (max-width: 900px) { .ss-gate-board { grid-template-columns:minmax(0,1fr); } .ss-production-summary { position:sticky; top:0; } }
</style>
"""
BEGINNER_NAV_GROUPS = (
    ("홈", (("/","home","홈"),)),
    ("자동 구축", (("/build","auto_awesome","자동 구축"),)),
    ("상품·소싱", (("/source-safety","health_and_safety","소스 안전"),("/products","inventory_2","상품"),("/collections","collections_bookmark","컬렉션"))),
    ("스토어 디자인", (("/brand","palette","브랜드"),("/navigation","account_tree","메뉴"),("/homepage","web","홈페이지"))),
    ("스토어 완성", (("/production","rocket_launch","실전 스토어 완성"),("/completion","fact_check","완성도 검사"),("/pilot","science","실전 파일럿"))),
    ("기록", (("/history","history","기록"),)),
)

def paginate_rows(rows,page=0,page_size=50):
    size=max(1,min(MAX_UI_ROWS,int(page_size))); start=max(0,int(page))*size
    return {"rows":list(rows)[start:start+size],"page":max(0,int(page)),"page_size":size,"total":len(rows)}
