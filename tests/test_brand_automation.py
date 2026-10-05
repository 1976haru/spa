from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest
from PIL import Image

import shopsource.brand_automation as brand
from shopsource.db import connect, init_db, upsert_store
from shopsource.store_build import STAGES


@pytest.fixture
def env(monkeypatch, request):
    root = Path.cwd() / "exports" / ".test_scratch" / f"brand-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    request.addfinalizer(lambda: shutil.rmtree(root, ignore_errors=True))
    db = root / "fixture.sqlite3"
    export_dir = root / "exports"
    monkeypatch.setattr(brand, "EXPORT_DIR", export_dir)
    init_db(db)
    upsert_store({"store_id": "s1", "store_name": "Cabin Tidy", "category": "Car organization",
                  "brand_name": "Cabin Tidy", "target_customer": "Drivers", "target_country": "United States",
                  "brand_personality": ["clean", "practical", "premium"],
                  "brand_colors": {"primary": "#24364B", "secondary": "#FFFFFF", "accent": "#D7C7A6"}}, db)
    profile = brand.brand_profile_from_store("s1", db=db)
    return db, export_dir, profile


def image(path, size=(512, 512), color=(36, 54, 75, 255)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, (0, 0, 0, 0)).save(path)
    canvas = Image.open(path)
    # A bold centered mark; retain transparent margins for favicon cropping tests.
    from PIL import ImageDraw
    ImageDraw.Draw(canvas).rounded_rectangle((size[0]//4, size[1]//4, size[0]*3//4, size[1]*3//4), radius=size[0]//8, fill=color)
    canvas.save(path)
    return path


def make_mark(env, *, approved=True):
    db, exports, _ = env
    source = image(exports / "fixture_mark.png")
    asset = brand._register_asset("s1", "LOGO_MARK", source, provider="FIXTURE", db=db)
    return brand.approve_asset(asset["asset_id"], db=db) if approved else asset


def test_brand_profile_from_store_profile(env):
    db, _, profile = env
    assert profile["profile"]["primary_category"] == "Car organization"
    assert profile["profile"]["target_country"] == "United States"
    assert profile["profile"]["colors"]["primary"] == "#24364B"


def test_existing_brand_name_not_renamed(env):
    db, _, _ = env
    refreshed = brand.brand_profile_from_store("s1", overrides={"primary_category": "Travel"}, db=db)
    assert refreshed["profile"]["brand_name"] == "Cabin Tidy"


def test_brand_name_prompt_and_candidates_saved(env):
    db, exports, _ = env
    prompt = brand.brand_name_prompt("s1", db=db)
    candidates = brand.suggest_brand_names("s1", db=db)
    assert "exactly 10" in prompt and "trademark" in prompt.lower()
    assert len(candidates) == 10
    saved = list(exports.glob("brand_assets/s1/v*/brand_name_candidates.json"))
    assert saved and len(json.loads(saved[0].read_text(encoding="utf-8"))["candidates"]) == 10


def test_logo_prompt_generated_and_mark_prompt_excludes_text(env):
    _, _, profile = env
    assert "Professional horizontal Shopify" in brand.logo_prompt(profile)
    prompt = brand.logo_mark_prompt(profile)
    assert "NO TEXT" in prompt and "no brand name" in prompt.lower()


def test_wordmark_exact_brand_spelling(env):
    db, _, _ = env
    mark = make_mark(env)
    logo = brand.compose_horizontal_logo("s1", mark["asset_id"], db=db)
    assert logo["metadata"]["exact_wordmark"] == "Cabin Tidy"
    assert brand.validate_logo_horizontal(logo)["valid"]
    assert Path(logo["local_path"]).with_suffix(".svg").is_file()


def test_logo_asset_versioning(env):
    first = make_mark(env)
    second = make_mark(env)
    assert (first["version"], second["version"]) == (1, 2)
    assert first["asset_id"] != second["asset_id"]


def test_favicon_derived_from_approved_logo_mark(env):
    db, _, _ = env
    mark = make_mark(env)
    result = brand.derive_favicon("s1", mark["asset_id"], db=db)
    assert result["favicon_32"]["source_asset_id"] == result["master"]["asset_id"]
    assert result["master"]["source_asset_id"] == mark["asset_id"]


def test_favicon_prompt_preserves_approved_mark_identity():
    prompt = brand.favicon_prompt({"brand_name": "Cabin Tidy"})
    assert "approved brand logo mark" in prompt and "No text" in prompt and "32x32" in prompt


def test_favicon_exact_32x32_and_transparency_option(env):
    db, _, _ = env
    mark = make_mark(env)
    result = brand.derive_favicon("s1", mark["asset_id"], transparent_white=True, db=db)
    assert brand.validate_favicon(result["favicon_32"]) == {"valid": True, "width": 32, "height": 32, "format": "PNG"}
    assert result["favicon_32"]["transparent_background"] == 1
    assert result["favicon_32"]["metadata"]["palette"]["primary"] == "#24364B"


def test_generated_assets_require_approval(env):
    db, _, _ = env
    mark = make_mark(env, approved=False)
    with pytest.raises(ValueError, match="APPROVED"):
        brand.derive_favicon("s1", mark["asset_id"], db=db)


def test_openai_brand_generation_requires_opt_in(env):
    class Provider:
        provider_name = "MOCK"
        def __init__(self): self.calls = 0
        def generate(self, prompt, size, output, **kwargs):
            self.calls += 1
            image(Path(output))
            return {"path": str(output), "metadata": {}}
    provider = Provider()
    with pytest.raises(RuntimeError): brand.generate_logo_mark("s1", provider=provider, enabled=False, db=env[0])
    assert provider.calls == 0
    generated = brand.generate_logo_mark("s1", provider=provider, enabled=True, db=env[0])
    assert generated["approval_status"] == "NEEDS_REVIEW" and provider.calls == 1


def test_openai_keyring_credential_lookup_without_persistence(monkeypatch):
    import sys
    from types import SimpleNamespace
    from shopsource.collection_images import OpenAIImagesProvider
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls=[]
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_password=lambda service, user: calls.append((service,user)) or "credential-fixture"))
    provider = OpenAIImagesProvider()
    assert provider.api_key == "credential-fixture"
    assert calls == [("ShopSourceStudio", "openai-images-api-key")]


