"""Local collection image preparation; paid generation requires explicit opt-in."""
from __future__ import annotations

import base64
import json
import os
import shutil
import re
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from .db import connect, init_db, utc_now
from .paths import EXPORT_DIR
from .image_validation import inspect_image


class CollectionImageProvider(ABC):
    provider_name = "BASE"
    model_version = ""

    @abstractmethod
    def generate(self, prompt: str, size: str, output_path: str | Path) -> dict:
        raise NotImplementedError


class ManualImageProvider(CollectionImageProvider):
    """MANUAL means select an existing local asset; it never fabricates an image."""
    provider_name = "MANUAL"
    model_version = "local-file-v1"

    def generate(self, prompt: str, size: str, output_path: str | Path) -> dict:
        raise RuntimeError("MANUAL provider does not generate images; select a local file")

    def register(self, store_id: str, collection_key: str, path: str | Path, alt_text: str, db=None) -> dict:
        source=Path(path).expanduser().resolve()
        target=image_path(store_id,collection_key,1).with_suffix(source.suffix.lower())
        target.parent.mkdir(parents=True,exist_ok=True)
        if source != target: shutil.copyfile(source,target)
        return register_image_asset(store_id, collection_key, target, provider=self.provider_name,
                                    model=self.model_version, alt_text=alt_text,
                                    metadata={"mode":"manual", "inspection": inspect_image(target, asset_type="COLLECTION_IMAGE")}, db=db)


class OpenAIImagesProvider(CollectionImageProvider):
    """Optional OpenAI Images API client. Tests inject a transport; callers gate opt-in."""
    provider_name = "OPENAI_IMAGES"
    endpoint = "https://api.openai.com/v1/images/generations"

    def __init__(self, api_key: str | None = None, *, model: str | None = None, opener=urllib.request.urlopen):
        self.api_key = api_key if api_key is not None else get_openai_api_key()
        self.model_version = model or os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
        self.opener = opener

    def generate(self, prompt: str, size: str, output_path: str | Path, *, enabled: bool = False) -> dict:
        if not enabled:
            raise RuntimeError("Enable paid image generation (opt-in) before calling the image API")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        if size not in {"1024x1024", "1536x1024", "1024x1536"}:
            raise ValueError("Unsupported image size")
        safe_prompt = f"{prompt.strip()} Square ecommerce lifestyle photography, centered category product, photorealistic, no text, no logo, no watermark."
        body=json.dumps({"model":self.model_version,"prompt":safe_prompt,"size":size,"n":1,"output_format":"png"}).encode()
        request=urllib.request.Request(self.endpoint,data=body,headers={"Content-Type":"application/json","Authorization":f"Bearer {self.api_key}"})
        try:
            with self.opener(request,timeout=180) as response:
                payload=json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"OpenAI image generation failed: {type(exc).__name__}") from None
        entries=payload.get("data") or []
        encoded=entries[0].get("b64_json") if entries else None
        if not encoded:
            raise RuntimeError("OpenAI image response did not contain image data")
        target=Path(output_path); target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(base64.b64decode(encoded,validate=True))
        return {"path":str(target),"provider":self.provider_name,"model":self.model_version,
                "metadata":{"size":size,"response_created":payload.get("created")}}


def get_openai_api_key() -> str | None:
    """Read image credentials from the environment or OS credential store only."""
    value=os.environ.get("OPENAI_API_KEY")
    if value:return value
    try:
        import keyring
        return keyring.get_password("ShopSourceStudio", "openai-images-api-key")
    except Exception:
        return None


def image_path(store_id: str, collection_key: str, version: int = 1) -> Path:
    safe_store="".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in store_id)
    safe_key="".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in collection_key)
    return EXPORT_DIR / "collection_images" / safe_store / safe_key / f"v{version}.png"


