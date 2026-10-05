"""Collection-driven Shopify navigation and cautious mega-menu automation.

The default path is local planning and read-only preview. All remote mutation
calls are explicit methods and injectable for tests.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .db import connect, init_db
from .homepage_collections import ShopifyThemeReader, _body_content, _section_schema
from .paths import EXPORT_DIR
from .shopify_collections import ShopifyGraphQLClient, get_connection, get_shopify_token

READ_SCOPE = "read_online_store_navigation"
WRITE_SCOPE = "write_online_store_navigation"
MENUS_QUERY = """query ShopSourceMenus { menus(first:100) { nodes { id handle title isDefault items { id title type url resourceId tags items { id title type url resourceId tags items { id title type url resourceId tags } } } } } }"""
MENU_CREATE = """mutation ShopSourceMenuCreate($title:String!,$handle:String!,$items:[MenuItemCreateInput!]!) { menuCreate(title:$title,handle:$handle,items:$items) { menu { id handle title items { id title type url resourceId items { id title type url resourceId } } } userErrors { field message } } }"""
MENU_UPDATE = """mutation ShopSourceMenuUpdate($id:ID!,$title:String!,$items:[MenuItemUpdateInput!]!) { menuUpdate(id:$id,title:$title,items:$items) { menu { id handle title items { id title type url resourceId items { id title type url resourceId } } } userErrors { field message } } }"""
THEME_UPSERT = """mutation ShopSourceMegaMenuThemeUpsert($themeId:ID!,$files:[OnlineStoreThemeFilesUpsertFileInput!]!) { themeFilesUpsert(themeId:$themeId,files:$files) { upsertedThemeFiles { filename } userErrors { field message } } }"""


def _now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def _hash(value): return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
def _slug(value): return re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")[:90] or "shop-menu"


def _ensure(db=None):
    init_db(db)
    with connect(db) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS store_navigation_plans(
          plan_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,source_collection_plan_id TEXT,plan_version INTEGER NOT NULL,
          status TEXT NOT NULL,root_menu_handle TEXT NOT NULL,root_menu_title TEXT NOT NULL,plan_json TEXT NOT NULL,
          input_hash TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS store_navigation_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT,plan_id TEXT NOT NULL,item_key TEXT NOT NULL,parent_item_key TEXT,
          title TEXT NOT NULL,link_type TEXT NOT NULL,resource_id TEXT,url TEXT,position INTEGER NOT NULL,depth INTEGER NOT NULL,
          enabled INTEGER NOT NULL,source_collection_key TEXT,shopify_menu_item_id TEXT,sync_status TEXT NOT NULL,
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(plan_id,item_key));
        CREATE TABLE IF NOT EXISTS navigation_sync_previews(
          preview_id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,store_id TEXT NOT NULL,menu_id TEXT,menu_hash TEXT NOT NULL,
          input_hash TEXT NOT NULL,status TEXT NOT NULL,preview_json TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS navigation_sync_runs(
          run_id TEXT PRIMARY KEY,preview_id TEXT NOT NULL,store_id TEXT NOT NULL,status TEXT NOT NULL,
          result_json TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mega_menu_previews(
          preview_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,theme_id TEXT,settings_hash TEXT NOT NULL,
          status TEXT NOT NULL,preview_json TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mega_menu_backups(
          backup_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,theme_id TEXT NOT NULL,settings_data TEXT NOT NULL,
          folder TEXT NOT NULL,created_at TEXT NOT NULL);
        """)
        # Collection publisher owns this table; create the compatible shape for a fresh local DB.
        con.execute("""CREATE TABLE IF NOT EXISTS shopify_collection_mappings(
          store_id TEXT NOT NULL,collection_key TEXT NOT NULL,handle TEXT NOT NULL,shopify_collection_id TEXT NOT NULL,
          last_synced_hash TEXT NOT NULL DEFAULT '',last_synced_at TEXT NOT NULL DEFAULT '',published_ids_json TEXT NOT NULL DEFAULT '[]',
          image_url TEXT,PRIMARY KEY(store_id,collection_key),UNIQUE(store_id,handle))""")


def _menu_fingerprint(menu):
    def clean(item):
        return {key:item.get(key) for key in ("title","type","url","resourceId","tags")} | {"items":[clean(child) for child in item.get("items",[])]}
    return _hash({"id":(menu or {}).get("id"),"handle":(menu or {}).get("handle"),"title":(menu or {}).get("title"),
                  "items":[clean(item) for item in (menu or {}).get("items",[])]})


def _api_item(item, *, updates):
    allowed=("id","title","type","url","resourceId","tags","items") if updates else ("title","type","url","resourceId","tags","items")
    value={key:item[key] for key in allowed if key in item and item[key] is not None}
    value["items"]=[_api_item(child,updates=updates) for child in item.get("items",[])]
    return value


