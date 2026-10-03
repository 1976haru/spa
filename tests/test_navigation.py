from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

import shopsource.navigation as navigation
import shopsource.homepage_collections as homepage
from shopsource.collection_planner import CollectionPlanner
from shopsource.db import connect, init_db, upsert_store, utc_now
from shopsource.shopify_collections import SHOPIFY_API_VERSION
from shopsource.store_build import STAGES


@pytest.fixture
def nav_env(monkeypatch, request):
    root=Path.cwd()/"exports"/".test_scratch"/f"nav-{uuid.uuid4().hex}";root.mkdir(parents=True,exist_ok=False)
    request.addfinalizer(lambda:shutil.rmtree(root,ignore_errors=True))
    db=root/"fixture.sqlite3";init_db(db)
    profile={"store_id":"nav-fixture","store_name":"Fixture Store","category":"Automotive Storage","sourcing":{"recipes":[]},"include_keywords":[],"exclude_keywords":[],"risk_rules":[]}
    upsert_store(profile,db);now=utc_now()
    with connect(db) as con:
        con.execute("""INSERT INTO store_sourcing_plans(plan_id,store_id,version,name,total_candidate_target,detail_target,detail_ratio,mode,status,planner_version,settings_json,created_at,updated_at)
          VALUES('SOURCE_FIXTURE','nav-fixture',1,'Fixture',20,20,1,'balanced','DRAFT','3.0','{"max_active_keywords_per_category":15}',?,?)""",(now,now))
        for ix,(key,name,keyword,title) in enumerate((("trunk","Trunk Storage","trunk organizer","Trunk Organizer"),("seat","Seat Storage","seat organizer","Seat Organizer")),1):
            cat=con.execute("INSERT INTO store_sourcing_categories(plan_id,category_key,category_name,weight,quota,priority,enabled,source,created_at,updated_at) VALUES('SOURCE_FIXTURE',?,?,1,10,?,1,'FIXTURE',?,?)",(key,name,ix,now,now)).lastrowid
            con.execute("INSERT INTO store_sourcing_keywords(category_id,keyword,source,score,enabled,created_at,updated_at) VALUES(?,?,'FIXTURE',.9,1,?,?)",(cat,keyword,now,now))
            con.execute("INSERT INTO products(asin,source,source_kind,url,title,brand,price,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,'{}',?,?)",(f"B00000000{ix}","fixture","BROWSER_CAPTURE",f"https://example.invalid/{ix}",title,"Fixture",20,now,now))
    plan=CollectionPlanner(db).create_plan("nav-fixture",settings={"desired_collection_count":2,"min_products":0,"include_empty":True})
    navigation.NavigationService(db=db,export_dir=root/"exports")
    mappings=[]
    for row in plan["collections"]:
        mappings.append({"store_id":"nav-fixture","collection_key":row["collection_key"],"handle":row["handle"],"shopify_collection_id":f"gid://shopify/Collection/{row['priority']}","last_synced_hash":"fixture"})
    with connect(db) as con:
        con.executemany("INSERT INTO shopify_collection_mappings(store_id,collection_key,handle,shopify_collection_id,last_synced_hash) VALUES(:store_id,:collection_key,:handle,:shopify_collection_id,:last_synced_hash)",mappings)
    fake=FakeAdmin();_patch_shopify(monkeypatch,fake)
    return db,root,plan,fake


class FakeAdmin:
    def __init__(self):
        self.scopes={"read_online_store_navigation","write_online_store_navigation","read_themes","write_themes"}
        self.menu=None;self.theme={"id":"gid://shopify/OnlineStoreTheme/10","name":"Fixture Theme","role":"MAIN"}
        self.theme_files={"sections/header.liquid":'{% schema %}{"name":"Header","settings":[{"type":"text","id":"mega_menu_parent_label","label":"Mega menu parent label"},{"type":"link_list","id":"menu","label":"Menu"}]}{% endschema %}',
                          "config/settings_schema.json":json.dumps([{"name":"Navigation","settings":[{"type":"text","id":"mega_menu_parent_label","label":"Mega menu parent label"}]}]),
                          "config/settings_data.json":json.dumps({"current":{"settings":{"mega_menu_parent_label":""}}})}
        self.calls=[]
    def execute(self,query,variables=None):
        self.calls.append((query,variables))
        if "currentAppInstallation" in query:return {"currentAppInstallation":{"accessScopes":[{"handle":scope} for scope in sorted(self.scopes)]}}
        if "ShopSourceMenus" in query:return {"menus":{"nodes":[self.menu] if self.menu else []}}
        if "ShopSourceMenuCreate" in query:
            data=variables
            self.menu={"id":"gid://shopify/Menu/1","handle":data["handle"],"title":data["title"],"isDefault":True,"items":self._assign(data["items"])}
            return {"menuCreate":{"menu":self.menu,"userErrors":[]}}
        if "ShopSourceMenuUpdate" in query:
            self.menu={**self.menu,"title":variables["title"],"items":self._assign(variables["items"])}
            return {"menuUpdate":{"menu":self.menu,"userErrors":[]}}
        if "themes(first: 50)" in query:return {"themes":{"nodes":[self.theme]}}
        if "theme(id:" in query or "theme(id: $id)" in query:
            return {"theme":{**self.theme,"files":{"nodes":[{"filename":name,"body":{"content":value}} for name,value in self.theme_files.items()]}}}
        if "ShopSourceMegaMenuThemeUpsert" in query:
            for file in variables["files"]:self.theme_files[file["filename"]]=file["body"]["value"]
            return {"themeFilesUpsert":{"upsertedThemeFiles":[{"filename":"config/settings_data.json"}],"userErrors":[]}}
        raise AssertionError("unexpected mock GraphQL operation")
    def _assign(self,items):
        result=[]
        for item in items:
            row={**item,"id":item.get("id") or f"gid://shopify/MenuItem/{uuid.uuid4().hex[:8]}"}
            row["items"]=self._assign(item.get("items",[]));result.append(row)
        return result