def register_image_asset(store_id: str, collection_key: str, path: str | Path, *, provider: str = "MANUAL",
                         model: str = "", alt_text: str = "", metadata: dict | None = None, db=None) -> dict:
    target=Path(path).expanduser().resolve()
    if not target.is_file(): raise FileNotFoundError(target)
    if target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}: raise ValueError("Use PNG, JPEG, or WebP collection artwork")
    metadata = dict(metadata or {})
    metadata.setdefault("inspection", inspect_image(target, asset_type="COLLECTION_IMAGE"))
    init_db(db)
    with connect(db) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS collection_image_assets (
          store_id TEXT NOT NULL,collection_key TEXT NOT NULL,path TEXT NOT NULL,provider TEXT NOT NULL,
          model TEXT NOT NULL DEFAULT '',alt_text TEXT NOT NULL DEFAULT '',metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,
          approval_status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW',
          PRIMARY KEY(store_id,collection_key))""")
        columns={row["name"] for row in con.execute("PRAGMA table_info(collection_image_assets)")}
        if "approval_status" not in columns:
            con.execute("ALTER TABLE collection_image_assets ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW'")
        con.execute("""INSERT INTO collection_image_assets(store_id,collection_key,path,provider,model,alt_text,metadata_json,created_at)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(store_id,collection_key) DO UPDATE SET path=excluded.path,
          provider=excluded.provider,model=excluded.model,alt_text=excluded.alt_text,metadata_json=excluded.metadata_json,created_at=excluded.created_at""",
          (store_id,collection_key,str(target),provider,model,alt_text,json.dumps(metadata,ensure_ascii=False),utc_now()))
    return {"store_id":store_id,"collection_key":collection_key,"path":str(target),"provider":provider,"model":model,"alt_text":alt_text}


def approve_collection_image(store_id: str, collection_key: str, *, db=None) -> bool:
    """Explicitly approve a registered collection image for storefront reuse."""
    init_db(db)
    with connect(db) as con:
        exists=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_image_assets'").fetchone()
        if not exists:return False
        columns={row["name"] for row in con.execute("PRAGMA table_info(collection_image_assets)")}
        if "approval_status" not in columns:
            con.execute("ALTER TABLE collection_image_assets ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW'")
        row=con.execute("SELECT path FROM collection_image_assets WHERE store_id=? AND collection_key=?",(store_id,collection_key)).fetchone()
        if not row:return False
        inspection=inspect_image(row["path"],asset_type="COLLECTION_IMAGE")
        if not inspection.get("valid"):
            if inspection.get("status")=="DEPENDENCY_MISSING":raise RuntimeError(inspection.get("message_ko"))
            return False
        return con.execute("UPDATE collection_image_assets SET approval_status='APPROVED' WHERE store_id=? AND collection_key=?",
                           (store_id,collection_key)).rowcount > 0


def approved_collection_images(store_id: str, *, db=None) -> dict[str, dict]:
    init_db(db)
    with connect(db) as con:
        exists=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_image_assets'").fetchone()
        if not exists:return {}
        columns={row["name"] for row in con.execute("PRAGMA table_info(collection_image_assets)")}
        if "approval_status" not in columns:
            con.execute("ALTER TABLE collection_image_assets ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW'")
        return {row["collection_key"]:dict(row) for row in con.execute(
            "SELECT * FROM collection_image_assets WHERE store_id=? AND approval_status='APPROVED'",(store_id,))}


def generate_collection_image(store_id: str, definition: dict, *, provider: CollectionImageProvider,
                              enabled: bool = False, version: int | None = None, db=None) -> dict:
    if isinstance(provider, OpenAIImagesProvider) and not enabled:
        raise RuntimeError("Enable '이미지 자동 생성 사용' before calling the paid image API")
    prompt = definition["image_prompt"]
    if db is not None:
        with connect(db) as con:
            exists=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='brand_profiles'").fetchone()
            brand=con.execute("SELECT profile_json FROM brand_profiles WHERE store_id=?",(store_id,)).fetchone() if exists else None
        if brand:
            profile=json.loads(brand["profile_json"])
            prompt=(f"Consistent brand identity: {profile.get('personality')}; palette {profile.get('colors')}; "
                    f"visual direction {profile.get('logo_style')}. Preserve a clean restrained aesthetic.\n\n"+prompt)
    if version is None:
        init_db(db)
        with connect(db) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS collection_image_assets (
              store_id TEXT NOT NULL,collection_key TEXT NOT NULL,path TEXT NOT NULL,provider TEXT NOT NULL,
              model TEXT NOT NULL DEFAULT '',alt_text TEXT NOT NULL DEFAULT '',metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,
              PRIMARY KEY(store_id,collection_key))""")
            previous=con.execute("SELECT path FROM collection_image_assets WHERE store_id=? AND collection_key=?",
                                 (store_id,definition["collection_key"])).fetchone()
        match=re.search(r"v(\d+)$",Path(previous[0]).stem) if previous else None
        version=int(match.group(1))+1 if match else 1
    path=image_path(store_id,definition["collection_key"],version)
    kwargs={"enabled":enabled} if isinstance(provider,OpenAIImagesProvider) else {}
    result=provider.generate(prompt,"1024x1024",path,**kwargs)
    asset=register_image_asset(store_id,definition["collection_key"],result["path"],provider=provider.provider_name,
                               model=provider.model_version,alt_text=definition.get("image_alt_text", ""),metadata=result.get("metadata"),db=db)
    return {**asset,"preview_url":Path(asset["path"]).as_uri()}
