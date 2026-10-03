from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from shopsource.live_pilot import ControlledLivePilotService, LivePilotStopped


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def preflight(self, store_id):
        self.calls.append(("preflight", store_id))
        return {"shop_domain": "cabin-tidy.myshopify.com", "credential_present": True,
                "authenticated": True, "api_version": "2026-01", "api_version_ready": True,
                "scopes": {"read_products": True, "write_products": True},
                "product_count": 4, "collections": [], "navigation": [],
                "published_theme": {"id": "theme-1"}, "logo": "logo.png", "favicon": "fav.png",
                "publication": "Online Store", "target_market": "US", "currency": "USD"}

    def preview_products(self, store_id, limit):
        self.calls.append(("preview_products", store_id, limit))
        return {"run_id": "SYNC1", "pilot_run_id": "P1", "items": [
            {"source_id": f"A{i:09d}", "title": f"Product {i}", "source_price": 3,
             "selling_price": 12 + i, "currency": "USD", "action": "CREATE", "tags": ["shop-source"]}
            for i in range(limit)]}

    def write_products(self, sync_run_id, preview=None):
        self.calls.append(("write_products", sync_run_id))
        return {"status": "COMPLETE", "remote_ids": [f"gid://Product/{i}" for i in range(len(preview["items"]))]}

    def verify_products(self, preview, result):
        self.calls.append(("verify_products",))
        return [{"source_id": x["source_id"], "title": x["title"], "status": "DRAFT",
                 "selling_price": x["selling_price"], "identity_match": True, "duplicate": False,
                 "merchant_tags_preserved": True, "variant_mapping_valid": True, "verified": True}
                for x in preview["items"]]

    def preview_collections(self, store_id, pilot_run_id):
        return {"items": [{"title": f"C{i}", "match_count": 4 - i} for i in range(5)]}

    def write_collections(self, preview):
        self.calls.append(("write_collections",))
        return {"status": "COMPLETE", "count": len(preview["items"])}

    def write_navigation(self, preview):
        self.calls.append(("write_navigation",))
        return {"status": "COMPLETE"}

    def apply_homepage(self, preview):
        self.calls.append(("apply_homepage",))
        return {"status": "APPLIED"}


@pytest.fixture
def service(tmp_path):
    adapter = FakeAdapter()
    svc = ControlledLivePilotService(db=tmp_path / "pilot.sqlite3", adapter=adapter,
                                     reports_root=tmp_path / "reports")
    svc.fake = adapter
    return svc


def start(service):
    return service.create_run("001", "cabin-tidy.myshopify.com")["run_id"]


def through_products(service):
    run = start(service)
    service.preflight(run)
    service.preview_products(run)
    service.write_products(run, confirmed=True)
    service.verify_products(run)
    return run


def through_collections(service):
    run = through_products(service)
    service.preview_collections(run)
    service.write_collections(run, confirmed=True)
    service.verify_collections(run, {"verified": True, "duplicates": False})
    return run


def test_live_pilot_requires_correct_store(service):
    with pytest.raises(ValueError): service.create_run("002", "other.myshopify.com")
    run = start(service)
    bad = service.fake.preflight("001") | {"shop_domain": "wrong.myshopify.com"}
    with pytest.raises(LivePilotStopped): service.preflight(run, bad)
    assert service.get_run(run)["status"] == "STOPPED"


def test_live_pilot_preflight_read_only(service):
    run = start(service); result = service.preflight(run)
    assert result["read_only"] and not result["writes_performed"]
    assert [x[0] for x in service.fake.calls] == ["preflight"]


def test_live_pilot_exact_max_10_products(service):
    run = start(service); service.preflight(run)
    assert len(service.preview_products(run, limit=10)["items"]) == 10
    run2 = start(service); service.preflight(run2)
    with pytest.raises(ValueError): service.preview_products(run2, limit=11)


def test_live_pilot_forces_draft(service):
    run = start(service); service.preflight(run)
    preview = service.preview_products(run, preview={"run_id": "X", "items": [
        {"source_id": "A1", "title": "x", "selling_price": 10, "status": "ACTIVE"}]})
    assert preview["items"][0]["status"] == "DRAFT"
    assert preview["items"][0]["media_mode"] == "MANUAL_MEDIA"


def test_live_pilot_requires_explicit_confirmation(service):
    run = start(service); service.preflight(run); service.preview_products(run)
    with pytest.raises(RuntimeError): service.write_products(run)
    assert not any(call[0] == "write_products" for call in service.fake.calls)