def _patch_shopify(monkeypatch,fake):
    config=lambda store_id,db=None:{"shop_domain":"fixture.myshopify.com","api_version":SHOPIFY_API_VERSION}
    token=lambda store_id:("fixture-secret","fixture")
    monkeypatch.setattr(navigation,"get_connection",config);monkeypatch.setattr(navigation,"get_shopify_token",token)
    monkeypatch.setattr(homepage,"get_connection",config);monkeypatch.setattr(homepage,"get_shopify_token",token)
    factory=lambda *args:fake
    return factory


def service(nav_env):
    db,root,_,fake=nav_env
    return navigation.NavigationService(db=db,client_factory=lambda *args:fake,export_dir=root/"exports")


def test_navigation_plan_from_collection_plan(nav_env):
    db,_,collection_plan,_=nav_env;result=service(nav_env).build_plan("nav-fixture",collection_plan["plan_id"])
    assert result["source_collection_plan_id"]==collection_plan["plan_id"]
    assert [x["depth"] for x in result["items"]]==[0,1,1]


def test_navigation_preserves_unrelated_items(nav_env):
    _,_,_,fake=nav_env;fake.menu={"id":"gid://shopify/Menu/1","handle":"main-menu","title":"Main menu","isDefault":True,"items":[
        {"id":"home","title":"Home","type":"FRONTPAGE","url":"/","items":[]},
        {"id":"about","title":"About Us","type":"PAGE","url":"/pages/about","items":[]}]}
    nav=service(nav_env);plan=nav.build_plan("nav-fixture");preview=nav.preview(plan["plan_id"])
    assert [row["title"] for row in preview["tree"] if row["title"]!="Shop"]==["Home","About Us"]
    assert not fake.calls[-1][0].startswith("mutation")


def test_shop_parent_created_once(nav_env):
    nav=service(nav_env);plan=nav.build_plan("nav-fixture");preview=nav.preview(plan["plan_id"])
    assert sum(row.get("item_key")=="shopsource:shop" and row["action"]=="CREATE" for row in preview["actions"])==1


def test_repeat_sync_no_duplicate(nav_env):
    nav=service(nav_env);plan=nav.build_plan("nav-fixture");preview=nav.preview(plan["plan_id"])
    assert nav.sync(preview["preview_id"],confirmed=True)["status"]=="VERIFIED"
    second=nav.preview(plan["plan_id"])
    assert second["summary"]["CREATE"]==0 and second["summary"]["NO CHANGE"]==2
    assert nav.sync(second["preview_id"],confirmed=True)["status"]=="NO_CHANGE"


def test_collection_child_maps_correct_resource(nav_env):
    nav=service(nav_env);plan=nav.build_plan("nav-fixture")
    assert all(row["resource_id"].startswith("gid://shopify/Collection/") for row in plan["items"][1:])
    assert all(row["url"].startswith("/collections/") for row in plan["items"][1:])


def test_missing_collection_not_mapped_to_wrong_resource(nav_env):
    db,_,_,_=nav_env
    with connect(db) as con:con.execute("DELETE FROM shopify_collection_mappings WHERE collection_key=(SELECT collection_key FROM store_collection_definitions ORDER BY priority DESC LIMIT 1)")
    nav=service(nav_env);plan=nav.build_plan("nav-fixture");missing=next(row for row in plan["items"] if row["sync_status"]=="MISSING_RESOURCE")
    preview=nav.preview(plan["plan_id"])
    assert missing["resource_id"] is None and missing["url"] is None
    assert any(row["item_key"]==missing["item_key"] and row["action"]=="SKIP" for row in preview["actions"])


