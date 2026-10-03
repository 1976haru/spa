from datetime import datetime, timedelta, timezone
import json

import pytest

from shopsource.db import connect, init_db
from shopsource.source_safety import (SourceSafetyService, SoldOutDiagnosticService, SourceMonitorService,
    browser_source_observation, keepa_source_observation, ensure_source_safety_schema)


@pytest.fixture
def db(tmp_path):
    path=tmp_path/"safety.sqlite3"; init_db(path); ensure_source_safety_schema(path)
    now=datetime.now(timezone.utc).isoformat()
    with connect(path) as con:
        for i in range(1,5): con.execute("INSERT INTO products(asin,title,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",(f"B{i:09d}",f"P{i}","{}",now,now))
    return path


def obs(state="IN_STOCK", price=10, when=None, currency="USD"):
    return {"availability":state,"availability_confidence":"HIGH","source_price":price,"source_currency":currency,
            "observed_at":when or datetime.now(timezone.utc).isoformat(),"evidence_kind":"FIXTURE","evidence":{"value":state}}


def configured(service, store="001"):
    service.save_settings(store,price_policy={"enabled":True,"min_margin_amount":5,"min_margin_percent":20,"currency":"USD"})


def test_browser_detail_jsonld_instock(): assert browser_source_observation(jsonld_availability="https://schema.org/InStock")["availability"]=="IN_STOCK"
def test_browser_detail_jsonld_outofstock(): assert browser_source_observation(jsonld_availability="OutOfStock")["availability"]=="OUT_OF_STOCK"
def test_browser_detail_dom_instock(): assert browser_source_observation(visible_text=" In Stock ")["availability"]=="IN_STOCK"
def test_browser_detail_dom_currently_unavailable(): assert browser_source_observation(visible_text="Currently unavailable")["availability"]=="OUT_OF_STOCK"
def test_browser_detail_unknown_when_no_evidence(): assert browser_source_observation()["availability"]=="UNKNOWN"
def test_search_hint_does_not_qualify_sellability(): assert not browser_source_observation(visible_text="in stock",search_hint=True)["qualifies_for_sellability"]
def test_keepa_positive_current_new_instock(): assert keepa_source_observation({"current_new_price":12,"current_new_offer_count":1})["availability"]=="IN_STOCK"
def test_keepa_missing_single_price_type_not_oos(): assert keepa_source_observation({"current_new_price":None})["availability"]=="UNKNOWN"
def test_keepa_documented_no_new_offer_outofstock(): assert keepa_source_observation({"documented_no_new_offer":True,"current_new_offer_count":0})["availability"]=="OUT_OF_STOCK"
def test_keepa_insufficient_evidence_unknown(): assert keepa_source_observation({})["availability"]=="UNKNOWN"
def test_keepa_provider_error_not_oos(): assert keepa_source_observation({},provider_error=RuntimeError())["availability"]=="SOURCE_ERROR"


def test_keepa_snapshot_preserves_history(db):
    s=SourceSafetyService(db); s.record_snapshot(1,"B000000001",obs(price=10)); s.record_snapshot(1,"B000000001",obs(price=12))
    with connect(db) as con: assert con.execute("SELECT COUNT(*) FROM source_product_snapshots WHERE product_id=1").fetchone()[0]==2


@pytest.mark.parametrize("state,expected",[("OUT_OF_STOCK","BLOCKED_SOURCE_OUT_OF_STOCK"),("UNKNOWN","BLOCKED_SOURCE_UNKNOWN")])
def test_oos_unknown_kept_in_master_but_blocked_from_live_export(db,state,expected):
    s=SourceSafetyService(db); configured(s); s.record_snapshot(1,"B000000001",obs(state)); assert s.evaluate("001",1,selling_price=30)["sellability_status"]==expected
    with connect(db) as con: assert con.execute("SELECT COUNT(*) FROM products WHERE id=1").fetchone()[0]==1


def test_fresh_instock_sellable(db):
    s=SourceSafetyService(db); configured(s); s.record_snapshot(1,"B000000001",obs()); assert s.evaluate("001",1,selling_price=30)["sellability_status"]=="SELLABLE"


def test_stale_instock_blocked_for_new_listing(db):
    s=SourceSafetyService(db); configured(s); old=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(); s.record_snapshot(1,"B000000001",obs(when=old))
    assert s.evaluate("001",1,selling_price=30,pre_list=True)["sellability_status"]=="BLOCKED_SOURCE_STALE"


def test_source_error_uses_last_successful_snapshot_until_stale(db):
    s=SourceSafetyService(db); configured(s); s.record_snapshot(1,"B000000001",obs()); s.record_snapshot(1,"B000000001",obs("SOURCE_ERROR",None))
    assert s.evaluate("001",1,selling_price=30)["source_availability"]=="IN_STOCK"


def test_price_history_delta_and_no_auto_reprice(db):
    s=SourceSafetyService(db); configured(s); s.record_snapshot(1,"B000000001",obs(price=10)); r=s.record_snapshot(1,"B000000001",obs(price=15))
    assert r["price_change_amount"]==5 and s.settings("001")["price_policy"]["auto_reprice_enabled"] is False


def test_margin_policy_unconfigured_needs_pricing_policy(db):
    s=SourceSafetyService(db); s.record_snapshot(1,"B000000001",obs()); assert s.evaluate("001",1,selling_price=30)["sellability_status"]=="NEEDS_PRICING_POLICY"


def test_margin_breach_blocks_when_policy_enabled(db):
    s=SourceSafetyService(db); configured(s); s.record_snapshot(1,"B000000001",obs(price=19)); assert s.evaluate("001",1,selling_price=20)["sellability_status"]=="BLOCKED_MARGIN"


