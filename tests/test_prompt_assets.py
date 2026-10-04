import json

from shopsource.prompt_assets import PromptAssetService


def test_prompt_set_contains_required_asset_families_and_no_text_hero():
    result = PromptAssetService().build(store={"store_name": "Cabin Tidy"}, collection_plan={"collections": [
        {"collection_key": "trunk", "title": "Trunk Organizers", "enabled": True},
        {"collection_key": "seat", "title": "Seat Organizers", "enabled": True},
    ]})
    by_type = {x["asset_type"]: x for x in result["assets"]}
    assert {"HERO_BANNER", "COLLECTION_IMAGE", "CATEGORY_SHORTCUT", "HEADER_SUPPORT", "BRAND_REUSE_GUIDANCE"} <= set(by_type)
    assert "no words" in by_type["HERO_BANNER"]["prompt_main"]
    assert by_type["HERO_BANNER"]["suggested_aspect_ratio"] == "16:9"
    assert by_type["COLLECTION_IMAGE"]["alt_text_suggestion"]
    assert by_type["CATEGORY_SHORTCUT"]["file_naming_rule"]


def test_cabin_tidy_context_defaults_and_per_collection_prompts():
    result = PromptAssetService().build(collection_plan={"collections": [
        {"collection_key": "trunk_organizers", "title": "Trunk Organizers", "enabled": True},
        {"collection_key": "trash_cleanup", "title": "Trash & Cleanup", "enabled": False},
    ]})
    assert result["store"] == "Cabin Tidy"
    assert result["summary"]["collections"] == 1
    assert all("Cabin Tidy" in x["prompt_main"] for x in result["assets"] if x["asset_type"] == "HERO_BANNER")


def test_prompt_export_writes_plain_text_bundle_and_assets(tmp_path):
    prompt_set = PromptAssetService().build(collection_plan={"collections": []})
    exported = PromptAssetService().export(prompt_set, store_id="001", output_root=tmp_path)
    assert set(exported["files"]) == {"hero_prompts.md", "collection_prompts.md", "category_prompts.md", "header_prompts.md", "copy_paste_bundle.txt", "asset_prompts.json"}
    bundle = (tmp_path / "asset_prompts" / "001" / prompt_set["run_id"] / "copy_paste_bundle.txt").read_text(encoding="utf-8")
    payload = json.loads((tmp_path / "asset_prompts" / "001" / prompt_set["run_id"] / "asset_prompts.json").read_text(encoding="utf-8"))
    assert "[Homepage Hero Banner / Copy]" in bundle
    assert payload["assets"]


def test_prompt_export_uses_store_scoped_run_path(tmp_path):
    service = PromptAssetService(); prompt_set = service.build()
    out = service.export(prompt_set, store_id="store two", output_root=tmp_path)
    assert "store-two" in out["folder"] and prompt_set["run_id"] in out["folder"]