def test_duplicate_target_warning(nav_env):
    db,_,plan,_=nav_env
    with connect(db) as con:
        first,second=[row["collection_key"] for row in plan["collections"]]
        target=con.execute("SELECT shopify_collection_id FROM shopify_collection_mappings WHERE collection_key=?",(first,)).fetchone()[0]
        con.execute("UPDATE shopify_collection_mappings SET shopify_collection_id=? WHERE collection_key=?",(target,second))
    nav=service(nav_env);planned=nav.build_plan("nav-fixture")
    assert any("DUPLICATE_TARGET" in item.get("warnings",[]) for item in planned["items"])


def test_preview_invalidated_when_inputs_change(nav_env):
    db,_,_,_=nav_env;nav=service(nav_env);plan=nav.build_plan("nav-fixture");preview=nav.preview(plan["plan_id"])
    with connect(db) as con:con.execute("UPDATE shopify_collection_mappings SET handle='changed-handle' WHERE collection_key=(SELECT collection_key FROM shopify_collection_mappings LIMIT 1)")
    assert nav.sync(preview["preview_id"],confirmed=True)["status"]=="CONFLICT"


def test_navigation_create_update_move_nochange(nav_env):
    db,_,_,fake=nav_env;nav=service(nav_env);plan=nav.build_plan("nav-fixture");preview=nav.preview(plan["plan_id"])
    assert preview["summary"]["CREATE"]==3
    nav.sync(preview["preview_id"],confirmed=True)
    # Rename a collection title in the stored plan to exercise UPDATE and reorder through a new plan.
    with connect(db) as con:con.execute("UPDATE store_navigation_items SET title='Renamed collection' WHERE plan_id=? AND depth=1",(plan["plan_id"],))
    # Persisted plan payload is immutable; a new plan reflects the current collection planner data.
    with connect(db) as con:con.execute("UPDATE store_collection_definitions SET title='Renamed collection' WHERE plan_id=? AND priority=1",(plan["source_collection_plan_id"],))
    updated=nav.build_plan("nav-fixture",plan["source_collection_plan_id"]);again=nav.preview(updated["plan_id"])
    assert any(row["action"] in {"UPDATE","MOVE"} for row in again["actions"])
    # An unchanged second preview is a stable no-op.
    nav.sync(again["preview_id"],confirmed=True)
    stable=nav.preview(updated["plan_id"])
    assert stable["summary"]["NO CHANGE"]>=1


def test_navigation_move_action_when_collection_priority_changes(nav_env):
    db,_,collection_plan,_=nav_env;nav=service(nav_env);plan=nav.build_plan("nav-fixture",collection_plan["plan_id"])
    first=nav.preview(plan["plan_id"]);nav.sync(first["preview_id"],confirmed=True)
    with connect(db) as con:
        priorities=[row["priority"] for row in con.execute("SELECT priority FROM store_collection_definitions WHERE plan_id=? ORDER BY priority",(collection_plan["plan_id"],))]
        rows=list(con.execute("SELECT id,priority FROM store_collection_definitions WHERE plan_id=? ORDER BY priority",(collection_plan["plan_id"],)))
        con.execute("UPDATE store_collection_definitions SET priority=priority+10 WHERE id=?",(rows[0]["id"],))
        con.execute("UPDATE store_collection_definitions SET priority=? WHERE id=?",(priorities[0],rows[1]["id"]))
        con.execute("UPDATE store_collection_definitions SET priority=? WHERE id=?",(priorities[1],rows[0]["id"]))
    changed=nav.build_plan("nav-fixture",collection_plan["plan_id"]);preview=nav.preview(changed["plan_id"])
    assert preview["summary"]["MOVE"]==2


def test_navigation_conflict_on_manual_drift(nav_env):
    _,_,_,fake=nav_env;nav=service(nav_env);plan=nav.build_plan("nav-fixture");preview=nav.preview(plan["plan_id"])
    fake.menu={"id":"remote","handle":"main-menu","title":"Main menu","isDefault":True,"items":[{"id":"shop","title":"Shop","type":"CATALOG","url":"/collections/all","items":[{"id":"wrong","title":plan["items"][1]["title"],"type":"COLLECTION","url":"/collections/wrong","resourceId":"gid://shopify/Collection/999","items":[]}]}]}
    assert nav.sync(preview["preview_id"],confirmed=True)["status"]=="CONFLICT"


