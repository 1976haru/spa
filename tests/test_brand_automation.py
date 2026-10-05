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


def test_existing_name_lock_migration_is_additive_and_idempotent(env):
    db,_,profile=env
    # Simulate a pre-migration database where a profile exists but name state does not.
    with brand.connect(db) as con:con.execute("DELETE FROM brand_name_state WHERE store_id='s1'")
    first=brand.migrate_locked_existing_brand_name("s1","Cabin Tidy",confirmed=True,db=db)
    second=brand.migrate_locked_existing_brand_name("s1","Cabin Tidy",confirmed=True,db=db)
    assert first["name_status"]==second["name_status"]=="LOCKED"
    assert first["brand_name"]=="Cabin Tidy" and brand.get_brand_profile("s1",db=db)["profile"]["brand_name"]=="Cabin Tidy"
    with pytest.raises(ValueError):brand.migrate_locked_existing_brand_name("s1","Wrong Name",confirmed=True,db=db)


def test_existing_brand_name_not_renamed(env):
    db, _, _ = env
    refreshed = brand.brand_profile_from_store("s1", overrides={"primary_category": "Travel"}, db=db)
    assert refreshed["profile"]["brand_name"] == "Cabin Tidy"


def test_brand_name_prompt_and_candidates_saved(env):
    db, exports, _ = env
    prompt = brand.brand_name_prompt("s1", db=db)
    candidates = brand.suggest_brand_names("s1", db=db)
    assert "exactly 10" in prompt and "trademark" in prompt.lower()
    assert candidates == []  # prompt-first: no fabricated/default names
    assert not list(exports.glob("brand_assets/s1/v*/brand_name_candidates.json"))


def _candidate_payload():
    directions=["EXPLORE","TRUST","MODERN"]
    candidates=[{"brand_name":f"Mira{index} Way","pronunciation":f"mee-rah {index}",
                 "meaning_and_rationale":"A short extensible identity for the intended audience.",
                 "brand_image":"clear, useful, memorable","direction":directions[index%3],"review_status":"UNREVIEWED"}
                for index in range(10)]
    # Ensure ten distinct names while the shortlist spans three strategic directions.
    candidates[3]["direction"]="TRUST"; candidates[4]["direction"]="MODERN"
    return {"candidates":candidates,"shortlist":[candidates[0]["brand_name"],candidates[1]["brand_name"],candidates[2]["brand_name"]]}


def test_name_candidate_contract_import_lock_and_explicit_change(env):
    db, _, profile=env
    assert profile["profile"]["brand_name"]=="Cabin Tidy"
    assert brand.brand_name_state("s1",db=db)["name_status"]=="LOCKED"
    prompt=brand.brand_name_prompt("s1",db=db).lower()
    assert "exactly 10" in prompt and "shortlist" in prompt and "trademark" in prompt and "domain" in prompt
    payload=_candidate_payload()
    imported=brand.import_brand_name_candidates("s1",payload,db=db)
    assert len(imported)==10 and sum(x["shortlisted"] for x in imported)==3
    with pytest.raises(PermissionError):brand.lock_brand_name("s1",payload["candidates"][0]["brand_name"],confirmed=True,clearance_reviewed=True,db=db)
    brand.begin_brand_name_change("s1",confirmed=True,db=db)
    selected=payload["candidates"][0]["brand_name"]
    locked=brand.lock_brand_name("s1",selected,confirmed=True,clearance_reviewed=True,db=db)
    assert locked["profile"]["brand_name"]==selected and locked["name_status"]=="LOCKED"
    assert brand.check_brand_name_conflicts("s1",selected,db=db)["legal_clearance"]=="NOT_CHECKED"


def test_name_prompt_is_store_contextual_and_prompt_exports_have_no_template_tokens(env):
    db,exports,_=env
    upsert_store({"store_id":"s2","store_name":"Garden Orbit","category":"Garden tools","target_customer":"Home gardeners","market":"Canada"},db)
    p2=brand.brand_profile_from_store("s2",db=db)
    prompt=brand.brand_name_prompt("s2",db=db)
    assert "Garden tools" in prompt and "Home gardeners" in prompt and p2["profile"]["brand_name"]=="Garden Orbit"
    files=brand.export_brand_prompts("s2",db=db)
    assert {Path(v).name for v in files.values()}=={"brand_name_prompt.txt","logo_prompt.txt","favicon_prompt.txt"}
    logo=Path(files["logo_prompt"]).read_text(encoding="utf-8")
    assert "Garden Orbit" in logo and "Garden tools" in logo and "Canada" not in logo  # logo contract uses actual visual fields
    assert all(Path(value).is_file() for value in files.values())