def _menu_item(item):
    return {key:item.get(key) for key in ("id","title","type","url","resourceId","tags") if item.get(key) is not None} | {"items":[_menu_item(child) for child in item.get("items",[])]}


def inspect_assignment_menu(menu, planned_children=()):
    """Beginner-facing checks; warnings never prevent assignment-mode preview."""
    items=(menu or {}).get("items",[]);parents=[item for item in items if str(item.get("title","")).casefold()=="shop"]
    findings=[]
    if not parents:findings.append({"code":"MISSING_PARENT","severity":"WARNING","message":"Shop 상위 메뉴가 없습니다."})
    if len(parents)>1:findings.append({"code":"DUPLICATE_PARENT","severity":"WARNING","message":"Shop 상위 메뉴가 중복되어 있습니다."})
    children=parents[0].get("items",[]) if len(parents)==1 else []
    target_groups={}
    for child in children:
        target=child.get("resourceId") or child.get("url")
        if not target:findings.append({"code":"MISSING_LINK","label":child.get("title"),"severity":"WARNING"})
        else:target_groups.setdefault(str(target),[]).append(child)
    for target,rows in target_groups.items():
        labels={str(row.get("title","")).casefold() for row in rows}
        if len(rows)>1 and len(labels)>1:
            findings.append({"code":"DUPLICATE_TARGET","target":target,"labels":[row.get("title") for row in rows],"severity":"WARNING",
                             "message":"여러 메뉴가 같은 컬렉션에 연결되어 있습니다. 과제용 임시 연결일 수 있습니다."})
            findings.append({"code":"PLACEHOLDER_LINK","target":target,"severity":"WARNING"})
    labels={str(row.get("title","")).casefold() for row in children}
    for desired in planned_children:
        if desired.get("title","").casefold() not in labels:
            findings.append({"code":"MISSING_CHILD","label":desired.get("title"),"severity":"WARNING"})
    return {"parent_exists":len(parents)==1,"children_indented":bool(parents and children),"child_count":len(children),"findings":findings,
            "ready":not any(row["code"] in {"DUPLICATE_PARENT"} for row in findings)}


