"""Per-store brand identity, logo/favicons, and guarded Shopify theme settings.

Binary assets live under exports; SQLite stores metadata and approval state only.
All network edges are injectable so tests can stay synthetic.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont
except ModuleNotFoundError:  # Profile/prompt reads must work before dependency repair.
    Image = ImageChops = ImageDraw = ImageFont = None

from .db import connect, get_store, init_db
from .paths import EXPORT_DIR
from .shopify_collections import SHOPIFY_API_VERSION, ShopifyGraphQLClient, ShopifyFileUploader, get_connection, get_shopify_token

APPROVAL_STATES = {"DRAFT", "GENERATED", "NEEDS_REVIEW", "APPROVED", "REJECTED", "SUPERSEDED"}
ASSET_KINDS = {"LOGO_MARK", "LOGO_HORIZONTAL", "FAVICON_MASTER", "FAVICON_32", "FAVICON_64"}
BRAND_ASSIGNMENT_CONTRACT = {
    "version": "1.0", "name_candidates": 10, "shortlist": 3,
    "candidate_fields": ("brand_name", "pronunciation", "meaning_and_rationale", "brand_image", "direction", "review_status"),
    "name_review_states": ("UNREVIEWED", "BASIC_CONFLICT_FOUND", "BASIC_CHECK_PASS", "DOMAIN_REVIEW_REQUIRED", "TRADEMARK_REVIEW_REQUIRED", "USER_APPROVED"),
    "legal_clearance_automated": False, "default_image_provider": "PROMPT_ONLY",
}


def _require_pillow():
    if Image is None:
        raise RuntimeError("이미지 기능에 필요한 Pillow가 현재 실행 환경에 없습니다. ShopSource 환경 자동 복구를 실행하세요.")
THEME_FILES_QUERY = """query BrandThemeFiles($id: ID!) {
 theme(id:$id) { id name role files(first:10, filenames:[\"config/settings_schema.json\",\"config/settings_data.json\"]) {
 nodes { filename body { __typename ... on OnlineStoreThemeFileBodyText { content } ... on OnlineStoreThemeFileBodyBase64 { contentBase64 } } }
 } }
}"""
THEMES_QUERY = "query BrandThemes { themes(first:50) { nodes { id name role } } }"
UPSERT_THEME_FILES = """mutation BrandThemeFilesUpsert($themeId:ID!,$files:[OnlineStoreThemeFilesUpsertFileInput!]!) {
 themeFilesUpsert(themeId:$themeId,files:$files) { job { id } upsertedThemeFiles { filename } userErrors { field message } }
}"""


def _now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def _hash(value): return hashlib.sha256(value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
def _safe_store(value): return re.sub(r"[^A-Za-z0-9_-]", "_", str(value))
def _brand_root(store_id, version): return EXPORT_DIR / "brand_assets" / _safe_store(store_id) / f"v{int(version)}"


def _install(db=None):
    init_db(db)
    with connect(db) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS brand_profiles(
          store_id TEXT PRIMARY KEY,version INTEGER NOT NULL,profile_json TEXT NOT NULL,source_hash TEXT NOT NULL,
          approval_status TEXT NOT NULL DEFAULT 'DRAFT',approved_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS brand_assets(
          asset_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,asset_type TEXT NOT NULL,version INTEGER NOT NULL,
          provider TEXT NOT NULL,provider_model TEXT NOT NULL DEFAULT '',prompt_hash TEXT NOT NULL DEFAULT '',
          source_asset_id TEXT,width INTEGER,height INTEGER,format TEXT NOT NULL,transparent_background INTEGER NOT NULL DEFAULT 0,
          local_path TEXT NOT NULL,sha256 TEXT NOT NULL,approval_status TEXT NOT NULL,generation_status TEXT NOT NULL,
          shopify_file_id TEXT,shopify_url TEXT,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,
          UNIQUE(store_id,asset_type,version));
        CREATE TABLE IF NOT EXISTS brand_apply_previews(
          preview_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,theme_id TEXT NOT NULL,template_hash TEXT NOT NULL,
          proposal_json TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS brand_name_state(
          store_id TEXT PRIMARY KEY,brand_name TEXT NOT NULL,name_status TEXT NOT NULL,
          locked_at TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS brand_name_candidates(
          store_id TEXT NOT NULL,candidate_key TEXT NOT NULL,candidate_json TEXT NOT NULL,
          review_status TEXT NOT NULL,conflicts_json TEXT NOT NULL DEFAULT '[]',shortlisted INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,PRIMARY KEY(store_id,candidate_key));
        CREATE TABLE IF NOT EXISTS brand_theme_backups(
          backup_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,theme_id TEXT NOT NULL,settings_schema TEXT NOT NULL,
          settings_data TEXT NOT NULL,folder TEXT NOT NULL,created_at TEXT NOT NULL);
        """)