def test_name_candidates_detect_existing_store_conflict_without_legal_claim(env):
    db,_,_=env
    upsert_store({"store_id":"s2","store_name":"Mira0 Way","category":"Garden"},db)
    payload=_candidate_payload()
    imported=brand.import_brand_name_candidates("s1",payload,db=db)
    conflict=next(row for row in imported if row["brand_name"]=="Mira0 Way")
    assert conflict["review_status"]=="BASIC_CONFLICT_FOUND"
    assert conflict["conflicts"] and "trademark" in brand.brand_name_prompt("s1",db=db).lower()


def test_name_suggestions_do_not_reuse_fabricated_fixed_names(env):
    db,_,_=env
    source=Path(brand.__file__).read_text(encoding="utf-8")
    for old in ("Northway","Kindred","Everstead","Clearfield","Morrow","Brightwell","Truehaven","Openline","Fieldnote","Wellmark"):
        assert old not in source
    assert brand.suggest_brand_names("s1",db=db)==[]


def test_logo_prompt_generated_and_mark_prompt_excludes_text(env):
    _, _, profile = env
    prompt = brand.logo_prompt(profile)
    assert "Cabin Tidy" in prompt and "horizontal" in prompt and "mobile" in prompt
    assert "3D" in prompt and "watermark" in prompt and "safe padding" in prompt
    prompt = brand.logo_mark_prompt(profile)
    assert "NO TEXT" in prompt and "no brand name" in prompt.lower()


def test_logo_prompt_uses_profile_audience_palette_and_avoidance(env):
    db,_,_=env
    profile=brand.brand_profile_from_store("s1",overrides={"primary_category":"Travel storage","target_customer":"Commuters",
        "desired_brand_image":"calm modern premium","colors":{"primary":"navy","accent":"sand"},
        "avoid_colors":["neon"],"avoid_styles":["mascots","3D"]},db=db)
    prompt=brand.logo_prompt(profile)
    for value in ("Cabin Tidy","Travel storage","Commuters","calm modern premium","navy","neon","mascots","mobile","horizontal"):
        assert value in prompt


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


def test_favicon_identity_stale_after_logo_identity_change(env):
    db,_,_=env
    mark=make_mark(env)
    result=brand.derive_favicon("s1",mark["asset_id"],db=db)
    approved=brand.approve_asset(result["favicon_32"]["asset_id"],db=db)
    brand.mark_brand_assets_stale("s1",reason="TEST_IDENTITY_CHANGE",db=db)
    stale=brand.get_brand_asset(approved["asset_id"],db=db)
    report=brand.validate_favicon(stale)
    assert stale["approval_status"]=="NEEDS_REVIEW"
    assert report["identity_status"]=="STALE_IDENTITY_REVIEW_REQUIRED"
    assert Path(stale["local_path"]).is_file()


def test_favicon_initial_requires_explicit_choice_and_validates(env):
    db,_,_=env
    with pytest.raises(PermissionError):brand.derive_initial_favicon("s1","CT",db=db)
    asset=brand.derive_initial_favicon("s1","CT",confirmed=True,db=db)
    assert asset["metadata"]["selected_initial"]=="CT"
    assert brand.validate_favicon(asset)["width"]==32


def test_brand_identity_evidence_never_guesses_remote_verification(env):
    db,_,_=env
    mark=make_mark(env);logo=brand.compose_horizontal_logo("s1",mark["asset_id"],db=db)
    brand.approve_asset(logo["asset_id"],db=db)
    evidence=brand.brand_identity_evidence("s1",db=db)
    assert evidence["name_status"]=="LOCKED"
    assert evidence["logo"]=="APPROVED_LOCAL" and evidence["favicon"]=="MISSING"
    assert evidence["logo"]!="VERIFIED_REMOTE"


def test_favicon_prompt_preserves_approved_mark_identity():
    prompt = brand.favicon_prompt({"brand_name": "Cabin Tidy"}, source_logo_mark_id="mark-1")
    assert "approved LOGO_MARK" in prompt and "Do not use the full brand name" in prompt and "32x32" in prompt


def test_favicon_exact_32x32_and_transparency_option(env):
    db, _, _ = env
    mark = make_mark(env)
    result = brand.derive_favicon("s1", mark["asset_id"], transparent_white=True, db=db)
    report = brand.validate_favicon(result["favicon_32"])
    assert report["valid"] and report["width"] == 32 and report["format"] == "PNG"
    assert report["visual_review_required"] and report["technical_status"] in {"TECHNICAL_PASS", "NEEDS_VISUAL_REVIEW"}
    meta=result["favicon_32"]["metadata"]
    assert meta["source_logo_mark_id"] == mark["asset_id"]
    assert meta["source_sha256"] == mark["sha256"]
    assert meta["brand_profile_version"] == 1 and meta["palette_snapshot"]["primary"] == "#24364B"
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