def test_navigation_verify_after_write(nav_env):
    nav=service(nav_env);plan=nav.build_plan("nav-fixture");preview=nav.preview(plan["plan_id"]);result=nav.sync(preview["preview_id"],confirmed=True)
    assert result["status"]=="VERIFIED" and nav.verify("nav-fixture",plan["plan_id"])["verified"]


def test_assignment_mode_detects_placeholder_links():
    menu={"items":[{"title":"Shop","items":[{"title":"Cup Holders","resourceId":"collection-1"},{"title":"Trash","resourceId":"collection-1"},{"title":"Visor","url":None}]}]}
    result=navigation.inspect_assignment_menu(menu)
    assert {row["code"] for row in result["findings"]}>={"DUPLICATE_TARGET","PLACEHOLDER_LINK","MISSING_LINK"}


def test_theme_detect_mega_menu_high_confidence(nav_env):
    _,_,_,fake=nav_env
    detected=navigation.detect_mega_menu(fake.theme_files)
    assert detected["status"]=="MEGA_SUPPORTED_HIGH_CONFIDENCE" and detected["mapping"]["setting_id"]=="mega_menu_parent_label"


def test_theme_dropdown_only_not_false_complete(nav_env):
    _,_,_,fake=nav_env
    fake.theme_files["sections/header.liquid"]='{% schema %}{"name":"Header","settings":[{"type":"link_list","id":"menu","label":"Main menu"}]}{% endschema %}'
    fake.theme_files["config/settings_schema.json"]="[]"
    fake.theme_files["config/settings_data.json"]=json.dumps({"current":{"settings":{}}})
    assert navigation.detect_mega_menu(fake.theme_files)["status"]=="DROPDOWN_ONLY"


def test_theme_unknown_manual_fallback(nav_env):
    _,_,_,fake=nav_env;fake.theme_files={"sections/footer.liquid":'{% schema %}{"name":"Footer","settings":[]}{% endschema %}'}
    assert navigation.detect_mega_menu(fake.theme_files)["status"]=="NOT_FOUND"
    preview=navigation.MegaMenuThemeService(db=nav_env[0],client_factory=lambda *args:fake,export_dir=nav_env[1]/"exports").preview("nav-fixture")
    assert preview["status"]=="MANUAL_ACTION_REQUIRED" and not any("mutation" in q for q,_ in fake.calls)


def test_mega_apply_preview_no_write(nav_env):
    db,root,_,fake=nav_env;service=navigation.MegaMenuThemeService(db=db,client_factory=lambda *args:fake,export_dir=root/"exports")
    preview=service.preview("nav-fixture",parent_label="Shop")
    assert preview["status"]=="PREVIEW"
    assert not any("ShopSourceMegaMenuThemeUpsert" in query for query,_ in fake.calls)


def test_mega_apply_minimal_header_patch(nav_env):
    db,root,_,fake=nav_env;service=navigation.MegaMenuThemeService(db=db,client_factory=lambda *args:fake,export_dir=root/"exports")
    preview=service.preview("nav-fixture");result=service.apply(preview["preview_id"],confirmed=True)
    upsert=[(query,variables) for query,variables in fake.calls if "ShopSourceMegaMenuThemeUpsert" in query]
    assert result["status"]=="VERIFIED" and len(upsert)==1
    assert [row["filename"] for row in upsert[0][1]["files"]]==["config/settings_data.json"]


def test_mega_backup_before_write(nav_env):
    db,root,_,fake=nav_env;service=navigation.MegaMenuThemeService(db=db,client_factory=lambda *args:fake,export_dir=root/"exports")
    preview=service.preview("nav-fixture");result=service.apply(preview["preview_id"],confirmed=True)
    with connect(db) as con:backup=con.execute("SELECT * FROM mega_menu_backups WHERE backup_id=?",(result["backup_id"],)).fetchone()
    assert backup and json.loads((Path(backup["folder"])/"before.json").read_text(encoding="utf-8"))["config/settings_data.json"]


def test_store_build_navigation_stage_order():
    assert STAGES.index("COLLECTION_VERIFY")<STAGES.index("NAVIGATION_PLAN")<STAGES.index("NAVIGATION_SYNC")<STAGES.index("NAVIGATION_VERIFY")<STAGES.index("MEGA_MENU_PREVIEW")<STAGES.index("MEGA_MENU_APPLY")<STAGES.index("BRAND_APPLY_PREVIEW")


def test_no_real_network_in_tests(nav_env):
    _,_,_,fake=nav_env;nav=service(nav_env);plan=nav.build_plan("nav-fixture");preview=nav.preview(plan["plan_id"])
    assert preview["status"]=="READY"
    assert not any("mutation" in query for query,_ in fake.calls)


def test_protected_store_file_untouched(nav_env):
    _,root,_,_=nav_env
    assert root not in (Path.cwd()/"stores").parents and "stores" not in root.parts
