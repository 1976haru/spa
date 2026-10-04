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
    result = dict(row); result["profile"] = json.loads(result.pop("profile_json")); return result


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
    profile.update(overrides or {})
    # A selected/existing brand name is immutable unless the user explicitly overrides it.
    if existing and not (overrides or {}).get("brand_name"):
        profile["brand_name"] = existing["profile"]["brand_name"]
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
    root = _brand_root(store_id, version); root.mkdir(parents=True, exist_ok=True)
    (root / "brand_profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(store_id, f"profile-v{version}", db=db)
    return {"store_id": store_id, "version": version, "profile": profile, "approval_status": "DRAFT", "source_hash": source_hash}


def brand_name_prompt(store_id, *, db=None):
    profile = get_brand_profile(store_id, db=db)
    if not profile: profile = brand_profile_from_store(store_id, db=db)
    p = profile["profile"]
    return ("Recommend exactly 10 distinctive English ecommerce brand names. Names must be short, pronounceable, "
            "memorable, extensible beyond one product, easy to spell, and less likely to be confused with famous brands. "
            "For each provide pronunciation, meaning/rationale, and brand image. End with 3 candidates representing "
            "different strategic directions. Do not claim trademark/domain clearance; state that both require review. "
            f"Store category: {p['primary_category']}; customers: {p['target_customer']}; country: {p['target_country']}; "
            f"personality/keywords: {p['personality']} / {p['brand_keywords']}. Existing brand '{p['brand_name']}' must not be renamed or auto-selected.")


def suggest_brand_names(store_id, *, db=None):
    """Produce editable candidates only; these are not legal/domain-cleared recommendations."""
    profile = get_brand_profile(store_id, db=db) or brand_profile_from_store(store_id, db=db)
    category = re.sub(r"[^A-Za-z ]", " ", profile["profile"]["primary_category"]).strip().title() or "Everyday"
    roots = ["Northway", "Kindred", "Everstead", "Clearfield", "Morrow", "Brightwell", "Truehaven", "Openline", "Fieldnote", "Wellmark"]
    candidates = [{"name": name, "pronunciation": name.lower(), "meaning": f"A distinctive, extensible identity for {category.lower()} customers.",
                   "brand_image": ["clean", "trustworthy", "memorable"][index % 3], "selected": False} for index, name in enumerate(roots)]
    root = _brand_root(store_id, profile["version"]); root.mkdir(parents=True, exist_ok=True)
    (root / "brand_name_candidates.json").write_text(json.dumps({"prompt": brand_name_prompt(store_id, db=db), "candidates": candidates,
      "directions": candidates[:3], "review_required": ["domain", "trademark"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    return candidates


def logo_mark_prompt(profile):
    p = profile.get("profile", profile)
    return (f"Create one isolated, simple geometric brand symbol for an ecommerce brand in {p['primary_category']}. "
            f"Brand personality: {p['personality']}. Palette: {p['colors']}. Style: {p['logo_style']}. "
            f"Avoid: {p['avoid_styles']}. Strong silhouette and contrast at small sizes, balanced negative space. "
            "NO TEXT, NO LETTERS, no brand name, no mockup, no packaging, no sign, no 3D, no watermark, no thin details. "
            "Square canvas, white or transparent background, centered isolated mark.")


def logo_prompt(profile):
    p = profile.get("profile", profile)
    return ("Professional horizontal Shopify header logo: a simple geometric symbol plus exact wordmark "
            f"'{p['brand_name']}'. Clean, practical, premium, readable on mobile, 2–3 colors {p['colors']}, ample padding, "
            "white or transparent background. Avoid direct product illustrations, childish styling, busy artwork, mockups, "
            "signage, packaging, 3D, complex gradients, thin details, and watermarks. Program will typeset the exact wordmark separately.")


def favicon_prompt(profile):
    return "Derive a simple centered favicon mark from the approved brand logo mark. Preserve its exact shape and palette. No text or new symbol; square layout with safe padding and strong contrast at 32x32."


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
    root = _brand_root(store_id, profile["version"]); root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{asset_type.lower()}_v{version}.{target.suffix.lstrip('.').lower()}"
    if destination != target.resolve(): shutil.copyfile(target, destination)
    asset_id = "BRA_" + secrets.token_hex(10); now = _now()
    with connect(db) as con:
        con.execute("""INSERT INTO brand_assets(asset_id,store_id,asset_type,version,provider,provider_model,prompt_hash,source_asset_id,
          width,height,format,transparent_background,local_path,sha256,approval_status,generation_status,metadata_json,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (asset_id,store_id,asset_type,version,provider,model,
          _hash(prompt) if prompt else "",source_asset_id,width,height,fmt,int(transparent),str(destination),digest,status,"COMPLETE",
          json.dumps(metadata or {},ensure_ascii=False),now))
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
      transparent=True,status="NEEDS_REVIEW",metadata={"font_family":font_name,"exact_wordmark":name,"layout":"horizontal"},db=db)
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
    return {"valid":True,"width":asset["width"],"height":asset["height"],"sha256":asset["sha256"]}


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
        transparent=transparent_white,status="NEEDS_REVIEW",metadata={"palette":colors,"padding_ratio":.16},db=db)
    small=master.resize((32,32),Image.Resampling.LANCZOS)
    path=root/f"favicon_32_v{_next_version(store_id,'FAVICON_32',db)}.png";small.save(path,optimize=True)
    asset=_register_asset(store_id,"FAVICON_32",path,provider="DERIVED",model="Pillow-Lanczos",source_asset_id=master_asset["asset_id"],
       transparent=transparent_white,status="NEEDS_REVIEW",metadata={"palette":colors,"source_mark_id":mark_asset_id},db=db)
    icon64=master.resize((64,64),Image.Resampling.LANCZOS);path64=root/f"favicon_64_v{_next_version(store_id,'FAVICON_64',db)}.png";icon64.save(path64,optimize=True)
    _register_asset(store_id,"FAVICON_64",path64,provider="DERIVED",model="Pillow-Lanczos",source_asset_id=master_asset["asset_id"],
       transparent=transparent_white,status="NEEDS_REVIEW",metadata={"palette":colors},db=db)
    (root/"favicon_prompt.txt").write_text(favicon_prompt(profile["profile"])+"\n",encoding="utf-8")
    validate_favicon(asset)
    return {"master":master_asset,"favicon_32":asset}


def validate_favicon(asset):
    _require_pillow()
    with Image.open(asset["local_path"]) as image:
        if image.size!=(32,32) or image.format!="PNG":raise ValueError("Favicon must be exactly 32x32 PNG")
        if image.getchannel("A").getbbox() is None:raise ValueError("Favicon is fully transparent")
        if image.getbbox() is None:raise ValueError("Favicon is empty")
    return {"valid":True,"width":32,"height":32,"format":"PNG"}


def approve_asset(asset_id, *, db=None):
    asset=get_brand_asset(asset_id,db=db)
    if not asset:raise KeyError(asset_id)
    if Path(asset["local_path"]).suffix.lower()!=".svg":
        _require_pillow()
        with Image.open(asset["local_path"]) as image:image.verify()
    with connect(db) as con:
        con.execute("UPDATE brand_assets SET approval_status='SUPERSEDED' WHERE store_id=? AND asset_type=? AND approval_status='APPROVED'",(asset["store_id"],asset["asset_type"]))
        con.execute("UPDATE brand_assets SET approval_status='APPROVED' WHERE asset_id=?",(asset_id,))
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
    return _register_asset(store_id,asset_type,path,provider="MANUAL",model="assignment-upload",status="NEEDS_REVIEW",
        transparent=transparent,metadata={"assignment_manual_mode":True},db=db)


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
            if asset["asset_type"]=="FAVICON_32" and (asset["width"],asset["height"])!=(32,32):result["warning"]="32x32 PNG 권장"
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
        config=get_connection(store_id,db=self.db);token,_=get_shopify_token(store_id)
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
        config=get_connection(store_id,db=self.db);token,_=get_shopify_token(store_id)
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
        config=get_connection(row["store_id"],db=self.db);token,_=get_shopify_token(row["store_id"])
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
        config=get_connection(backup["store_id"],db=self.db);token,_=get_shopify_token(backup["store_id"])
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