class NavigationService:
    def __init__(self, *, db=None, client_factory=ShopifyGraphQLClient, export_dir=None):
        self.db,self.client_factory,self.export_dir=db,client_factory,Path(export_dir) if export_dir else EXPORT_DIR
        _ensure(db)

    def _client(self,store_id):
        config=get_connection(store_id,db=self.db);token,_=get_shopify_token(store_id,db=self.db)
        if not config or not token:raise RuntimeError("Shopify connection/credential is missing")
        return config,self.client_factory(config["shop_domain"],token,config["api_version"])

    def discover_main_menu(self,store_id):
        try:config,client=self._client(store_id)
        except RuntimeError as exc:return {"status":"NOT_CONNECTED","menu":None,"scopes":[],"reason":str(exc)}
        data=client.execute("query ShopSourceNavigationScopes { currentAppInstallation { accessScopes { handle } } }")
        scopes=sorted({row.get("handle","") for row in (data.get("currentAppInstallation") or {}).get("accessScopes",[])})
        if READ_SCOPE not in scopes:return {"status":"MANUAL_ACTION_REQUIRED","menu":None,"scopes":scopes,"reason":f"Missing {READ_SCOPE}"}
        menus=client.execute(MENUS_QUERY).get("menus",{}).get("nodes",[])
        exact=[row for row in menus if str(row.get("handle","")).casefold() in {"main-menu","main_menu"}]
        if not exact:exact=[row for row in menus if str(row.get("title","")).casefold() in {"main menu","main"}]
        if len(exact)>1:return {"status":"AMBIGUOUS","menu":None,"candidates":exact,"scopes":scopes,"menus":menus}
        return {"status":"FOUND" if exact else "NOT_FOUND","menu":exact[0] if exact else None,"scopes":scopes,"menus":menus,
                "write_ready":WRITE_SCOPE in scopes,"shop_domain":config["shop_domain"],"api_version":config["api_version"]}

    def build_plan(self,store_id,collection_plan_id=None,*,options=None):
        from .collection_planner import CollectionPlanner
        planner=CollectionPlanner(self.db)
        if not collection_plan_id:
            with connect(self.db) as con:row=con.execute("SELECT plan_id FROM store_collection_plans WHERE store_id=? ORDER BY updated_at DESC LIMIT 1",(store_id,)).fetchone()
            if not row:raise RuntimeError("Create a Phase 3.2 collection plan before designing navigation")
            collection_plan_id=row["plan_id"]
        collection_plan=planner.get_plan(collection_plan_id)
        if collection_plan["store_id"]!=store_id:raise ValueError("Collection plan belongs to another store")
        options={"parent_label":"Shop","parent_url":"/collections/all",**(options or {})}
        with connect(self.db) as con:
            mappings={row["collection_key"]:dict(row) for row in con.execute("SELECT * FROM shopify_collection_mappings WHERE store_id=?",(store_id,))}
            previous=con.execute("SELECT MAX(plan_version) FROM store_navigation_plans WHERE store_id=?",(store_id,)).fetchone()[0] or 0
        root_key="shopsource:shop";children=[];warnings=[]
        for index,definition in enumerate(sorted(collection_plan["collections"],key=lambda row:(row.get("priority",999),row.get("collection_key","")))):
            if not definition.get("enabled",1):continue
            mapping=mappings.get(definition["collection_key"]);remote_id=(mapping or {}).get("shopify_collection_id");handle=(mapping or {}).get("handle")
            valid=bool(remote_id and handle)
            child={"item_key":f"collection:{definition['collection_key']}","parent_item_key":root_key,"title":definition["title"],
                   "link_type":"COLLECTION","resource_id":remote_id if valid else None,
                   "url":f"/collections/{handle}" if valid else None,"position":index,"depth":1,"enabled":True,
                   "source_collection_key":definition["collection_key"],"sync_status":"PLANNED" if valid else "MISSING_RESOURCE",
                   "collection_handle":handle,"collection_plan_status":"MAPPED" if valid else "PLAN_ONLY",
                   "expected_product_count":definition.get("estimated_product_count",0),"warnings":definition.get("warnings",[])}
            if not valid:warnings.append({"code":"MISSING_REMOTE_ID","collection_key":definition["collection_key"],"title":definition["title"]})
            children.append(child)
        desired_ids={row["resource_id"] for row in children if row["resource_id"]}
        duplicates={rid for rid in desired_ids if sum(row["resource_id"]==rid for row in children)>1}
        for row in children:
            if row["resource_id"] in duplicates:
                row["warnings"].append("DUPLICATE_TARGET");warnings.append({"code":"DUPLICATE_TARGET","resource_id":row["resource_id"]})
        parent={"item_key":root_key,"parent_item_key":None,"title":options["parent_label"],"link_type":"CATALOG","resource_id":None,
                "url":options["parent_url"],"position":1,"depth":0,"enabled":True,"source_collection_key":None,"sync_status":"PLANNED"}
        plan_id="NVP_"+secrets.token_hex(10);now=_now()
        payload={"plan_id":plan_id,"store_id":store_id,"source_collection_plan_id":collection_plan_id,"plan_version":previous+1,
                 "status":"READY","root_menu_handle":"main-menu","root_menu_title":"Main menu","items":[parent,*children],"warnings":warnings,"options":options}
        input_hash=_hash({"collection_plan":collection_plan,"mappings":mappings,"options":options})
        with connect(self.db) as con:
            con.execute("INSERT INTO store_navigation_plans VALUES(?,?,?,?,?,?,?,?,?,?,?)",(plan_id,store_id,collection_plan_id,previous+1,"READY","main-menu","Main menu",json.dumps(payload,ensure_ascii=False),input_hash,now,now))
            for item in payload["items"]:
                con.execute("INSERT INTO store_navigation_items(plan_id,item_key,parent_item_key,title,link_type,resource_id,url,position,depth,enabled,source_collection_key,sync_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (plan_id,item["item_key"],item["parent_item_key"],item["title"],item["link_type"],item["resource_id"],item["url"],item["position"],item["depth"],1,item["source_collection_key"],item["sync_status"],now,now))
        self._report(store_id,plan_id,plan=payload)
        return payload

    def get_plan(self,plan_id):
        with connect(self.db) as con:row=con.execute("SELECT * FROM store_navigation_plans WHERE plan_id=?",(plan_id,)).fetchone()
        if not row:raise KeyError(plan_id)
        result=dict(row);result["plan"]=json.loads(result.pop("plan_json"));return result

    def _input_hash(self,plan,snapshot):
        from .collection_planner import CollectionPlanner
        source_plan=CollectionPlanner(self.db).get_plan(plan["source_collection_plan_id"]) if plan.get("source_collection_plan_id") else None
        with connect(self.db) as con:
            mappings={row["collection_key"]:{"shopify_collection_id":row["shopify_collection_id"],"handle":row["handle"]} for row in con.execute("SELECT collection_key,shopify_collection_id,handle FROM shopify_collection_mappings WHERE store_id=?",(plan["store_id"],))}
        return _hash({"plan_hash":_hash(plan),"source_collection_plan_hash":_hash(source_plan) if source_plan else None,
                      "menu_hash":_menu_fingerprint(snapshot.get("menu")),"mappings":mappings})

    def preview(self,plan_id):
        dbrow=self.get_plan(plan_id);plan=dbrow["plan"];snapshot=self.discover_main_menu(plan["store_id"]);menu=snapshot.get("menu") or {}
        if snapshot["status"] in {"NOT_CONNECTED","MANUAL_ACTION_REQUIRED","AMBIGUOUS"}:
            result={"preview_id":"NVPV_"+secrets.token_hex(8),"status":snapshot["status"],"plan_id":plan_id,"actions":[],"tree":[],"summary":{"conflicts":1},"reason":snapshot.get("reason")}
            self._save_preview(result,plan,snapshot);return result
        current_items=menu.get("items",[]);shops=[item for item in current_items if str(item.get("title","")).casefold()==plan["options"]["parent_label"].casefold()]
        actions=[];conflicts=[]
        if len(shops)>1:
            actions.append({"action":"CONFLICT","item_key":"shopsource:shop","reason":"Multiple matching Shop parents"});conflicts.append("AMBIGUOUS_PARENT")
            shop=None
        else:shop=shops[0] if shops else None
        desired_children=[row for row in plan["items"] if row["depth"]==1]
        for row in desired_children:
            if row["sync_status"]!="PLANNED":actions.append({"action":"SKIP","item_key":row["item_key"],"title":row["title"],"warning":"MISSING_COLLECTION"});continue
            if any(item["resource_id"]==row["resource_id"] for item in desired_children if item["item_key"]!=row["item_key"]):
                actions.append({"action":"CONFLICT","item_key":row["item_key"],"title":row["title"],"warning":"DUPLICATE_TARGET"});conflicts.append(row["item_key"]);continue
            siblings=(shop or {}).get("items",[]);by_resource=[item for item in siblings if item.get("resourceId")==row["resource_id"]]
            by_title=[item for item in siblings if str(item.get("title","")).casefold()==row["title"].casefold()]
            if len(by_resource)>1:
                actions.append({"action":"CONFLICT","item_key":row["item_key"],"warning":"DUPLICATE_TARGET"});conflicts.append(row["item_key"]);continue
            if by_title and not by_resource:
                actions.append({"action":"CONFLICT","item_key":row["item_key"],"title":row["title"],"warning":"PLACEHOLDER_LINK","current_target":by_title[0].get("resourceId") or by_title[0].get("url"),"proposed_target":row["resource_id"]});conflicts.append(row["item_key"]);continue
            if by_resource:
                remote=by_resource[0];old_index=siblings.index(remote)
                if remote.get("title")!=row["title"] or remote.get("url")!=row["url"] or remote.get("type")!="COLLECTION" or bool(remote.get("tags")):action="UPDATE"
                elif old_index!=row["position"]:action="MOVE"
                else:action="NO CHANGE"
                actions.append({"action":action,"item_key":row["item_key"],"title":row["title"],"target":row["url"],"remote_id":remote.get("id"),"position":row["position"]})
            else:actions.append({"action":"CREATE","item_key":row["item_key"],"title":row["title"],"target":row["url"],"position":row["position"]})
        if not shop:actions.append({"action":"CREATE","item_key":"shopsource:shop","title":plan["options"]["parent_label"],"target":plan["options"]["parent_url"],"position":1})
        desired_by_key={row["item_key"]:row for row in desired_children if row["sync_status"]=="PLANNED"}
        proposed_items=[]
        for item in current_items:
            if item is shop:continue
            proposed_items.append(_menu_item(item))
        managed=[];used=set()
        old_children=(shop or {}).get("items",[])
        for row in desired_children:
            if row["sync_status"]!="PLANNED":continue
            remote=next((candidate for candidate in old_children if candidate.get("resourceId")==row["resource_id"] and candidate.get("id") not in used),None)
            if remote:
                used.add(remote.get("id"));managed.append({**_menu_item(remote),"title":row["title"],"type":"COLLECTION","url":row["url"],"resourceId":row["resource_id"],"tags":[]})
            else:managed.append({"title":row["title"],"type":"COLLECTION","url":row["url"],"resourceId":row["resource_id"],"items":[]})
        unmanaged=[_menu_item(item) for item in old_children if item.get("id") not in used]
        parent=_menu_item(shop) if shop else {"title":plan["options"]["parent_label"],"type":"CATALOG","url":plan["options"]["parent_url"],"items":[]}
        parent["items"]=managed+unmanaged
        parent_index=current_items.index(shop) if shop else min(1,len(proposed_items))
        proposed_items.insert(parent_index,parent)
        summary={name:sum(row["action"]==name for row in actions) for name in ("CREATE","UPDATE","MOVE","NO CHANGE","SKIP","CONFLICT")}
        status="CONFLICT" if conflicts else "READY"
        result={"preview_id":"NVPV_"+secrets.token_hex(8),"status":status,"store_id":plan["store_id"],"plan_id":plan_id,
                "menu":menu,"menu_id":menu.get("id"),"write_ready":snapshot.get("write_ready",False),"scopes":snapshot.get("scopes",[]),
                "actions":actions,"tree":proposed_items,"summary":summary,"warnings":plan.get("warnings",[]),
                "assignment_checks":inspect_assignment_menu(menu,desired_children)}
        self._save_preview(result,plan,snapshot);self._report(plan["store_id"],result["preview_id"],preview=result)
        return result

    def _save_preview(self,result,plan,snapshot):
        with connect(self.db) as con:con.execute("INSERT INTO navigation_sync_previews VALUES(?,?,?,?,?,?,?,?,?)",(result["preview_id"],result["plan_id"],plan["store_id"],(snapshot.get("menu") or {}).get("id"),_menu_fingerprint(snapshot.get("menu")),self._input_hash(plan,snapshot),result["status"],json.dumps(result,ensure_ascii=False),_now()))

    def sync(self,preview_id,*,confirmed=False,client=None):
        if confirmed is not True:raise RuntimeError("Explicit confirmation is required before Shopify navigation sync")
        with connect(self.db) as con:row=con.execute("SELECT * FROM navigation_sync_previews WHERE preview_id=?",(preview_id,)).fetchone()
        if not row:raise KeyError(preview_id)
        preview=json.loads(row["preview_json"]);planrow=self.get_plan(row["plan_id"]);plan=planrow["plan"]
        if row["status"]!="READY":return {"status":"CONFLICT" if row["status"]=="CONFLICT" else "MANUAL_ACTION_REQUIRED","preview_id":preview_id}
        with connect(self.db) as con:latest=con.execute("SELECT preview_id FROM navigation_sync_previews WHERE plan_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",(row["plan_id"],)).fetchone()
        if not latest or latest["preview_id"]!=preview_id:return {"status":"CONFLICT","reason":"A newer navigation preview exists; review it before sync."}
        snapshot=self.discover_main_menu(row["store_id"])
        if snapshot["status"] not in {"FOUND","NOT_FOUND"} or not snapshot.get("write_ready"):
            return {"status":"MANUAL_ACTION_REQUIRED","reason":f"Missing {WRITE_SCOPE} or navigation access"}
        if _menu_fingerprint(snapshot.get("menu"))!=row["menu_hash"] or self._input_hash(plan,snapshot)!=row["input_hash"]:
            return {"status":"CONFLICT","reason":"Navigation, collection mapping, or plan changed after preview; review a fresh preview."}
        changes=[item for item in preview["actions"] if item["action"] in {"CREATE","UPDATE","MOVE"}]
        if not changes:return {"status":"NO_CHANGE","menu_id":row["menu_id"],"counts":preview["summary"]}
        if client is None:
            _config,client=self._client(row["store_id"])
        items=[_api_item(item,updates=bool(snapshot.get("menu"))) for item in preview["tree"]]
        if snapshot.get("menu"):
            response=client.execute(MENU_UPDATE,{"id":snapshot["menu"]["id"],"title":snapshot["menu"]["title"],"items":items}).get("menuUpdate") or {}
        else:
            response=client.execute(MENU_CREATE,{"title":plan["root_menu_title"],"handle":plan["root_menu_handle"],"items":items}).get("menuCreate") or {}
        errors=response.get("userErrors",[])
        if errors:
            result={"status":"FAILED","errors":[{"field":e.get("field"),"message":str(e.get("message",""))[:240]} for e in errors]}
            self._record_run(row,result);return result
        verified=self.discover_main_menu(row["store_id"])
        if verified["status"]!="FOUND":result={"status":"VERIFY_FAILED","menu":verified.get("menu")}
        else:
            actual=verified["menu"];expected={"title":actual["title"],"handle":actual["handle"],"items":preview["tree"]}
            result={"status":"VERIFIED" if _menu_semantic(actual)==_menu_semantic(expected) else "VERIFY_FAILED","menu_id":actual["id"],"menu":actual,
                    "counts":preview["summary"]}
            if result["status"]=="VERIFIED":
                with connect(self.db) as con:
                    for child in actual.get("items",[]):
                        if str(child.get("title","")).casefold()==plan["options"]["parent_label"].casefold():
                            for remote in child.get("items",[]):
                                for planned in plan["items"]:
                                    if planned.get("resource_id") and planned["resource_id"]==remote.get("resourceId"):
                                        con.execute("UPDATE store_navigation_items SET shopify_menu_item_id=?,sync_status='VERIFIED',updated_at=? WHERE plan_id=? AND item_key=?",(remote.get("id"),_now(),row["plan_id"],planned["item_key"]))
        self._record_run(row,result);self._report(row["store_id"],"run-"+secrets.token_hex(4),result=result)
        return result

    def _record_run(self,row,result):
        with connect(self.db) as con:con.execute("INSERT INTO navigation_sync_runs VALUES(?,?,?,?,?,?)",("NVR_"+secrets.token_hex(8),row["preview_id"],row["store_id"],result["status"],json.dumps(result,ensure_ascii=False),_now()))

    def verify(self,store_id,plan_id=None):
        snapshot=self.discover_main_menu(store_id)
        if snapshot["status"]!="FOUND":return {"status":snapshot["status"],"verified":False}
        menu=snapshot["menu"]
        with connect(self.db) as con:latest=con.execute("SELECT preview_json FROM navigation_sync_previews WHERE store_id=? AND status='READY' ORDER BY created_at DESC LIMIT 1",(store_id,)).fetchone()
        if not latest:return {"status":"NEEDS_REVIEW","verified":False,"menu":menu}
        preview=json.loads(latest["preview_json"]);expected={"title":menu["title"],"handle":menu["handle"],"items":preview["tree"]};verified=_menu_semantic(menu)==_menu_semantic(expected)
        return {"status":"VERIFIED" if verified else "CONFLICT","verified":verified,"menu_id":menu["id"]}

    def retry_failed(self,plan_id,*,confirmed=False):
        if not confirmed:raise RuntimeError("Explicit confirmation is required to retry navigation sync")
        preview=self.preview(plan_id)
        return self.sync(preview["preview_id"],confirmed=True) if preview["status"]=="READY" else {"status":preview["status"],"preview":preview}

    def _report(self,store_id,run_id,*,plan=None,preview=None,result=None,theme=None):
        root=self.export_dir/"navigation_reports"/_slug(store_id)/_slug(run_id);root.mkdir(parents=True,exist_ok=True)
        if plan:(root/"navigation_plan.json").write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding="utf-8")
        if preview:(root/"sync_preview.json").write_text(json.dumps(preview,ensure_ascii=False,indent=2),encoding="utf-8")
        if result:(root/"sync_result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        if theme:(root/"theme_mega_preview.json").write_text(json.dumps(theme,ensure_ascii=False,indent=2),encoding="utf-8")
        (root/"summary.md").write_text(f"# Navigation {run_id}\n\nStore: {store_id}\nStatus: {(preview or result or theme or plan or {}).get('status','PLAN')}\n",encoding="utf-8")


def _menu_semantic(menu):
    def clean(item):return {key:item.get(key) for key in ("title","type","url","resourceId","tags")}|{"items":[clean(child) for child in item.get("items",[])]}
    if isinstance(menu,list):return [clean(row) for row in menu]
    return {"title":menu.get("title"),"handle":menu.get("handle"),"items":[clean(row) for row in menu.get("items",[])]}


def detect_mega_menu(theme_files):
    headers=[]
    for filename,raw in (theme_files or {}).items():
        if not filename.startswith("sections/") or not filename.endswith(".liquid"):continue
        schema=_section_schema(raw)
        if not schema:continue
        searchable=f"{filename} {schema.get('name','')}".casefold()
        if "header" in searchable:headers.append((filename,schema))
    if not headers:return {"status":"NOT_FOUND","mapping":None,"reason":"Published theme header schema was not found."}
    candidates=[];review_candidates=[]
    try:global_schema=json.loads((theme_files or {}).get("config/settings_schema.json") or "[]")
    except Exception:global_schema=[]
    try:settings_data=json.loads((theme_files or {}).get("config/settings_data.json") or "{}")
    except Exception:settings_data={}
    global_settings=(settings_data.get("current") or {}).get("settings") or {}
    for group in global_schema if isinstance(global_schema,list) else []:
        for field in group.get("settings",[]):
            searchable=f"{field.get('id','')} {field.get('label','')}".casefold()
            if "mega" in searchable and ("menu" in searchable or "parent" in searchable) and field.get("id") in global_settings:
                candidates.append({"filename":"config/settings_schema.json","kind":"SETTING","container":"current.settings","setting_id":field.get("id"),"label":field.get("label"),"confidence":.99})
    for filename,schema in headers:
        for field in schema.get("settings",[]):
            searchable=f"{field.get('id','')} {field.get('label','')}".casefold()
            if "mega" in searchable and ("menu" in searchable or "parent" in searchable):
                section_type=filename.rsplit("/",1)[-1].removesuffix(".liquid")
                sections=(settings_data.get("current") or {}).get("sections") or {}
                matches=[(section_id,section) for section_id,section in sections.items() if section.get("type")==section_type and field.get("id") in (section.get("settings") or {})]
                if len(matches)==1:
                    section_id,_section=matches[0]
                    candidates.append({"filename":filename,"kind":"SETTING","container":f"current.sections.{section_id}.settings","section_id":section_id,"setting_id":field.get("id"),"label":field.get("label"),"confidence":.97})
                else:review_candidates.append({"filename":filename,"kind":"SETTING","setting_id":field.get("id"),"label":field.get("label"),"confidence":.78})
        for block in schema.get("blocks",[]):
            searchable=f"{block.get('type','')} {block.get('name','')}".casefold()
            if "mega" in searchable and "menu" in searchable:
                review_candidates.append({"filename":filename,"kind":"BLOCK","block_type":block.get("type"),"label":block.get("name"),"confidence":.86})
    if candidates:
        candidates.sort(key=lambda row:-row["confidence"])
        return {"status":"MEGA_SUPPORTED_HIGH_CONFIDENCE","mapping":candidates[0],"candidates":candidates}
    if review_candidates:
        return {"status":"MEGA_SUPPORTED_REVIEW_REQUIRED","mapping":review_candidates[0],"candidates":review_candidates,
                "reason":"Mega-menu schema is present but its exact runtime settings location is not safely resolvable."}
    for filename,schema in headers:
        for field in schema.get("settings",[]):
            if field.get("type")=="link_list" or str(field.get("id","")).casefold() in {"menu","main_menu"}:
                return {"status":"DROPDOWN_ONLY","mapping":{"filename":filename,"setting_id":field.get("id"),"type":field.get("type")},"candidates":[]}
    return {"status":"MEGA_SUPPORTED_REVIEW_REQUIRED","mapping":None,"candidates":[],"reason":"Header found but mega-menu controls are ambiguous."}


class MegaMenuThemeService:
    def __init__(self,*,db=None,client_factory=ShopifyGraphQLClient,export_dir=None):
        self.db,self.client_factory,self.export_dir=db,client_factory,Path(export_dir) if export_dir else EXPORT_DIR
        self.reader=ShopifyThemeReader(db=db,client_factory=client_factory);_ensure(db)

    def discover(self,store_id):
        snapshot=self.reader.discover(store_id)
        if snapshot.get("status")!="CONNECTED":return {"status":"MANUAL_ACTION_REQUIRED","theme":snapshot.get("theme"),"support":{"status":"MANUAL_ACTION_REQUIRED"},"scopes":snapshot.get("scopes",[]),"files":snapshot.get("theme_files",{})}
        support=detect_mega_menu(snapshot.get("theme_files",{}));snapshot["support"]=support;snapshot["write_themes"]="write_themes" in snapshot.get("scopes",[])
        return snapshot

    def preview(self,store_id,parent_label="Shop",menu_handle="main-menu"):
        snapshot=self.discover(store_id);support=snapshot.get("support",{});files=snapshot.get("theme_files",{});settings_text=files.get("config/settings_data.json") or "{}"
        try:current=json.loads(settings_text)
        except Exception:current={}
        proposed=json.loads(json.dumps(current));mapping=support.get("mapping") or {};actions=[]
        if support.get("status")=="MEGA_SUPPORTED_HIGH_CONFIDENCE" and mapping.get("kind")=="SETTING" and mapping.get("setting_id"):
            path=str(mapping.get("container","current.settings")).split(".");settings=proposed
            for segment in path:settings=settings.setdefault(segment,{})
            value=parent_label
            actions.append({"action":"SET_MEGA_MENU","file":"config/settings_data.json","setting_id":mapping["setting_id"],"current":settings.get(mapping["setting_id"]),"proposed":value,"confidence":mapping["confidence"]})
            settings[mapping["setting_id"]]=value
            status="PREVIEW" if snapshot.get("write_themes") else "MANUAL_ACTION_REQUIRED"
        elif support.get("status")=="DROPDOWN_ONLY":actions.append({"action":"MANUAL_ACTION_REQUIRED","reason":"Theme supports dropdown navigation but no detected mega-menu control."});status="DROPDOWN_ONLY"
        else:actions.append({"action":"MANUAL_ACTION_REQUIRED","reason":support.get("reason","Manual theme configuration required.")});status="MANUAL_ACTION_REQUIRED"
        if not snapshot.get("write_themes") and status=="PREVIEW":actions.append({"action":"MANUAL_ACTION_REQUIRED","reason":"Missing write_themes or Shopify theme-file exemption."});status="MANUAL_ACTION_REQUIRED"
        theme=snapshot.get("theme") or {};preview={"preview_id":"MMP_"+secrets.token_hex(9),"store_id":store_id,"theme":theme,"support":support,"status":status,
                 "actions":actions,"current":current,"proposed":proposed,"settings_schema":files.get("config/settings_schema.json"),"settings_data":settings_text,
                 "write_themes":bool(snapshot.get("write_themes")),"settings_hash":_hash(current),"menu_handle":menu_handle,"parent_label":parent_label,
                 "manual_steps":["Online Store","Theme Edit / Customize","Header","메가메뉴 설정/블록 선택","Shop에 연결","Save"]}
        with connect(self.db) as con:con.execute("INSERT INTO mega_menu_previews VALUES(?,?,?,?,?,?,?)",(preview["preview_id"],store_id,theme.get("id"),preview["settings_hash"],status,json.dumps(preview,ensure_ascii=False),_now()))
        NavigationService(db=self.db,client_factory=self.client_factory,export_dir=self.export_dir)._report(store_id,preview["preview_id"],theme=preview)
        return preview

    def apply(self,preview_id,*,confirmed=False,client=None):
        if confirmed is not True:raise RuntimeError("Explicit confirmation required before mega-menu theme apply")
        with connect(self.db) as con:row=con.execute("SELECT * FROM mega_menu_previews WHERE preview_id=?",(preview_id,)).fetchone()
        if not row:raise KeyError(preview_id)
        preview=json.loads(row["preview_json"])
        if row["status"]!="PREVIEW" or not preview.get("write_themes"):return {"status":"MANUAL_ACTION_REQUIRED","steps":preview["manual_steps"]}
        snapshot=self.discover(row["store_id"]);files=snapshot.get("theme_files",{})
        try:current=json.loads(files.get("config/settings_data.json") or "{}")
        except Exception:current={}
        if (snapshot.get("theme") or {}).get("id")!=row["theme_id"] or _hash(current)!=row["settings_hash"]:return {"status":"CONFLICT","reason":"Theme settings changed since preview."}
        config=get_connection(row["store_id"],db=self.db);token,_=get_shopify_token(row["store_id"],db=self.db);client=client or self.client_factory(config["shop_domain"],token,config["api_version"])
        folder=self.export_dir/"theme_backups"/_slug(row["store_id"])/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+secrets.token_hex(3));folder.mkdir(parents=True,exist_ok=False)
        before={"config/settings_data.json":files.get("config/settings_data.json") or "{}","theme_id":row["theme_id"]}
        (folder/"before.json").write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding="utf-8")
        (folder/"proposed.json").write_text(json.dumps(preview["proposed"],ensure_ascii=False,indent=2),encoding="utf-8")
        (folder/"diff.md").write_text(f"before={row['settings_hash']}\nproposed={_hash(preview['proposed'])}\n",encoding="utf-8")
        backup_id="MMB_"+secrets.token_hex(8)
        with connect(self.db) as con:con.execute("INSERT INTO mega_menu_backups VALUES(?,?,?,?,?,?)",(backup_id,row["store_id"],row["theme_id"],before["config/settings_data.json"],str(folder),_now()))
        result=client.execute(THEME_UPSERT,{"themeId":row["theme_id"],"files":[{"filename":"config/settings_data.json","body":{"type":"TEXT","value":json.dumps(preview["proposed"],ensure_ascii=False)}}]}).get("themeFilesUpsert") or {}
        if result.get("userErrors"):return {"status":"FAILED","errors":[{"field":e.get("field"),"message":str(e.get("message",""))[:240]} for e in result["userErrors"]],"backup_id":backup_id}
        after=self.discover(row["store_id"]);actual=after.get("theme_files",{}).get("config/settings_data.json") or "{}"
        try:actual_json=json.loads(actual)
        except Exception:actual_json={}
        return {"status":"VERIFIED" if actual_json==preview["proposed"] else "VERIFY_FAILED","backup_id":backup_id,"theme_id":row["theme_id"]}

    def verify_manual_apply(self,preview_id):
        with connect(self.db) as con:row=con.execute("SELECT * FROM mega_menu_previews WHERE preview_id=?",(preview_id,)).fetchone()
        if not row:return {"status":"NOT_FOUND"}
        preview=json.loads(row["preview_json"]);snapshot=self.discover(row["store_id"])
        try:observed=json.loads(snapshot.get("theme_files",{}).get("config/settings_data.json") or "{}")
        except Exception:observed={}
        return {"status":"VERIFIED" if observed==preview.get("proposed") else "MANUAL_ACTION_REQUIRED","observed_hash":_hash(observed),"expected_hash":_hash(preview.get("proposed"))}

    def manual_instructions(self):return ["Online Store","Theme Edit / Customize","Header","메가메뉴 설정/블록 선택","Shop에 연결","Save"]