def test_live_pilot_stops_on_identity_mismatch(service):
    run = start(service); service.preflight(run); service.preview_products(run); service.write_products(run, confirmed=True)
    with pytest.raises(LivePilotStopped):
        service.verify_products(run, [{"identity_match": False, "duplicate": False, "status": "DRAFT", "verified": False}])
    assert service.get_run(run)["status"] == "STOPPED"


def test_live_pilot_verifies_remote_product(service):
    run = through_products(service)
    assert service.get_run(run)["checkpoint"]["product_verify"]["status"] == "VERIFIED"


def test_live_pilot_collection_max_3(service):
    run = through_products(service)
    assert len(service.preview_collections(run)["items"]) == 3


def test_live_pilot_does_not_duplicate_existing_collection(service):
    run = through_products(service)
    preview = {"items": [{"title": "Trunk Organizers", "action": "SAFE ADOPT", "remote_id": "C1"},
                         {"title": "Trunk Organizers", "action": "CONFLICT", "remote_id": "C1"}]}
    result = service.preview_collections(run, preview)
    assert all(item["action"] != "CREATE" for item in result["items"])


def test_live_pilot_navigation_preserves_unrelated_items(service):
    existing = [{"title": "Home", "url": "/"}, {"title": "Shop", "items": [{"title": "Old"}]},
                {"title": "Contact", "url": "/pages/contact"}]
    merged = service.preserve_navigation(existing, {"title": "Shop", "items": [{"title": "Trunk"}]})
    assert merged[0] == existing[0] and merged[2] == existing[2] and merged[1]["items"][0]["title"] == "Trunk"


def test_live_pilot_brand_existing_asset_nochange(service):
    assert service.brand_asset_status({"logo": "x", "favicon": "y"}) == {
        "logo": "EXISTING_VERIFIED", "favicon": "EXISTING_VERIFIED"}


def test_live_pilot_theme_requires_high_confidence(service):
    run = through_collections(service)
    service.preview_navigation(run, [], None, scopes={x: True for x in service.REQUIRED_NAV_SCOPES})
    service.write_navigation(run, confirmed=True); service.verify_navigation(run, {"verified": True, "unrelated_preserved": True})
    service.inspect_brand_theme(run, {"logo": "x", "favicon": "y", "published_theme": "T"})
    result = service.preview_homepage(run, {"published_theme": "T", "high_confidence_mapping": False,
                                            "backup_ready": True, "images_approved": True})
    assert result["apply_status"] == "MANUAL_ACTION_REQUIRED"


def test_live_pilot_theme_backup_required(service):
    run = through_collections(service)
    service.preview_navigation(run, [], None, scopes={x: True for x in service.REQUIRED_NAV_SCOPES})
    service.write_navigation(run, confirmed=True); service.verify_navigation(run, {"verified": True, "unrelated_preserved": True})
    service.inspect_brand_theme(run, {})
    result = service.preview_homepage(run, {"published_theme": "T", "high_confidence_mapping": True,
                                            "backup_ready": False, "images_approved": True})
    assert result["apply_status"] == "MANUAL_ACTION_REQUIRED"


def test_live_pilot_policies_not_written_without_business_input(service):
    result = service.policy_preview({})
    assert result["status"] == "REQUIRES_BUSINESS_INPUT" and not result["publish_allowed"]


def test_live_pilot_commerce_settings_read_only(service):
    result = service.commerce_readiness({"shipping": "READY", "payment": "NEEDS_REVIEW"})
    assert result["read_only"] and not result["writes_performed"]


def test_live_pilot_report_no_secrets(service):
    run = start(service)
    snap = service.fake.preflight("001") | {"note": "Bearer abcdefghijklmnopqrstuvwxyz123456"}
    service.preflight(run, snap)
    text = (Path(service.get_run(run)["report_dir"]) / "preflight.json").read_text(encoding="utf-8")
    assert "abcdefghijklmnopqrstuvwxyz" not in text and "[REDACTED]" in text


def test_live_pilot_no_delete_mutation(service):
    source = Path(__file__).parents[1] / "src" / "shopsource" / "live_pilot.py"
    text = source.read_text(encoding="utf-8")
    assert "mutation productDelete" not in text and "mutation collectionDelete" not in text


def test_live_pilot_stop_condition_persists(service):
    run = start(service)
    with pytest.raises(LivePilotStopped): service.stop(run, "unexpected mutation")
    reloaded = ControlledLivePilotService(db=service.db, adapter=FakeAdapter(), reports_root=service.reports_root)
    assert reloaded.get_run(run)["status"] == "STOPPED"
    with pytest.raises(LivePilotStopped): reloaded.preflight(run)


def test_live_pilot_protected_file_untouched(service):
    path = Path(__file__).parents[1] / "stores" / "001_cabin_tidy.json"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    run = start(service); service.preflight(run); service.preview_products(run)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before