def _write_report(store_id, run_id, *, apply_preview=None, db=None):
    profile = get_brand_profile(store_id, db=db)
    assets = list_brand_assets(store_id, db=db)
    root = EXPORT_DIR / "brand_reports" / _safe_store(store_id) / _safe_store(run_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "brand_profile.json").write_text(json.dumps(profile or {}, ensure_ascii=False, indent=2), encoding="utf-8")
    public_assets = [{key: asset.get(key) for key in ("asset_id","asset_type","version","provider","provider_model","prompt_hash","source_asset_id","width","height","format","transparent_background","local_path","sha256","approval_status","generation_status","shopify_file_id","shopify_url","metadata")}
                     for asset in assets]
    (root / "assets.json").write_text(json.dumps(public_assets, ensure_ascii=False, indent=2), encoding="utf-8")
    if apply_preview is not None:
        (root / "apply_preview.json").write_text(json.dumps(apply_preview, ensure_ascii=False, indent=2), encoding="utf-8")
    lines=[f"# Brand automation {run_id}","",f"Store: {store_id}",f"Brand: {(profile or {}).get('profile',{}).get('brand_name','(unset)')}",
      f"Profile version: {(profile or {}).get('version','n/a')}",f"Assets: {len(assets)}"]
    if apply_preview:lines += [f"Theme status: {apply_preview.get('status')}",f"Theme actions: {', '.join(row.get('action','') for row in apply_preview.get('actions',[]))}"]
    (root/"summary.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    return root


def get_brand_profile(store_id, *, db=None):
    _install(db)
    with connect(db) as con:
        row = con.execute("SELECT * FROM brand_profiles WHERE store_id=?", (store_id,)).fetchone()
    if not row: return None
    result = dict(row); result["profile"] = json.loads(result.pop("profile_json"))
    with connect(db) as con:
        name_state = con.execute("SELECT brand_name,name_status,locked_at FROM brand_name_state WHERE store_id=?", (str(store_id),)).fetchone()
    result["name_status"] = name_state["name_status"] if name_state else "REVIEW_REQUIRED"
    result["name_locked"] = result["name_status"] == "LOCKED"
    return result


def _seed_brand_name_state(store_id, brand_name, *, explicit=False, db=None):
    _install(db)
    now = _now()
    state = "LOCKED" if explicit and str(brand_name or "").strip() else "REVIEW_REQUIRED"
    with connect(db) as con:
        con.execute("""INSERT OR IGNORE INTO brand_name_state(store_id,brand_name,name_status,locked_at,updated_at)
            VALUES(?,?,?,?,?)""", (str(store_id), str(brand_name or ""), state, now if state == "LOCKED" else None, now))
        return dict(con.execute("SELECT * FROM brand_name_state WHERE store_id=?", (str(store_id),)).fetchone())


def brand_name_state(store_id, *, db=None):
    profile = get_brand_profile(store_id, db=db)
    if profile:
        with connect(db) as con:
            row = con.execute("SELECT * FROM brand_name_state WHERE store_id=?", (str(store_id),)).fetchone()
        if row: return dict(row)
        try: raw = get_store(store_id, db)
        except KeyError: raw = {}
        return _seed_brand_name_state(store_id, profile["profile"].get("brand_name"),
            explicit=bool(raw.get("brand_name") or raw.get("brand")), db=db)
    try: raw = get_store(store_id, db)
    except KeyError: raw = {}
    name = raw.get("brand_name") or raw.get("brand") or ""
    return _seed_brand_name_state(store_id, name, explicit=bool(name), db=db)


def migrate_locked_existing_brand_name(store_id, approved_name, *, confirmed=False, db=None):
    """Additive migration for a previously user-approved name; never edits Store JSON."""
    if confirmed is not True: raise PermissionError("기존 브랜드명 lock migration에는 명시적 확인이 필요합니다.")
    profile=get_brand_profile(store_id,db=db)
    if not profile or str(profile["profile"].get("brand_name") or "").casefold()!=str(approved_name).strip().casefold():
        raise ValueError("저장된 Brand Profile 이름이 지정한 승인 이름과 일치하지 않습니다.")
    now=_now();_install(db)
    with connect(db) as con:
        con.execute("""INSERT INTO brand_name_state(store_id,brand_name,name_status,locked_at,updated_at)
                      VALUES(?,?, 'LOCKED', ?, ?)
                      ON CONFLICT(store_id) DO UPDATE SET brand_name=excluded.brand_name,name_status='LOCKED',
                      locked_at=COALESCE(brand_name_state.locked_at,excluded.locked_at),updated_at=excluded.updated_at""",
                    (str(store_id),str(approved_name).strip(),now,now))
    return brand_name_state(store_id,db=db)


def brand_profile_from_store(store_id, *, overrides=None, db=None):
    """Seed a reusable profile from the existing Store Profile; never rename it."""
    store = get_store(store_id, db)
    existing = get_brand_profile(store_id, db=db)
    raw = dict(store)
    brand_name = (existing or {}).get("profile", {}).get("brand_name") or raw.get("brand_name") or raw.get("brand") or raw.get("store_name") or store_id
    sourcing = raw.get("sourcing") if isinstance(raw.get("sourcing"), dict) else {}
    category = raw.get("primary_category") or raw.get("category") or ", ".join(raw.get("sourcing_categories", []) or [])
    profile = {
        "store_id": store_id, "brand_name": str(brand_name), "tagline": raw.get("tagline"),
        "primary_category": str(category or "General merchandise"),
        "target_customer": raw.get("target_customer") or raw.get("audience") or "Online retail customers",
        "target_country": raw.get("target_country") or raw.get("market") or "United States",
        "brand_keywords": list(raw.get("brand_keywords") or raw.get("include_keywords") or []),
        "personality": raw.get("brand_personality") or raw.get("brand_voice") or ["clear", "useful", "trustworthy"],
        "colors": raw.get("brand_colors") or {"primary": "#24364B", "secondary": "#FFFFFF", "accent": "#D7C7A6"},
        "background_preference": raw.get("background_preference") or "white or transparent",
        "typography_style": raw.get("typography_style") or "clean sans serif",
        "logo_style": raw.get("logo_style") or "simple geometric symbol and horizontal wordmark",
        "icon_style": raw.get("icon_style") or "simple bold silhouette",
        "avoid_styles": list(raw.get("avoid_styles") or ["watermarks", "mockups", "3D", "thin details"]),
        "visual_identity_version": (existing or {}).get("version", 0) + 1,
    }
    requested_name = (overrides or {}).get("brand_name")
    current_state = brand_name_state(store_id, db=db) if existing else None
    if existing and current_state and current_state.get("name_status") == "LOCKED" and requested_name and requested_name != existing["profile"]["brand_name"]:
        raise PermissionError("브랜드명이 잠겨 있습니다. 명시적인 브랜드명 변경 절차를 먼저 시작하세요.")
    profile.update(overrides or {})
    # A selected/existing brand name is immutable unless the user explicitly overrides it.
    if existing and not (overrides or {}).get("brand_name"):
        profile["brand_name"] = existing["profile"]["brand_name"]
    identity_changed = bool(existing and any(existing["profile"].get(key) != profile.get(key)
                         for key in ("brand_name", "colors", "logo_style", "icon_style")))
    source_hash = _hash({"store": store, "overrides": overrides or {}})
    now = _now(); _install(db)
    with connect(db) as con:
        if existing:
            version = existing["version"] if existing["source_hash"] == source_hash else existing["version"] + 1
        else: version = 1
        profile["visual_identity_version"] = version
        con.execute("""INSERT INTO brand_profiles(store_id,version,profile_json,source_hash,approval_status,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET version=excluded.version,profile_json=excluded.profile_json,
          source_hash=excluded.source_hash,approval_status='DRAFT',approved_at=NULL,updated_at=excluded.updated_at""",
          (store_id, version, json.dumps(profile, ensure_ascii=False), source_hash, "DRAFT", now, now))
    _seed_brand_name_state(store_id, profile["brand_name"],
        explicit=bool(raw.get("brand_name") or raw.get("brand") or (existing and current_state and current_state.get("name_status") == "LOCKED")), db=db)
    if identity_changed: mark_brand_assets_stale(store_id, reason="BRAND_PROFILE_IDENTITY_CHANGED", db=db)
    root = _brand_root(store_id, version); root.mkdir(parents=True, exist_ok=True)
    (root / "brand_profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(store_id, f"profile-v{version}", db=db)
    return {"store_id": store_id, "version": version, "profile": profile, "approval_status": "DRAFT", "source_hash": source_hash}


def brand_name_prompt(store_id, *, db=None):
    profile = get_brand_profile(store_id, db=db)
    if not profile: profile = brand_profile_from_store(store_id, db=db)
    p = profile["profile"]
    return ("Return ONLY valid JSON matching this schema: {\"candidates\":[10 objects],\"shortlist\":[3 brand_name strings]}. "
            "Each candidate object must contain brand_name, pronunciation, meaning_and_rationale, brand_image, direction, review_status. "
            "Use review_status UNREVIEWED. The 3 shortlisted names must represent distinct strategic directions. "
            "Recommend exactly 10 distinctive English ecommerce brand names. Names must be short, pronounceable, "
            "memorable, extensible beyond one product, easy to spell, and less likely to be confused with famous brands. "
            "For each provide pronunciation, meaning/rationale, and brand image. End with 3 candidates representing "
            "different strategic directions. Do not claim trademark/domain clearance; state that both require review. "
            f"Store category: {p['primary_category']}; customers: {p['target_customer']}; country: {p['target_country']}; "
            f"personality/keywords: {p['personality']} / {p['brand_keywords']}. Desired image/keywords: {p.get('desired_brand_image', p['personality'])}. "
            f"Do not repeat the current name '{p['brand_name']}' among candidates; it must not be renamed or auto-selected. "
            "Do not claim trademark/domain clearance. Provide manual steps: search official trademark registers and domain registrars, "
            "record conflicts, and get qualified legal advice where needed.")


def suggest_brand_names(store_id, *, db=None):
    """Prompt-first: return only candidates imported/reviewed by the operator."""
    return list_brand_name_candidates(store_id, db=db)


def check_brand_name_conflicts(store_id, name, *, db=None):
    normalized = re.sub(r"\s+", " ", str(name or "").strip()).casefold()
    conflicts = []
    if not normalized: return {"status": "BASIC_CONFLICT_FOUND", "conflicts": ["empty name"]}
    _install(db)
    with connect(db) as con:
        rows = con.execute("SELECT store_id,store_name,profile_json FROM stores WHERE store_id<>?", (str(store_id),)).fetchall()
        profiles = con.execute("SELECT store_id,profile_json FROM brand_profiles WHERE store_id<>?", (str(store_id),)).fetchall()
    for row in rows:
        if str(row["store_name"] or "").strip().casefold() == normalized:
            conflicts.append({"kind": "OTHER_STORE_NAME", "store_id": row["store_id"]})
    for row in profiles:
        try: other = json.loads(row["profile_json"] or "{}")
        except (json.JSONDecodeError, TypeError): other = {}
        if str(other.get("brand_name") or "").strip().casefold() == normalized:
            conflicts.append({"kind": "PROJECT_BRAND_NAME", "store_id": row["store_id"]})
    return {"status": "BASIC_CONFLICT_FOUND" if conflicts else "BASIC_CHECK_PASS",
            "conflicts": conflicts, "legal_clearance": "NOT_CHECKED",
            "domain_review_required": True, "trademark_review_required": True}


def import_brand_name_candidates(store_id, payload, *, db=None):
    if isinstance(payload, str):
        try: payload = json.loads(payload)
        except json.JSONDecodeError as exc: raise ValueError("후보 JSON 형식을 확인하세요.") from exc
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    shortlist = payload.get("shortlist") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or len(candidates) != 10: raise ValueError("브랜드명 후보는 정확히 10개여야 합니다.")
    names = []
    normalized = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or any(not str(candidate.get(key) or "").strip() for key in BRAND_ASSIGNMENT_CONTRACT["candidate_fields"]):
            raise ValueError("각 후보에 이름·발음·의미·브랜드 이미지·전략 방향이 필요합니다.")
        if candidate.get("review_status") not in BRAND_ASSIGNMENT_CONTRACT["name_review_states"]:
            raise ValueError("후보 review_status 값이 계약에 없습니다.")
        candidate = dict(candidate); candidate["review_status"] = "UNREVIEWED"
        candidate["brand_name"] = str(candidate["brand_name"]).strip()
        names.append(candidate["brand_name"]); normalized.append(candidate)
    if len({name.casefold() for name in names}) != 10: raise ValueError("후보 이름은 서로 달라야 합니다.")
    profile=get_brand_profile(store_id,db=db)
    current_name=(profile or {}).get("profile",{}).get("brand_name")
    if current_name and current_name.casefold() in {name.casefold() for name in names}:
        raise ValueError("현재 확정된 브랜드명은 새 후보 목록에 넣지 마세요.")
    if not isinstance(shortlist, list) or len(shortlist) != 3 or len({str(x).casefold() for x in shortlist}) != 3:
        raise ValueError("서로 다른 3개 shortlist가 필요합니다.")
    lookup = {c["brand_name"].casefold(): c for c in normalized}
    if any(str(item).casefold() not in lookup for item in shortlist): raise ValueError("shortlist는 후보 10개 중에서 골라야 합니다.")
    directions = {lookup[str(item).casefold()]["direction"].casefold() for item in shortlist}
    if len(directions) != 3: raise ValueError("shortlist 3개는 서로 다른 전략 방향이어야 합니다.")
    _install(db); now = _now()
    checked = {candidate["brand_name"].casefold(): check_brand_name_conflicts(store_id, candidate["brand_name"], db=db)
               for candidate in normalized}
    with connect(db) as con:
        con.execute("DELETE FROM brand_name_candidates WHERE store_id=?", (str(store_id),))
        for candidate in normalized:
            conflict = checked[candidate["brand_name"].casefold()]
            candidate["review_status"] = conflict["status"]
            con.execute("INSERT INTO brand_name_candidates VALUES(?,?,?,?,?,?,?)", (str(store_id), candidate["brand_name"].casefold(),
                json.dumps(candidate, ensure_ascii=False), conflict["status"], json.dumps(conflict["conflicts"], ensure_ascii=False),
                int(candidate["brand_name"].casefold() in {str(x).casefold() for x in shortlist}), now))
    return list_brand_name_candidates(store_id, db=db)


def list_brand_name_candidates(store_id, *, db=None):
    _install(db)
    with connect(db) as con: rows = con.execute("SELECT candidate_json,review_status,conflicts_json,shortlisted FROM brand_name_candidates WHERE store_id=? ORDER BY created_at,candidate_key", (str(store_id),)).fetchall()
    result=[]
    for row in rows:
        candidate=json.loads(row["candidate_json"]);candidate["review_status"]=row["review_status"]
        candidate["conflicts"]=json.loads(row["conflicts_json"] or "[]");candidate["shortlisted"]=bool(row["shortlisted"]);result.append(candidate)
    return result


def lock_brand_name(store_id, brand_name, *, confirmed=False, clearance_reviewed=False, db=None):
    if confirmed is not True: raise PermissionError("브랜드명 잠금에는 명시적 확인이 필요합니다.")
    profile=get_brand_profile(store_id,db=db) or brand_profile_from_store(store_id,db=db)
    current=brand_name_state(store_id,db=db)
    if current.get("name_status")=="LOCKED" and str(current.get("brand_name") or "").casefold()!=str(brand_name).strip().casefold():
        raise PermissionError("기존 브랜드명이 잠겨 있습니다. 먼저 명시적으로 이름 변경 절차를 시작하세요.")
    candidate=next((c for c in list_brand_name_candidates(store_id,db=db) if c["brand_name"].casefold()==str(brand_name).strip().casefold()),None)
    if not candidate: raise ValueError("가져온 후보에서 브랜드명을 선택해야 합니다.")
    if candidate["review_status"]=="BASIC_CONFLICT_FOUND": raise ValueError("기본 충돌을 먼저 검토해야 합니다.")
    if not clearance_reviewed: raise ValueError("도메인·상표는 사용자가 별도로 조사했음을 확인해야 합니다.")
    data=dict(profile["profile"]);data["brand_name"]=candidate["brand_name"]
    now=_now()
    with connect(db) as con:
        con.execute("UPDATE brand_profiles SET profile_json=?,version=version+1,approval_status='DRAFT',approved_at=NULL,updated_at=? WHERE store_id=?",
                    (json.dumps(data,ensure_ascii=False),now,str(store_id)))
        con.execute("""INSERT INTO brand_name_state VALUES(?,?,?,?,?)
                    ON CONFLICT(store_id) DO UPDATE SET brand_name=excluded.brand_name,
                    name_status='LOCKED',locked_at=excluded.locked_at,updated_at=excluded.updated_at""",
                    (str(store_id),candidate["brand_name"],"LOCKED",now,now))
        con.execute("UPDATE brand_name_candidates SET review_status='USER_APPROVED' WHERE store_id=? AND candidate_key=?",(str(store_id),candidate["brand_name"].casefold()))
    mark_brand_assets_stale(store_id, reason="BRAND_NAME_CHANGED", db=db)
    return get_brand_profile(store_id,db=db)


def begin_brand_name_change(store_id, *, confirmed=False, db=None):
    if not confirmed: raise PermissionError("브랜드명 변경 절차 시작에 명시적 확인이 필요합니다.")
    with connect(db) as con:
        row=con.execute("SELECT brand_name FROM brand_name_state WHERE store_id=?",(str(store_id),)).fetchone()
        if not row: raise KeyError(store_id)
        con.execute("UPDATE brand_name_state SET name_status='REVIEW_REQUIRED',locked_at=NULL,updated_at=? WHERE store_id=?",(_now(),str(store_id)))
    return {"store_id":str(store_id),"previous_name":row["brand_name"],"name_status":"REVIEW_REQUIRED","assets_preserved":True}


def logo_mark_prompt(profile):
    p = profile.get("profile", profile)
    return (f"Create one isolated, simple geometric brand symbol for an ecommerce brand in {p['primary_category']}. "
            f"Brand personality: {p['personality']}. Palette: {p['colors']}. Style: {p['logo_style']}. "
            f"Avoid: {p['avoid_styles']}. Strong silhouette and contrast at small sizes, balanced negative space. "
            "NO TEXT, NO LETTERS, no brand name, no mockup, no packaging, no sign, no 3D, no watermark, no thin details. "
            "Square canvas, white or transparent background, centered isolated mark.")


def logo_prompt(profile):
    p = profile.get("profile", profile)
    return ("Create a simple, distinctive LOGO MARK only for a Shopify brand. The final ShopSource composition will pair this mark "
            f"with the exact, separately typeset brand name '{p['brand_name']}' in a horizontal header lockup. "
            f"Category: {p['primary_category']}. Audience: {p['target_customer']}. Brand image: {p.get('desired_brand_image', p['personality'])}. "
            f"Personality: {p['personality']}. Preferred colors: {p['colors']}. Avoid colors: {p.get('avoid_colors', [])}. "
            f"Avoid styles: {p['avoid_styles']}. Clean geometric silhouette; readable at small mobile-header size, generous safe padding and negative space. "
            "No text or generated lettering, no product illustration as the main mark, no 3D, no watermark, no mockup, no busy pattern, "
            "no tiny decoration, no thin lines. Transparent or plain background. The exact wordmark must be typeset deterministically by the application.")


def favicon_prompt(profile, *, source_logo_mark_id=None, selected_initial=None):
    p = profile.get("profile", profile)
    source = (f"Use approved LOGO_MARK asset {source_logo_mark_id}, preserving its exact identity and palette."
              if source_logo_mark_id else
              (f"The user explicitly selected initial '{selected_initial}'; use only that initial in a bold typographic treatment. Do not invent a symbol."
               if selected_initial in {"C", "CT"} else
               "The approved logo mark or user-selected C/CT initial is not available yet. Do not generate an image; wait until the user chooses one."))
    return (f"Prepare a favicon for '{p['brand_name']}' from its existing identity. {source} "
            "Square and centered with generous safe margin, strong silhouette and contrast at 32x32 pixels. "
            "Do not use the full brand name or horizontal wordmark, small text, thin lines, complex pattern, watermark, or unrelated logo. "
            "Keep the approved palette. A person must visually review the result.")


def export_brand_prompts(store_id, *, db=None):
    profile = get_brand_profile(store_id, db=db) or brand_profile_from_store(store_id, db=db)
    root = _brand_root(store_id, profile["version"]); root.mkdir(parents=True, exist_ok=True)
    name_path, logo_path, favicon_path = root / "brand_name_prompt.txt", root / "logo_prompt.txt", root / "favicon_prompt.txt"
    name_path.write_text(brand_name_prompt(store_id, db=db), encoding="utf-8")
    logo_path.write_text(logo_prompt(profile), encoding="utf-8")
    marks = [a for a in list_brand_assets(store_id, db=db) if a["asset_type"] == "LOGO_MARK" and a["approval_status"] == "APPROVED"]
    prompt = favicon_prompt(profile, source_logo_mark_id=marks[-1]["asset_id"]) if marks else favicon_prompt(profile)
    favicon_path.write_text(prompt, encoding="utf-8")
    return {"brand_name_prompt": str(name_path), "logo_prompt": str(logo_path), "favicon_prompt": str(favicon_path)}


def logo_preview_report(asset):
    _require_pillow()
    with Image.open(asset["local_path"]) as image:
        rgba = image.convert("RGBA"); bbox = rgba.getchannel("A").getbbox()
        if not bbox: raise ValueError("Logo image has no visible content")
        width, height = rgba.size
        padding = min(bbox[0], bbox[1], width-bbox[2], height-bbox[3]) / max(1, min(width, height))
        return {"full_size": [width, height], "desktop_header_width": 300, "mobile_header_width": 168,
                "light_preview": True, "dark_preview": True, "content_bounds": list(bbox),
                "safe_padding_ratio": round(padding, 4), "technical_status": "TECHNICAL_PASS" if padding >= .025 else "NEEDS_VISUAL_REVIEW",
                "visual_approval_required": True}


def mark_brand_assets_stale(store_id, *, reason="BRAND_IDENTITY_CHANGED", db=None):
    """Preserve old assets while explicitly requiring review of derived favicons."""
    _install(db)
    with connect(db) as con:
        rows = con.execute("SELECT asset_id,metadata_json FROM brand_assets WHERE store_id=? AND asset_type IN ('FAVICON_MASTER','FAVICON_32','FAVICON_64')", (str(store_id),)).fetchall()
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            metadata.update({"identity_status": "STALE_IDENTITY_REVIEW_REQUIRED", "stale_reason": reason})
            con.execute("UPDATE brand_assets SET metadata_json=?,approval_status=CASE WHEN approval_status='APPROVED' THEN 'NEEDS_REVIEW' ELSE approval_status END WHERE asset_id=?",
                        (json.dumps(metadata, ensure_ascii=False), row["asset_id"]))
    return len(rows)


def _next_version(store_id, asset_type, db):
    _install(db)
    with connect(db) as con:
        row = con.execute("SELECT MAX(version) n FROM brand_assets WHERE store_id=? AND asset_type=?", (store_id, asset_type)).fetchone()
    return int(row["n"] or 0) + 1


def _register_asset(store_id, asset_type, path, *, provider="MANUAL", model="", prompt="", source_asset_id=None,
                    transparent=False, status="NEEDS_REVIEW", metadata=None, db=None):
    target = Path(path).resolve()
    if asset_type not in ASSET_KINDS: raise ValueError("Unsupported brand asset type")
    if target.suffix.lower()==".svg":
        text=target.read_text(encoding="utf-8")
        if "<svg" not in text[:1000].lower():raise ValueError("Invalid SVG image")
        match_w=re.search(r"\bwidth=['\"](\d+)",text[:3000]);match_h=re.search(r"\bheight=['\"](\d+)",text[:3000])
        width,height=int(match_w.group(1)) if match_w else 0,int(match_h.group(1)) if match_h else 0;fmt="SVG"
    else:
        _require_pillow()
        with Image.open(target) as im: im.verify()
        with Image.open(target) as im: width,height,fmt=im.width,im.height,(im.format or target.suffix.lstrip(".")).upper()
    content = target.read_bytes(); digest = hashlib.sha256(content).hexdigest(); version = _next_version(store_id, asset_type, db)
    profile = get_brand_profile(store_id, db=db) or brand_profile_from_store(store_id, db=db)
    asset_metadata=dict(metadata or {})
    asset_metadata.setdefault("brand_profile_version",profile["version"])
    root = _brand_root(store_id, profile["version"]); root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{asset_type.lower()}_v{version}.{target.suffix.lstrip('.').lower()}"
    if destination != target.resolve(): shutil.copyfile(target, destination)
    asset_id = "BRA_" + secrets.token_hex(10); now = _now()
    with connect(db) as con:
        con.execute("""INSERT INTO brand_assets(asset_id,store_id,asset_type,version,provider,provider_model,prompt_hash,source_asset_id,
          width,height,format,transparent_background,local_path,sha256,approval_status,generation_status,metadata_json,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (asset_id,store_id,asset_type,version,provider,model,
          _hash(prompt) if prompt else "",source_asset_id,width,height,fmt,int(transparent),str(destination),digest,status,"COMPLETE",
          json.dumps(asset_metadata,ensure_ascii=False),now))
    asset=get_brand_asset(asset_id, db=db);_write_report(store_id,asset_id,db=db);return asset


def get_brand_asset(asset_id, *, db=None):
    _install(db)
    with connect(db) as con: row=con.execute("SELECT * FROM brand_assets WHERE asset_id=?",(asset_id,)).fetchone()
    if not row:return None
    result=dict(row);result["metadata"]=json.loads(result.pop("metadata_json"));return result


def list_brand_assets(store_id, *, db=None):
    _install(db)
    with connect(db) as con: ids=[row[0] for row in con.execute("SELECT asset_id FROM brand_assets WHERE store_id=? ORDER BY created_at,asset_type,version",(store_id,))]
    return [get_brand_asset(asset_id,db=db) for asset_id in ids]


def brand_identity_evidence(store_id, *, remote=None, db=None):
    """Compact, conservative production evidence for name/logo/favicon readiness."""
    profile=get_brand_profile(store_id,db=db) or brand_profile_from_store(store_id,db=db)
    name=brand_name_state(store_id,db=db)
    assets=list_brand_assets(store_id,db=db)
    current_mark=next((a for a in reversed(assets) if a["asset_type"]=="LOGO_MARK" and a["approval_status"]=="APPROVED"),None)
    remote=remote or {}
    result={"brand_name":profile["profile"].get("brand_name"),
            "name_status":"LOCKED" if name.get("name_status")=="LOCKED" else "REVIEW_REQUIRED",
            "logo":"MISSING","favicon":"MISSING","navigation":"UNKNOWN"}
    for kind,asset_type in (("logo","LOGO_HORIZONTAL"),("favicon","FAVICON_32")):
        candidates=[a for a in assets if a["asset_type"]==asset_type]
        approved=next((a for a in reversed(candidates) if a["approval_status"]=="APPROVED"),None)
        if not approved:
            result[kind]="NEEDS_REVIEW" if candidates else "MISSING"
            continue
        metadata=approved.get("metadata") or {}
        stale=(metadata.get("identity_status")=="STALE_IDENTITY_REVIEW_REQUIRED" or
               metadata.get("brand_profile_version") not in {None,profile["version"]} or
               (kind=="favicon" and metadata.get("source_logo_mark_id") and current_mark and
                metadata.get("source_logo_mark_id")!=current_mark["asset_id"]))
        if stale:
            result[kind]="NEEDS_REVIEW";continue
        current=remote.get(kind)
        if isinstance(current,dict): current=current.get("url") or current.get("src") or current.get("originalSrc")
        if current and approved.get("shopify_url") and str(current).rstrip("/")==str(approved["shopify_url"]).rstrip("/"):
            result[kind]="VERIFIED_REMOTE"
        elif approved.get("shopify_file_id") and approved.get("shopify_url"):
            result[kind]="APPLIED"
        else:
            result[kind]="APPROVED_LOCAL"
    return result


def generate_logo_mark(store_id, *, provider, enabled=False, model=None, db=None):
    if not enabled: raise RuntimeError("이미지 자동 생성 사용 opt-in이 필요합니다.")
    from .collection_images import OpenAIImagesProvider
    if not isinstance(provider, OpenAIImagesProvider) and not callable(getattr(provider,"generate",None)):
        raise ValueError("지원하지 않는 브랜드 이미지 provider")
    prof=get_brand_profile(store_id,db=db) or brand_profile_from_store(store_id,db=db)
    prompt=logo_mark_prompt(prof); version=_next_version(store_id,"LOGO_MARK",db)
    root=_brand_root(store_id,prof["version"]);root.mkdir(parents=True,exist_ok=True)
    output=root/f"logo_mark_v{version}.png"
    result=provider.generate(prompt,"1024x1024",output,**({"enabled":True} if isinstance(provider,OpenAIImagesProvider) else {}))
    asset=_register_asset(store_id,"LOGO_MARK",result["path"],provider=getattr(provider,"provider_name","CUSTOM"),
      model=model or getattr(provider,"model_version",""),prompt=prompt,transparent=True,status="NEEDS_REVIEW",metadata=result.get("metadata"),db=db)
    (root/"logo_prompt.txt").write_text(logo_prompt(prof)+"\n\nMARK PROMPT:\n"+prompt,encoding="utf-8")
    return asset


def _font(size):
    _require_pillow()
    candidates=[os.environ.get("SHOPSource_BRAND_FONT"),r"C:\Windows\Fonts\arial.ttf",r"C:\Windows\Fonts\segoeuib.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:return ImageFont.truetype(candidate,size),Path(candidate).stem
            except OSError:pass
    return ImageFont.load_default(),"PillowDefault"


def compose_horizontal_logo(store_id, mark_asset_id, *, db=None):
    _require_pillow()
    mark=get_brand_asset(mark_asset_id,db=db)
    if not mark or mark["asset_type"]!="LOGO_MARK" or mark["approval_status"] not in {"GENERATED","NEEDS_REVIEW","APPROVED"}:
        raise ValueError("A reviewed LOGO_MARK asset is required")
    prof=get_brand_profile(store_id,db=db); name=prof["profile"]["brand_name"]
    with Image.open(mark["local_path"]) as src:
        icon=src.convert("RGBA"); icon.thumbnail((300,300),Image.Resampling.LANCZOS)
    colors=prof["profile"].get("colors") or {}; ink=colors.get("primary","#24364B")
    canvas=Image.new("RGBA",(1200,360),(255,255,255,0)); canvas.alpha_composite(icon,(35,(360-icon.height)//2))
    draw=ImageDraw.Draw(canvas);font,font_name=_font(116)
    while draw.textbbox((0,0),name,font=font)[2]>785 and getattr(font,"size",0)>28:
        font,font_name=_font(getattr(font,"size",116)-4)
    draw.text((375,118),name,font=font,fill=ink,stroke_width=0)
    version=_next_version(store_id,"LOGO_HORIZONTAL",db);root=_brand_root(store_id,prof["version"]);root.mkdir(parents=True,exist_ok=True)
    path=root/f"logo_horizontal_v{version}.png";canvas.save(path,optimize=True)
    asset=_register_asset(store_id,"LOGO_HORIZONTAL",path,provider="COMPOSITOR",model="Pillow",source_asset_id=mark_asset_id,
      transparent=True,status="NEEDS_REVIEW",metadata={"font_family":font_name,"exact_wordmark":name,"layout":"horizontal",
          "layout_geometry":{"canvas":[1200,360],"mark_bounds":[35,(360-icon.height)//2,35+icon.width,(360+icon.height)//2],
                              "wordmark_origin":[375,118],"minimum_mark_wordmark_gap":40,"right_padding_min":35}},db=db)
    svg=root/f"logo_horizontal_v{version}.svg";png64=base64.b64encode(Path(mark["local_path"]).read_bytes()).decode("ascii")
    svg.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="360" viewBox="0 0 1200 360"><image href="data:image/png;base64,{png64}" x="35" y="30" width="300" height="300"/><text x="375" y="235" font-family="{html.escape(font_name)}" font-size="116" fill="{html.escape(ink)}">{html.escape(name)}</text></svg>',encoding="utf-8")
    validate_logo_horizontal(asset)
    return asset


def validate_logo_horizontal(asset):
    _require_pillow()
    path=Path(asset["local_path"])
    with Image.open(path) as image:
        if image.width<600 or image.height<120 or image.width/image.height<2: raise ValueError("Horizontal logo dimensions/ratio are too small")
        if image.getbbox() is None: raise ValueError("Logo is empty")
    if path.stat().st_size>12*1024*1024: raise ValueError("Logo exceeds 12 MB")
    report = logo_preview_report(asset)
    geometry=(asset.get("metadata") or {}).get("layout_geometry") or {}
    mark_bounds=geometry.get("mark_bounds")
    wordmark_origin=geometry.get("wordmark_origin")
    collision=bool(mark_bounds and wordmark_origin and mark_bounds[2] > wordmark_origin[0])
    report["icon_wordmark_collision"] = collision
    report["canvas_overflow"] = bool(report["content_bounds"][0] < 0 or report["content_bounds"][1] < 0 or
                                     report["content_bounds"][2] > asset["width"] or report["content_bounds"][3] > asset["height"])
    if collision or report["canvas_overflow"]: report["technical_status"]="NEEDS_VISUAL_REVIEW"
    return {"valid":True,"width":asset["width"],"height":asset["height"],"sha256":asset["sha256"], **report}


def derive_favicon(store_id, mark_asset_id, *, transparent_white=False, db=None):
    _require_pillow()
    mark=get_brand_asset(mark_asset_id,db=db)
    if not mark or mark["asset_type"]!="LOGO_MARK" or mark["approval_status"]!="APPROVED":
        raise ValueError("Favicon source must be an APPROVED LOGO_MARK")
    if Path(mark["local_path"]).suffix.lower()==".svg":raise ValueError("SVG mark must be converted to PNG before deterministic favicon creation")
    profile=get_brand_profile(store_id,db=db);colors=profile["profile"].get("colors") or {}
    with Image.open(mark["local_path"]) as source:
        icon=source.convert("RGBA")
    alpha=icon.getchannel("A");bbox=alpha.getbbox()
    if bbox: icon=icon.crop(bbox)
    side=max(icon.size); square=Image.new("RGBA",(side,side),(255,255,255,0));square.alpha_composite(icon,((side-icon.width)//2,(side-icon.height)//2))
    master=Image.new("RGBA",(1024,1024),(255,255,255,0))
    square.thumbnail((696,696),Image.Resampling.LANCZOS)
    master.alpha_composite(square,((1024-square.width)//2,(1024-square.height)//2))
    if transparent_white:
        pixels=master.load()
        for y in range(master.height):
            for x in range(master.width):
                r,g,b,a=pixels[x,y]
                if r>=248 and g>=248 and b>=248:pixels[x,y]=(r,g,b,0)
    version=_next_version(store_id,"FAVICON_MASTER",db);root=_brand_root(store_id,profile["version"]);root.mkdir(parents=True,exist_ok=True)
    master_path=root/f"favicon_master_v{version}.png";master.save(master_path,optimize=True)
    master_asset=_register_asset(store_id,"FAVICON_MASTER",master_path,provider="DERIVED",model="Pillow-Lanczos",source_asset_id=mark_asset_id,
        transparent=transparent_white,status="NEEDS_REVIEW",metadata={"palette":colors,"palette_snapshot":colors,"padding_ratio":.16,
            "source_logo_mark_id":mark_asset_id,"brand_profile_version":profile["version"],"source_sha256":mark["sha256"],
            "identity_status":"CURRENT"},db=db)
    small=master.resize((32,32),Image.Resampling.LANCZOS)
    path=root/f"favicon_32_v{_next_version(store_id,'FAVICON_32',db)}.png";small.save(path,optimize=True)
    asset=_register_asset(store_id,"FAVICON_32",path,provider="DERIVED",model="Pillow-Lanczos",source_asset_id=master_asset["asset_id"],
       transparent=transparent_white,status="NEEDS_REVIEW",metadata={"palette":colors,"source_mark_id":mark_asset_id,
           "source_logo_mark_id":mark_asset_id,"brand_profile_version":profile["version"],"palette_snapshot":colors,
           "source_sha256":mark["sha256"],"identity_status":"CURRENT"},db=db)
    icon64=master.resize((64,64),Image.Resampling.LANCZOS);path64=root/f"favicon_64_v{_next_version(store_id,'FAVICON_64',db)}.png";icon64.save(path64,optimize=True)
    _register_asset(store_id,"FAVICON_64",path64,provider="DERIVED",model="Pillow-Lanczos",source_asset_id=master_asset["asset_id"],
       transparent=transparent_white,status="NEEDS_REVIEW",metadata={"palette":colors,"palette_snapshot":colors,
           "source_logo_mark_id":mark_asset_id,"brand_profile_version":profile["version"],"source_sha256":mark["sha256"],
           "identity_status":"CURRENT"},db=db)
    (root/"favicon_prompt.txt").write_text(favicon_prompt(profile["profile"])+"\n",encoding="utf-8")
    validate_favicon(asset)
    return {"master":master_asset,"favicon_32":asset}


def validate_favicon(asset):
    _require_pillow()
    with Image.open(asset["local_path"]) as image:
        if image.size!=(32,32) or image.format!="PNG":raise ValueError("Favicon must be exactly 32x32 PNG")
        alpha_bbox=image.getchannel("A").getbbox()
        if alpha_bbox is None:raise ValueError("Favicon is fully transparent")
        alpha=image.getchannel("A"); content=Image.new("L",(32,32),0)
        content.paste(255,(0,0,32,32),alpha)
        bbox=alpha_bbox
        if not bbox:raise ValueError("Favicon is empty")
        left,top,right,bottom=bbox
        padding=min(left,top,32-right,32-bottom)/32
        coverage=((right-left)*(bottom-top))/1024
        touches_edge=left==0 or top==0 or right==32 or bottom==32
        if touches_edge:status="NEEDS_VISUAL_REVIEW"
        elif padding < .06 or coverage < .12 or coverage > .88:status="NEEDS_VISUAL_REVIEW"
        else:status="TECHNICAL_PASS"
    meta=asset.get("metadata") or {}
    stale=meta.get("identity_status")=="STALE_IDENTITY_REVIEW_REQUIRED"
    return {"valid":True,"width":32,"height":32,"format":"PNG","content_bbox":list(bbox),
            "edge_touch":touches_edge,"safe_padding_ratio":round(padding,4),"coverage_ratio":round(coverage,4),
            "technical_status":status,"approval_status":"NEEDS_REVIEW" if status!="TECHNICAL_PASS" or stale else asset.get("approval_status"),
            "identity_status":meta.get("identity_status","UNKNOWN"),"visual_review_required":True}


def derive_initial_favicon(store_id, initial, *, confirmed=False, db=None):
    """Create deterministic text-only initial favicon, only after explicit user choice."""
    if confirmed is not True: raise PermissionError("파비콘 이니셜은 사용자가 명시적으로 선택해야 합니다.")
    initial=str(initial or "").strip().upper()
    if initial not in {"C","CT"}: raise ValueError("허용되는 이니셜 예시는 C 또는 CT입니다.")
    _require_pillow(); profile=get_brand_profile(store_id,db=db) or brand_profile_from_store(store_id,db=db)
    p=profile["profile"]; colors=p.get("colors") or {}; fill=colors.get("primary","#24364B")
    image=Image.new("RGBA",(32,32),(0,0,0,0));draw=ImageDraw.Draw(image);font,_=_font(21 if len(initial)==1 else 15)
    bbox=draw.textbbox((0,0),initial,font=font); x=(32-(bbox[2]-bbox[0]))//2-bbox[0];y=(32-(bbox[3]-bbox[1]))//2-bbox[1]
    draw.text((x,y),initial,font=font,fill=fill)
    root=_brand_root(store_id,profile["version"]);root.mkdir(parents=True,exist_ok=True)
    path=root/f"favicon_initial_{initial.lower()}_v{_next_version(store_id,'FAVICON_32',db)}.png";image.save(path,optimize=True)
    asset=_register_asset(store_id,"FAVICON_32",path,provider="DETERMINISTIC_INITIAL",model="Pillow",status="NEEDS_REVIEW",
        transparent=True,metadata={"selected_initial":initial,"brand_profile_version":profile["version"],"palette_snapshot":colors,
                                   "source_sha256":None,"source_logo_mark_id":None,"identity_status":"CURRENT",
                                   "visual_review_required":True},db=db)
    validate_favicon(asset)
    return asset


def approve_asset(asset_id, *, db=None):
    asset=get_brand_asset(asset_id,db=db)
    if not asset:raise KeyError(asset_id)
    if Path(asset["local_path"]).suffix.lower()!=".svg":
        _require_pillow()
        with Image.open(asset["local_path"]) as image:image.verify()
    if asset["asset_type"]=="FAVICON_32":
        validation=validate_favicon(asset)
        if validation.get("identity_status")=="STALE_IDENTITY_REVIEW_REQUIRED":
            raise ValueError("로고/브랜드 identity가 바뀌었습니다. 현재 identity에서 파비콘을 다시 준비하세요.")
    replaced_approved = False
    with connect(db) as con:
        if asset["asset_type"] == "LOGO_MARK":
            replaced_approved = con.execute("SELECT 1 FROM brand_assets WHERE store_id=? AND asset_type='LOGO_MARK' AND approval_status='APPROVED' AND asset_id<>? LIMIT 1",
                                            (asset["store_id"], asset_id)).fetchone() is not None
        con.execute("UPDATE brand_assets SET approval_status='SUPERSEDED' WHERE store_id=? AND asset_type=? AND approval_status='APPROVED'",(asset["store_id"],asset["asset_type"]))
        con.execute("UPDATE brand_assets SET approval_status='APPROVED' WHERE asset_id=?",(asset_id,))
    if asset["asset_type"] == "LOGO_MARK" and replaced_approved:
        mark_brand_assets_stale(asset["store_id"], reason="SOURCE_LOGO_MARK_SUPERSEDED", db=db)
    return get_brand_asset(asset_id,db=db)


def approve_brand_profile(store_id, *, db=None):
    _install(db)
    with connect(db) as con:
        result=con.execute("UPDATE brand_profiles SET approval_status='APPROVED',approved_at=?,updated_at=? WHERE store_id=?",(_now(),_now(),store_id)).rowcount
    if not result:raise KeyError(store_id)
    return get_brand_profile(store_id,db=db)


def register_manual_asset(store_id, asset_type, source_path, *, db=None):
    path=Path(source_path).expanduser().resolve()
    allowed={"LOGO_MARK":{".png",".jpg",".jpeg",".svg"},"LOGO_HORIZONTAL":{".png",".jpg",".jpeg",".svg"},"FAVICON_32":{".png"}}
    if path.suffix.lower() not in allowed.get(asset_type,set()):raise ValueError("지원하지 않는 과제 자산 형식입니다.")
    if path.suffix.lower()==".svg":
        return _register_asset(store_id,asset_type,path,provider="MANUAL",model="assignment-svg",status="NEEDS_REVIEW",
                               metadata={"assignment_manual_mode":True,"svg_requires_shopify_conversion":True},db=db)
    _require_pillow()
    with Image.open(path) as image: image.verify()
    transparent=False
    if path.suffix.lower()==".png":
        with Image.open(path) as image:transparent="A" in image.getbands() and image.getchannel("A").getextrema()[0]<255
    profile=get_brand_profile(store_id,db=db) or brand_profile_from_store(store_id,db=db)
    mark=next((a for a in reversed(list_brand_assets(store_id,db=db)) if a["asset_type"]=="LOGO_MARK" and a["approval_status"]=="APPROVED"),None)
    metadata={"assignment_manual_mode":True}
    if asset_type=="FAVICON_32":
        metadata.update({"source_logo_mark_id":mark["asset_id"] if mark else None,
                         "brand_profile_version":profile["version"],"palette_snapshot":profile["profile"].get("colors") or {},
                         "source_sha256":mark["sha256"] if mark else None,"identity_status":"CURRENT",
                         "visual_review_required":True,"source_kind":"MANUAL_UPLOAD"})
    return _register_asset(store_id,asset_type,path,provider="MANUAL",model="assignment-upload",status="NEEDS_REVIEW",
        transparent=transparent,metadata=metadata,source_asset_id=mark["asset_id"] if asset_type=="FAVICON_32" and mark else None,db=db)


def register_manual_asset_bytes(store_id, asset_type, filename, content, *, db=None):
    suffix=Path(filename).suffix.lower()
    allowed={"LOGO_MARK":{".png",".jpg",".jpeg",".svg"},"LOGO_HORIZONTAL":{".png",".jpg",".jpeg",".svg"},"FAVICON_32":{".png"}}
    if suffix not in allowed.get(asset_type,set()):raise ValueError("로고는 PNG/JPG/SVG, 파비콘은 PNG 파일이어야 합니다.")
    if not content or len(content)>20*1024*1024:raise ValueError("파일이 비어 있거나 20MB 제한을 초과했습니다.")
    profile=get_brand_profile(store_id,db=db) or brand_profile_from_store(store_id,db=db)
    root=_brand_root(store_id,profile["version"]);root.mkdir(parents=True,exist_ok=True)
    temporary=root/("assignment_"+secrets.token_hex(6)+suffix);temporary.write_bytes(content)
    try:
        asset=register_manual_asset(store_id,asset_type,temporary,db=db)
        if asset["asset_type"]=="FAVICON_32":validate_favicon(asset)
        return asset
    finally:
        temporary.unlink(missing_ok=True)


def validate_manual_assets(store_id, *, db=None):
    results=[]
    for asset in list_brand_assets(store_id,db=db):
        if asset["provider"]!="MANUAL":continue
        try:
            if Path(asset["local_path"]).suffix.lower()==".svg":
                if "<svg" not in Path(asset["local_path"]).read_text(encoding="utf-8")[:1000].lower():raise ValueError("invalid SVG")
            else:
                _require_pillow()
                with Image.open(asset["local_path"]) as image:image.verify()
            result={"asset_id":asset["asset_id"],"valid":True,"width":asset["width"],"height":asset["height"],"format":asset["format"]}
            if asset["asset_type"]=="FAVICON_32":result.update(validate_favicon(asset))
        except Exception:result={"asset_id":asset["asset_id"],"valid":False,"error":"이미지 파일을 읽을 수 없습니다."}
        results.append(result)
    return results


def _theme_file_text(body):
    if body.get("content") is not None:return body["content"]
    encoded=body.get("contentBase64")
    if encoded:
        try:return base64.b64decode(encoded).decode("utf-8")
        except Exception:return None
    return None


def detect_brand_settings(schema, data):
    try:groups=json.loads(schema) if isinstance(schema,str) else schema
    except Exception:groups=[]
    try:settings_data=json.loads(data) if isinstance(data,str) else data
    except Exception:settings_data={}
    candidates={"logo":[],"favicon":[]}
    for group in groups if isinstance(groups,list) else []:
        for field in group.get("settings",[]):
            if field.get("type")!="image_picker":continue
            ident=str(field.get("id", ""));label=str(field.get("label", ""));words=f"{ident} {label}".casefold()
            for kind,terms in (("logo",("logo","brand mark")),("favicon",("favicon","site icon","browser icon"))):
                if not any(term in words for term in terms):continue
                score=1.0 if kind in ident.casefold() or label.casefold()==kind else .78
                candidates[kind].append({"setting_id":ident,"label":label,"confidence":score,"section":group.get("name")})
    current=(settings_data.get("current") or {}).get("settings") or {}
    result={}
    for kind,rows in candidates.items():
        rows.sort(key=lambda item:(-item["confidence"],item["setting_id"]))
        best=rows[0] if rows else None
        result[kind]={"status":"NOT_FOUND" if not best else "FOUND_HIGH_CONFIDENCE" if best["confidence"]>=.9 else "FOUND_REVIEW_REQUIRED",
                      "mapping":best,"current":current.get(best["setting_id"]) if best else None}
    return result


class BrandThemeService:
    def __init__(self, *, db=None, client_factory=ShopifyGraphQLClient, uploader_factory=ShopifyFileUploader):
        self.db,self.client_factory,self.uploader_factory=db,client_factory,uploader_factory

    def discover(self, store_id):
        config=get_connection(store_id,db=self.db);token,_=get_shopify_token(store_id,db=self.db)
        if not config or not token:return {"status":"MANUAL_ACTION_REQUIRED","reason":"Shopify 연결/credential 없음","write_themes":False}
        client=self.client_factory(config["shop_domain"],token,config["api_version"])
        scopes={row["handle"] for row in (client.execute("query BrandScopes { currentAppInstallation { accessScopes { handle } } }").get("currentAppInstallation") or {}).get("accessScopes",[])}
        if "read_themes" not in scopes:return {"status":"MANUAL_ACTION_REQUIRED","reason":"read_themes 권한이 없습니다.","scopes":sorted(scopes),"write_themes":"write_themes" in scopes}
        themes=client.execute(THEMES_QUERY).get("themes",{}).get("nodes",[]);theme=next((row for row in themes if row.get("role")=="MAIN"),None)
        if not theme:return {"status":"MANUAL_ACTION_REQUIRED","reason":"Published theme을 찾을 수 없습니다.","scopes":sorted(scopes),"write_themes":False}
        payload=client.execute(THEME_FILES_QUERY,{"id":theme["id"]}).get("theme") or {}
        files={row["filename"]:_theme_file_text(row.get("body") or {}) for row in (payload.get("files") or {}).get("nodes",[])}
        detection=detect_brand_settings(files.get("config/settings_schema.json") or "[]",files.get("config/settings_data.json") or "{}")
        return {"status":"CONNECTED","theme":theme,"scopes":sorted(scopes),"write_themes":"write_themes" in scopes,
                "settings_schema":files.get("config/settings_schema.json"),"settings_data":files.get("config/settings_data.json"),"detection":detection,"client":client}

    def upload_approved_asset(self, store_id, asset_id, *, uploader=None):
        asset=get_brand_asset(asset_id,db=self.db)
        if not asset or asset["store_id"]!=store_id or asset["approval_status"]!="APPROVED":raise RuntimeError("승인된 현재 스토어 asset만 업로드할 수 있습니다.")
        if not Path(asset["local_path"]).is_file() or Path(asset["local_path"]).suffix.lower()==".svg":raise RuntimeError("업로드할 PNG/JPEG asset이 없습니다.")
        config=get_connection(store_id,db=self.db);token,_=get_shopify_token(store_id,db=self.db)
        if not config or not token:raise RuntimeError("Shopify credential/연결을 확인하세요.")
        if uploader is None:
            client=self.client_factory(config["shop_domain"],token,config["api_version"]);uploader=self.uploader_factory(client)
            scopes={row["handle"] for row in (client.execute("query BrandFileScopes { currentAppInstallation { accessScopes { handle } } }").get("currentAppInstallation") or {}).get("accessScopes",[])}
            if "write_files" not in scopes:raise RuntimeError("Shopify write_files 권한이 없습니다.")
        uploaded=uploader.upload(asset["local_path"],f"{asset['asset_type']} {store_id}")
        if not uploaded.get("id") or not str(uploaded.get("url","")).startswith("https://"):raise RuntimeError("Shopify file URL 검증 실패")
        with connect(self.db) as con:con.execute("UPDATE brand_assets SET shopify_file_id=?,shopify_url=? WHERE asset_id=?",(uploaded["id"],uploaded["url"],asset_id))
        return {**asset,"shopify_file_id":uploaded["id"],"shopify_url":uploaded["url"]}

    def preview_apply(self, store_id, *, logo_asset_id, favicon_asset_id):
        snapshot=self.discover(store_id)
        logo=get_brand_asset(logo_asset_id,db=self.db);favicon=get_brand_asset(favicon_asset_id,db=self.db)
        errors=[]
        for name,asset in (("logo",logo),("favicon",favicon)):
            if not asset or asset["store_id"]!=store_id or asset["approval_status"]!="APPROVED":errors.append(f"{name}: APPROVED asset 필요")
            elif not asset.get("shopify_url"):errors.append(f"{name}: Shopify Files upload 필요")
        current_text=snapshot.get("settings_data") or "{}"
        try:current=json.loads(current_text)
        except Exception:current={}
        proposed=json.loads(json.dumps(current));settings=(proposed.setdefault("current",{})).setdefault("settings",{})
        actions=[]
        for kind,asset in (("logo",logo),("favicon",favicon)):
            found=snapshot.get("detection",{}).get(kind,{"status":"NOT_FOUND"});mapping=found.get("mapping")
            if not mapping or found["status"]!="FOUND_HIGH_CONFIDENCE":
                actions.append({"action":"MANUAL_ACTION_REQUIRED","kind":kind,"confidence":mapping.get("confidence") if mapping else 0,"reason":"Theme setting을 안전하게 특정할 수 없습니다."});continue
            setting_id=mapping["setting_id"];value=asset.get("shopify_url") if asset else None
            action="NO_CHANGE" if settings.get(setting_id)==value else "SET_LOGO" if kind=="logo" else "SET_FAVICON"
            settings[setting_id]=value;actions.append({"action":action,"kind":kind,"setting_id":setting_id,"current":mapping.get("current"),"proposed":value,"asset_id":(asset or {}).get("asset_id"),"confidence":mapping["confidence"]})
        if errors:actions.append({"action":"MANUAL_ACTION_REQUIRED","reason":"; ".join(errors)})
        if snapshot.get("status")!="CONNECTED" or not snapshot.get("write_themes"):actions.append({"action":"MANUAL_ACTION_REQUIRED","reason":"Shopify write_themes/예외 권한이 없습니다."})
        if any(item["action"]=="MANUAL_ACTION_REQUIRED" for item in actions):status="MANUAL_ACTION_REQUIRED"
        else:status="PREVIEW"
        preview_id="BAP_"+secrets.token_hex(10)
        payload={"actions":actions,"current":current,"proposed":proposed,"settings_schema":snapshot.get("settings_schema"),"settings_data":current_text,
                 "theme":snapshot.get("theme"),"write_themes":snapshot.get("write_themes",False),"diff":{"before_hash":_hash(current),"proposed_hash":_hash(proposed)},"instructions":manual_theme_instructions(store_id,logo,favicon)}
        _install(self.db)
        with connect(self.db) as con:con.execute("INSERT INTO brand_apply_previews VALUES(?,?,?,?,?,?,?)",(preview_id,store_id,(snapshot.get("theme") or {}).get("id",""),_hash(current),json.dumps(payload,ensure_ascii=False),status,_now()))
        result={"preview_id":preview_id,"status":status,**payload};_write_report(store_id,preview_id,apply_preview=result,db=self.db);return result

    def verify_manual_apply(self, preview_id):
        with connect(self.db) as con:row=con.execute("SELECT * FROM brand_apply_previews WHERE preview_id=?",(preview_id,)).fetchone()
        if not row:return {"status":"NOT_FOUND"}
        preview=json.loads(row["proposal_json"]);snapshot=self.discover(row["store_id"])
        try:observed=json.loads(snapshot.get("settings_data") or "{}")
        except Exception:observed={}
        return {"status":"VERIFIED" if observed==preview.get("proposed") else "MANUAL_ACTION_REQUIRED","observed_hash":_hash(observed),
                "expected_hash":preview.get("diff",{}).get("proposed_hash")}

    def apply(self, preview_id, *, confirmed=False, client=None):
        if confirmed is not True:raise RuntimeError("Shopify Theme 적용에는 명시적 확인이 필요합니다.")
        with connect(self.db) as con:row=con.execute("SELECT * FROM brand_apply_previews WHERE preview_id=?",(preview_id,)).fetchone()
        if not row:raise KeyError(preview_id)
        preview=json.loads(row["proposal_json"])
        if row["status"]!="PREVIEW" or not preview.get("write_themes"):return {"status":"MANUAL_ACTION_REQUIRED","instructions":preview.get("instructions")}
        with connect(self.db) as con:
            latest=con.execute("SELECT preview_id FROM brand_apply_previews WHERE store_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",(row["store_id"],)).fetchone()
        if not latest or latest["preview_id"]!=preview_id:return {"status":"CONFLICT","reason":"A newer brand apply preview exists; review it before applying."}
        for action in preview.get("actions",[]):
            asset_id=action.get("asset_id")
            if not asset_id:continue
            asset=get_brand_asset(asset_id,db=self.db)
            if not asset or asset["approval_status"]!="APPROVED" or asset.get("shopify_url")!=action.get("proposed"):
                return {"status":"CONFLICT","reason":"An approved Shopify asset changed after preview."}
        snapshot=self.discover(row["store_id"]);theme=snapshot.get("theme") or {}
        if theme.get("id")!=row["theme_id"] or _hash(json.loads(snapshot.get("settings_data") or "{}"))!=row["template_hash"]:
            return {"status":"CONFLICT","reason":"Theme settings changed since preview."}
        config=get_connection(row["store_id"],db=self.db);token,_=get_shopify_token(row["store_id"],db=self.db)
        client=client or self.client_factory(config["shop_domain"],token,config["api_version"])
        scopes=set(snapshot.get("scopes",[]))
        if "write_themes" not in scopes:return {"status":"MANUAL_ACTION_REQUIRED","instructions":preview.get("instructions")}
        folder=EXPORT_DIR/"theme_backups"/_safe_store(row["store_id"])/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+secrets.token_hex(3));folder.mkdir(parents=True,exist_ok=False)
        before={"config/settings_schema.json":snapshot["settings_schema"],"config/settings_data.json":snapshot["settings_data"]}
        (folder/"before.json").write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding="utf-8")
        (folder/"proposed.json").write_text(json.dumps(preview["proposed"],ensure_ascii=False,indent=2),encoding="utf-8")
        (folder/"diff.md").write_text(f"before={row['template_hash']}\nproposed={preview['diff']['proposed_hash']}\n",encoding="utf-8")
        backup_id="BTB_"+secrets.token_hex(8);_install(self.db)
        with connect(self.db) as con:con.execute("INSERT INTO brand_theme_backups VALUES(?,?,?,?,?,?,?)",(backup_id,row["store_id"],row["theme_id"],before["config/settings_schema.json"],before["config/settings_data.json"],str(folder),_now()))
        result=client.execute(UPSERT_THEME_FILES,{"themeId":row["theme_id"],"files":[{"filename":"config/settings_data.json","body":{"type":"TEXT","value":json.dumps(preview["proposed"],ensure_ascii=False)}}]}).get("themeFilesUpsert") or {}
        if result.get("userErrors"):return {"status":"VERIFY_FAILED","errors":[{ "field":x.get("field"),"message":str(x.get("message",""))[:180]} for x in result["userErrors"]],"backup_id":backup_id}
        after=self.discover(row["store_id"])
        try:observed=json.loads(after.get("settings_data") or "{}")
        except Exception:observed={}
        status="VERIFIED" if observed==preview["proposed"] else "VERIFY_FAILED"
        return {"status":status,"backup_id":backup_id,"theme_id":row["theme_id"],"changed_file":"config/settings_data.json"}

    def rollback(self, backup_id, *, confirmed=False, client=None):
        if confirmed is not True:raise RuntimeError("Theme rollback requires explicit confirmation")
        with connect(self.db) as con:backup=con.execute("SELECT * FROM brand_theme_backups WHERE backup_id=?",(backup_id,)).fetchone()
        if not backup:raise KeyError(backup_id)
        snapshot=self.discover(backup["store_id"])
        if not snapshot.get("write_themes"):return {"status":"MANUAL_ACTION_REQUIRED","instructions":"Shopify Theme Editor에서 백업의 settings_data.json 값을 복구하세요."}
        config=get_connection(backup["store_id"],db=self.db);token,_=get_shopify_token(backup["store_id"],db=self.db)
        client=client or self.client_factory(config["shop_domain"],token,config["api_version"])
        response=client.execute(UPSERT_THEME_FILES,{"themeId":backup["theme_id"],"files":[{"filename":"config/settings_data.json","body":{"type":"TEXT","value":backup["settings_data"]}}]}).get("themeFilesUpsert") or {}
        if response.get("userErrors"):
            return {"status":"VERIFY_FAILED","backup_id":backup_id}
        restored=self.discover(backup["store_id"])
        try:observed=json.loads(restored.get("settings_data") or "{}")
        except Exception:observed={}
        expected=json.loads(backup["settings_data"] or "{}")
        return {"status":"VERIFIED" if observed==expected else "VERIFY_FAILED","backup_id":backup_id,
                "observed_hash":_hash(observed),"expected_hash":_hash(expected)}


def manual_theme_instructions(store_id, logo=None, favicon=None):
    return {"steps":["Shopify Admin → Online Store → Themes → Customize", "Theme settings → Logo → Select/upload approved logo",
      "Theme settings → Favicon → Select/upload approved 32×32 PNG", "Save", "View store에서 헤더와 브라우저 탭 확인"],
      "store_id":store_id,"logo_path":(logo or {}).get("local_path"),"favicon_path":(favicon or {}).get("local_path")}
