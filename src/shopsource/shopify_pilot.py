"""Guarded, small-batch first-live Shopify product pilot."""
from __future__ import annotations

import json
import secrets

from .db import connect
from .shopify_collections import SHOPIFY_API_VERSION, get_connection, get_shopify_token
from .shopify_products import DirectShopifyProductPublisher, ELIGIBLE_STATUSES, _now


class ShopifyLivePilot:
    DEFAULT_LIMIT = 10
    MAX_LIMIT = 20

    def __init__(self, *, db=None, publisher=None, client_factory=None):
        self.db = db
        if publisher is None:
            kwargs = {"db": db}
            if client_factory:
                kwargs["client_factory"] = client_factory
            publisher = DirectShopifyProductPublisher(**kwargs)
        self.publisher = publisher

    def connection_preflight(self, store_id):
        config = get_connection(store_id, db=self.db)
        token, source = get_shopify_token(store_id)
        result = {"shop_domain": (config or {}).get("shop_domain"), "credential_present": bool(token),
                  "credential_source": source if token else "missing", "api_version": (config or {}).get("api_version"),
                  "api_version_ready": bool(config and config.get("api_version") == SHOPIFY_API_VERSION),
                  "read_products": False, "write_products": False, "read_publications": False,
                  "write_publications": False, "write_files": False, "errors": []}
        if not config:
            result["errors"].append("Shopify 스토어 연결 정보가 없습니다.")
        if not token:
            result["errors"].append("Shopify credential이 없습니다. 토큰 값은 표시하지 않습니다.")
        if config and token:
            try:
                client = self.publisher.client_factory(config["shop_domain"], token, config["api_version"])
                data = client.execute("query PilotScopes { currentAppInstallation { accessScopes { handle } } }")
                scopes = {item.get("handle") for item in (data.get("currentAppInstallation") or {}).get("accessScopes", [])}
                for name in ("read_products", "write_products", "read_publications", "write_publications", "write_files"):
                    result[name] = name in scopes
            except Exception:
                result["errors"].append("Shopify 권한 확인에 실패했습니다. 연결 상태를 확인하세요.")
        result["product_ready"] = bool(result["shop_domain"] and result["credential_present"] and result["api_version_ready"] and result["read_products"] and result["write_products"])
        result["collection_ready"] = bool(result["read_publications"] and result["write_publications"] and result["write_files"])
        result["missing_product_scopes"] = [name for name in ("read_products", "write_products") if not result[name]]
        result["missing_collection_scopes"] = [name for name in ("read_publications", "write_publications", "write_files") if not result[name]]
        return result

    @staticmethod
    def _complete(row):
        score = 0
        if str(row.get("title") or "").strip(): score += 16
        if row.get("selling_price") is not None:
            try:
                if float(row["selling_price"]) > 0: score += 32
            except (TypeError, ValueError): pass
        if str(row.get("asin") or "").strip(): score += 16
        if str(row.get("brand") or "").strip(): score += 8
        if str(row.get("source") or row.get("source_kind") or "").strip(): score += 4
        if int(row.get("source_variant_count") or 0) <= 1: score += 4
        return score

    def _select(self, store_id, limit):
        rows = []
        for row in self.publisher._catalog_rows(store_id):
            if str(row.get("final_status") or "").upper() not in ELIGIBLE_STATUSES or row.get("archived"):
                continue
            rows.append(row)
        # Stable tie-break by MASTER ID; completeness and a valid store price win.
        return sorted(rows, key=lambda row: (-self._complete(row), int(row["master_product_id"])))[:limit]

    def preview(self, store_id, *, limit=DEFAULT_LIMIT, media_mode="MANUAL_MEDIA", source_media_rights_confirmed=False):
        limit = int(limit)
        if not 1 <= limit <= self.MAX_LIMIT:
            raise ValueError("Pilot 상품 수는 1~20개로 제한됩니다.")
        preflight = self.connection_preflight(store_id)
        if not preflight["product_ready"]:
            raise RuntimeError("Shopify 상품 연결/권한이 준비되지 않았습니다: " + ", ".join(preflight["missing_product_scopes"]))
        if media_mode not in {"MANUAL_MEDIA", "SOURCE_MEDIA", "GENERATED_MEDIA", "MIXED"}:
            raise ValueError("지원하지 않는 이미지 업로드 방식입니다.")
        self.publisher.set_media_mode(store_id, media_mode, source_media_rights_confirmed=source_media_rights_confirmed)
        selected = self._select(store_id, limit)
        # Prepare the deterministic collection taxonomy before product tags are calculated.
        from .collection_planner import CollectionPlanner
        CollectionPlanner(self.db).create_plan(store_id, settings={"rule_strategy": "TAG_PREFERRED"})
        ids = [row["master_product_id"] for row in selected]
        result = self.publisher.preview(store_id, publish_status="DRAFT", limit=limit, master_product_ids=ids)
        pilot_id = "PILOT_" + secrets.token_hex(8)
        selected_by_id = {row["master_product_id"]: row for row in selected}
        with connect(self.db) as con:
            for item in con.execute("SELECT * FROM shopify_product_sync_items WHERE run_id=?", (result["run_id"],)).fetchall():
                payload = json.loads(item["payload_json"] or "{}")
                payload["pilot_run_id"] = pilot_id
                con.execute("UPDATE shopify_product_sync_items SET payload_json=? WHERE run_id=? AND master_product_id=?",
                            (json.dumps(payload, ensure_ascii=False), result["run_id"], item["master_product_id"]))
            run = con.execute("SELECT counts_json FROM shopify_product_sync_runs WHERE run_id=?", (result["run_id"],)).fetchone()
            summary = json.loads(run["counts_json"] or "{}")
            summary.update({"pilot_run_id": pilot_id, "publish_status": "DRAFT", "pilot_max": self.MAX_LIMIT})
            con.execute("UPDATE shopify_product_sync_runs SET counts_json=?,updated_at=? WHERE run_id=?",
                        (json.dumps(summary), _now(), result["run_id"]))
            con.execute("""CREATE TABLE IF NOT EXISTS shopify_live_pilots (
                pilot_run_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, sync_run_id TEXT NOT NULL,
                selected_count INTEGER NOT NULL, created_at TEXT NOT NULL)""")
            con.execute("INSERT INTO shopify_live_pilots(pilot_run_id,store_id,sync_run_id,selected_count,created_at) VALUES(?,?,?,?,?)",
                        (pilot_id, store_id, result["run_id"], sum(result["counts"].get(key, 0) for key in ("CREATE", "UPDATE", "NO CHANGE")), _now()))
            queue = {row["master_product_id"]: dict(row) for row in con.execute("SELECT * FROM shopify_product_sync_items WHERE run_id=?", (result["run_id"],))}
        items = []
        for row in selected:
            item = queue[row["master_product_id"]]
            payload = json.loads(item["payload_json"] or "{}")
            action = item["action"] if item["action"] in {"CREATE", "UPDATE", "NO CHANGE", "CONFLICT", "SKIP"} else "SKIP"
            items.append({"source_id": row.get("asin"), "master_product_id": row["master_product_id"],
                          "title": row.get("title") or "", "source_price": row.get("source_price"),
                          "selling_price": row.get("selling_price"), "currency": row.get("selling_currency") or "USD",
                          "status": "DRAFT", "action": action, "reason": item.get("error") or "",
                          "collection_tags": payload.get("owned_tags", []), "media_mode": media_mode,
                          "warning": ("상품 이미지는 업로드하지 않습니다." if media_mode == "MANUAL_MEDIA" else
                                      "원본 이미지 사용 권리가 확인되지 않았습니다." if media_mode in {"SOURCE_MEDIA", "MIXED"} and not source_media_rights_confirmed else
                                      "이미지 provider 설정이 필요합니다." if media_mode == "GENERATED_MEDIA" else "이미지 설정을 확인하세요.")})
        return {**result, "pilot_run_id": pilot_id, "limit": limit, "items": items, "preflight": preflight,
                "summary": {"selected": len(items), **result["counts"]}}

    def execute(self, sync_run_id, *, live_confirmed=False):
        if live_confirmed is not True:
            raise RuntimeError("파일럿 실제 실행 확인이 필요합니다.")
        with connect(self.db) as con:
            run = con.execute("SELECT * FROM shopify_product_sync_runs WHERE run_id=?", (sync_run_id,)).fetchone()
            if not run: raise KeyError(sync_run_id)
            options = json.loads(run["counts_json"] or "{}")
            count = con.execute("SELECT COUNT(*) FROM shopify_product_sync_items WHERE run_id=?", (sync_run_id,)).fetchone()[0]
        if run["status"] != "PREVIEW" or options.get("publish_status") != "DRAFT" or not options.get("pilot_run_id") or count > self.MAX_LIMIT:
            raise RuntimeError("유효한 DRAFT 파일럿 미리보기가 아니거나 최대 개수를 초과했습니다.")
        preflight = self.connection_preflight(run["store_id"])
        if not preflight["product_ready"]:
            raise RuntimeError("Shopify 상품 권한 확인이 필요합니다.")
        result = self.publisher.sync(sync_run_id, confirmed=True, expected_input_hash=run["input_hash"])
        with connect(self.db) as con:
            for item in con.execute("SELECT master_product_id,payload_json FROM shopify_product_sync_items WHERE run_id=? AND status='NO CHANGE'", (sync_run_id,)):
                pilot_id = json.loads(item["payload_json"] or "{}").get("pilot_run_id")
                if pilot_id:
                    con.execute("UPDATE shopify_product_mappings SET pilot_run_id=? WHERE store_id=? AND master_product_id=?",
                                (pilot_id, run["store_id"], item["master_product_id"]))
        return result

    def collection_preview(self, store_id, pilot_run_id):
        with connect(self.db) as con:
            pilot = con.execute("SELECT * FROM shopify_live_pilots WHERE store_id=? AND pilot_run_id=?", (store_id, pilot_run_id)).fetchone()
            if not pilot:
                raise RuntimeError("파일럿 ID를 찾을 수 없습니다.")
            rows = [dict(row) for row in con.execute("SELECT * FROM shopify_product_mappings WHERE store_id=? AND pilot_run_id=?", (store_id, pilot_run_id))]
        if len(rows) != pilot["selected_count"] or not rows or any(row["sync_status"] != "SYNCED" for row in rows):
            raise RuntimeError("파일럿 상품이 모두 검증되기 전에는 컬렉션 미리보기를 만들 수 없습니다.")
        from .collection_planner import CollectionPlanner
        plan = CollectionPlanner(self.db).create_plan(store_id, settings={"rule_strategy": "TAG_PREFERRED"})
        from .shopify_collections import ShopifyCollectionPublisher
        preview = ShopifyCollectionPublisher(db=self.db).dry_run(plan, publish_online_store=False)
        return {"pilot_run_id": pilot_run_id, "plan": plan, "collection_preview": preview,
                "writes_performed": False, "pilot_products": len(rows)}