def test_secret_not_logged(env, caplog):
    db, _, _ = env
    brand._write_report("s1", "safe-run", db=db)
    report = (brand.EXPORT_DIR / "brand_reports/s1/safe-run/summary.md").read_text(encoding="utf-8")
    assert "shpat_" not in report and "access_token" not in report and "OPENAI_API_KEY" not in report
    assert "shpat_" not in caplog.text


class FakeShopify:
    schema = json.dumps([{"name": "Theme settings", "settings": [
        {"type": "image_picker", "id": "logo", "label": "Logo"},
        {"type": "image_picker", "id": "favicon", "label": "Favicon"}]}])
    def __init__(self, writable=True):
        self.writable = writable
        self.settings = {"current": {"settings": {"logo": "old-logo", "favicon": "old-favicon"}}}
        self.writes = []
    def execute(self, query, variables=None):
        if "currentAppInstallation" in query:
            scopes = ["read_themes"] + (["write_themes"] if self.writable else [])
            return {"currentAppInstallation": {"accessScopes": [{"handle": x} for x in scopes]}}
        if "BrandThemes" in query:
            return {"themes": {"nodes": [{"id": "gid://shopify/OnlineStoreTheme/1", "name": "Fixture", "role": "MAIN"}]}}
        if "BrandThemeFilesUpsert" in query:
            self.writes.append(variables)
            self.settings = json.loads(variables["files"][0]["body"]["value"])
            return {"themeFilesUpsert": {"userErrors": [], "upsertedThemeFiles": [{"filename": "config/settings_data.json"}]}}
        return {"theme": {"id": "gid://shopify/OnlineStoreTheme/1", "name": "Fixture", "role": "MAIN",
                           "files": {"nodes": [
                               {"filename": "config/settings_schema.json", "body": {"content": self.schema}},
                               {"filename": "config/settings_data.json", "body": {"content": json.dumps(self.settings)}}]}}}