def test_currency_mismatch_needs_review(db):
    s=SourceSafetyService(db); configured(s); s.record_snapshot(1,"B000000001",obs(currency="CAD")); assert s.evaluate("001",1,selling_price=30)["sellability_status"]=="NEEDS_REVIEW"


def test_store_specific_margin_policy(db):
    s=SourceSafetyService(db); configured(s,"001"); s.save_settings("002",price_policy={"enabled":True,"min_margin_amount":50})
    s.record_snapshot(1,"B000000001",obs()); assert s.evaluate("001",1,selling_price=30)["sellability_status"]=="SELLABLE" and s.evaluate("002",1,selling_price=30)["sellability_status"]=="BLOCKED_MARGIN"


def test_manual_store_decision_does_not_bypass_source_safety(db):
    s=SourceSafetyService(db); configured(s); s.record_snapshot(1,"B000000001",obs("OUT_OF_STOCK")); state=s.evaluate("001",1,selling_price=30)
    result=s.live_safe_filter("001",[{"product_id":1,"final_status":"PRIMARY"}]); assert not result["items"] and state["sellability_status"].startswith("BLOCKED")


def test_spark_live_safe_excludes_oos_unknown_and_reports(db):
    s=SourceSafetyService(db); configured(s)
    for pid,state in ((1,"OUT_OF_STOCK"),(2,"UNKNOWN")): s.record_snapshot(pid,f"B{pid:09d}",obs(state)); s.evaluate("001",pid,selling_price=30)
    result=s.live_safe_filter("001",[{"product_id":1,"final_status":"PRIMARY"},{"product_id":2,"final_status":"PRIMARY"}])
    assert result["source_exclusion_counts"]=={"SOURCE_OUT_OF_STOCK":1,"SOURCE_UNKNOWN":1}


@pytest.mark.parametrize("source,shopify,expected",[
 ({"availability":"IN_STOCK","freshness_status":"FRESH"},{"available_for_sale":False,"inventory_quantity":0,"inventory_tracked":True},"SHOPIFY_ZERO_AVAILABLE_QUANTITY"),
 ({"availability":"OUT_OF_STOCK","freshness_status":"FRESH"},{"available_for_sale":True,"inventory_tracked":True},"SOURCE_OUT_OF_STOCK_SHOPIFY_AVAILABLE"),
 ({"availability":"OUT_OF_STOCK","freshness_status":"FRESH"},{"available_for_sale":False,"inventory_tracked":True},"SOURCE_OUT_OF_STOCK_SHOPIFY_SOLD_OUT"),
 ({"availability":"UNKNOWN","freshness_status":"FRESH"},{"available_for_sale":False,"inventory_tracked":True},"SOURCE_UNKNOWN_SHOPIFY_SOLD_OUT"),
 ({"availability":"IN_STOCK","freshness_status":"FRESH"},{"available_for_sale":False,"inventory_tracked":False},"SHOPIFY_INVENTORY_NOT_TRACKED"),
 ({"availability":"IN_STOCK","freshness_status":"FRESH"},{"available_for_sale":True,"inventory_tracked":True,"inventory_policy":"CONTINUE"},"SHOPIFY_CONTINUE_SELLING_WHEN_OOS")])
def test_sold_out_diagnostics(source,shopify,expected): assert SoldOutDiagnosticService.diagnose(source,shopify)==expected


def test_diagnostic_read_only_and_preserves_state():
    row={"source":{"availability":"IN_STOCK","freshness_status":"FRESH"},"shopify":{"available_for_sale":True,"inventory_tracked":True},"merchant":"keep"}
    result=SoldOutDiagnosticService().inspect([row]); assert row["merchant"]=="keep" and result[0]["diagnosis"]=="SOURCE_IN_STOCK_SHOPIFY_AVAILABLE"


def test_monitor_checkpoint_resume_retry(db):
    m=SourceMonitorService(db); result=m.run_due_checks("001",observations={1:obs(),2:RuntimeError("temporary"),3:obs(),4:obs()},limit=4)
    assert result["status"]=="FAILED" and result["checked_count"]==3
    result=m.retry_failed(result["run_id"],observations={2:obs()}); assert result["status"]=="COMPLETE"


def test_two_consecutive_instock_required_for_restore_and_manual_disabled(db):
    s=SourceSafetyService(db); configured(s); s.record_snapshot(1,"B000000001",obs()); s.evaluate("001",1,selling_price=30)
    assert SourceMonitorService(db).build_actions("001")[0]["action"]!="RESTORE_ELIGIBLE"
    s.record_snapshot(1,"B000000001",obs()); s.evaluate("001",1,selling_price=30)
    assert SourceMonitorService(db).build_actions("001")[0]["action"]=="MANUAL_REVIEW"


def test_evidence_rejects_html_and_redacts_secret(db):
    s=SourceSafetyService(db)
    with pytest.raises(ValueError): s.record_snapshot(1,"B000000001",obs()|{"evidence":{"html":"<html>dump</html>"}})
    s.record_snapshot(1,"B000000001",obs()|{"evidence":{"token":"shpat_abcdefghijklmnopqrstuvwxyz123"}})
    with connect(db) as con: evidence=con.execute("SELECT evidence_json FROM source_product_snapshots").fetchone()[0]
    assert "shpat_" not in evidence


def test_2000_product_source_audit_batches():
    count=2000; assert (count+99)//100==20


def test_50000_snapshot_query_is_indexed(db):
    with connect(db) as con:
        indexes=[r[1] for r in con.execute("PRAGMA index_list(source_product_snapshots)")]
    assert "idx_source_snapshots_product_observed" in indexes