def theme_service(monkeypatch, db, fake):
    monkeypatch.setattr(brand, "get_connection", lambda store_id, db=None: {"shop_domain": "fixture.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr(brand, "get_shopify_token", lambda store_id, db=None: ("fake-secret", "fixture"))
    return brand.BrandThemeService(db=db, client_factory=lambda *args: fake)


def approved_theme_assets(env):
    db, exports, _ = env
    mark = make_mark(env)
    logo = brand.compose_horizontal_logo("s1", mark["asset_id"], db=db)
    favicon = brand.derive_favicon("s1", mark["asset_id"], db=db)["favicon_32"]
    logo = brand.approve_asset(logo["asset_id"], db=db)
    favicon = brand.approve_asset(favicon["asset_id"], db=db)
    with connect(db) as con:
        con.execute("UPDATE brand_assets SET shopify_url='https://cdn.shopify.com/logo.png' WHERE asset_id=?", (logo["asset_id"],))
        con.execute("UPDATE brand_assets SET shopify_url='https://cdn.shopify.com/favicon.png' WHERE asset_id=?", (favicon["asset_id"],))
    return logo, favicon


def test_shopify_brand_file_upload_mock(env, monkeypatch):
    db, _, _ = env
    asset = make_mark(env)
    approved = brand.approve_asset(asset["asset_id"], db=db)
    class Uploader:
        def upload(self, path, alt): return {"id": "gid://shopify/GenericFile/1", "url": "https://cdn.shopify.com/mark.png"}
    monkeypatch.setattr(brand, "get_connection", lambda store_id, db=None: {"shop_domain": "fixture.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr(brand, "get_shopify_token", lambda store_id, db=None: ("fake", "fixture"))
    result = brand.BrandThemeService(db=db).upload_approved_asset("s1", approved["asset_id"], uploader=Uploader())
    assert result["shopify_file_id"].endswith("/1") and result["shopify_url"].startswith("https://")


def test_theme_logo_and_favicon_settings_detected():
    result = brand.detect_brand_settings(FakeShopify.schema, json.dumps({"current": {"settings": {"logo": "x", "favicon": "y"}}}))
    assert result["logo"]["status"] == result["favicon"]["status"] == "FOUND_HIGH_CONFIDENCE"


def test_unknown_theme_mapping_manual_fallback(env, monkeypatch):
    db, _, _ = env
    fake = FakeShopify(); fake.schema = "[]"
    service = theme_service(monkeypatch, db, fake)
    logo, favicon = approved_theme_assets(env)
    result = service.preview_apply("s1", logo_asset_id=logo["asset_id"], favicon_asset_id=favicon["asset_id"])
    assert result["status"] == "MANUAL_ACTION_REQUIRED"
    assert not fake.writes


def test_brand_apply_preview_no_write(env, monkeypatch):
    db, _, _ = env
    fake = FakeShopify()
    service = theme_service(monkeypatch, db, fake)
    logo, favicon = approved_theme_assets(env)
    result = service.preview_apply("s1", logo_asset_id=logo["asset_id"], favicon_asset_id=favicon["asset_id"])
    assert result["status"] == "PREVIEW"
    assert [x["action"] for x in result["actions"]] == ["SET_LOGO", "SET_FAVICON"]
    assert not fake.writes


def test_brand_apply_backup_verify_and_rollback(env, monkeypatch):
    db, exports, _ = env
    fake = FakeShopify(); service = theme_service(monkeypatch, db, fake)
    logo, favicon = approved_theme_assets(env)
    preview = service.preview_apply("s1", logo_asset_id=logo["asset_id"], favicon_asset_id=favicon["asset_id"])
    monkeypatch.setattr(brand, "EXPORT_DIR", exports)
    with pytest.raises(RuntimeError): service.apply(preview["preview_id"], confirmed=False, client=fake)
    applied = service.apply(preview["preview_id"], confirmed=True, client=fake)
    assert applied["status"] == "VERIFIED"
    assert (exports / "theme_backups/s1" / Path(applied["backup_id"])).exists() is False  # backup uses timestamp directory
    rolled = service.rollback(applied["backup_id"], confirmed=True, client=fake)
    assert rolled["status"] == "VERIFIED"
    assert len(fake.writes) == 2


def test_assignment_manual_asset_mode(env):
    db, exports, _ = env
    source = image(exports / "student-logo.png")
    asset = brand.register_manual_asset("s1", "LOGO_HORIZONTAL", source, db=db)
    assert asset["metadata"]["assignment_manual_mode"] is True
    assert asset["approval_status"] == "NEEDS_REVIEW"


def test_svg_manual_assignment_validation(env):
    db, exports, _ = env
    source = exports / "mark.svg"
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80"></svg>', encoding="utf-8")
    asset = brand.register_manual_asset("s1", "LOGO_HORIZONTAL", source, db=db)
    assert brand.validate_manual_assets("s1", db=db)[0]["valid"] is True


def test_brand_apply_newer_preview_blocks_stale_apply(env, monkeypatch):
    db, _, _ = env
    fake = FakeShopify(); service = theme_service(monkeypatch, db, fake)
    logo, favicon = approved_theme_assets(env)
    old = service.preview_apply("s1", logo_asset_id=logo["asset_id"], favicon_asset_id=favicon["asset_id"])
    newer = service.preview_apply("s1", logo_asset_id=logo["asset_id"], favicon_asset_id=favicon["asset_id"])
    assert service.apply(old["preview_id"], confirmed=True, client=fake)["status"] == "CONFLICT"
    assert not fake.writes
    assert newer["preview_id"] != old["preview_id"]


def test_write_themes_missing_forces_manual_preview(env, monkeypatch):
    db, _, _ = env
    fake = FakeShopify(writable=False); service = theme_service(monkeypatch, db, fake)
    logo, favicon = approved_theme_assets(env)
    preview = service.preview_apply("s1", logo_asset_id=logo["asset_id"], favicon_asset_id=favicon["asset_id"])
    assert preview["status"] == "MANUAL_ACTION_REQUIRED"
    assert service.apply(preview["preview_id"], confirmed=True, client=fake)["status"] == "MANUAL_ACTION_REQUIRED"
    assert not fake.writes


def test_collection_image_can_use_brand_profile(env, monkeypatch):
    from shopsource import collection_images
    db, exports, _ = env
    monkeypatch.setattr(collection_images, "EXPORT_DIR", exports)
    class MockProvider:
        provider_name = "MOCK"
        model_version = "fixture"
        def __init__(self): self.prompt = ""
        def generate(self, prompt, size, output_path):
            self.prompt = prompt
            path = image(Path(output_path))
            return {"path": str(path), "metadata": {}}
    provider = MockProvider()
    result = collection_images.generate_collection_image("s1", {
        "collection_key": "fixture", "image_prompt": "Organized travel storage", "image_alt_text": "Storage"},
        provider=provider, db=db)
    assert "#24364B" in provider.prompt and "Organized travel storage" in provider.prompt
    assert Path(result["path"]).is_file()


def test_store_build_brand_stage_order():
    assert STAGES.index("BRAND_PLAN") < STAGES.index("COLLECTION_PLAN")
    assert STAGES.index("NAVIGATION_VERIFY") < STAGES.index("HOMEPAGE_PLAN") < STAGES.index("HOMEPAGE_VERIFY")
    assert STAGES.index("HOMEPAGE_VERIFY") < STAGES.index("BRAND_APPLY_PREVIEW") < STAGES.index("BRAND_APPLY")


def test_protected_store_file_not_part_of_brand_storage(env):
    _, exports, _ = env
    assert "stores" not in str(exports).split("\\")
