"""Read-only Shopify homepage collection planning and safe patch export.

The production adapter deliberately has no theme mutation operation. Applying or
rolling back through the UI only creates a local backup/manual patch package.
Mutation protocols are injected by tests so orchestration can be verified with
an isolated mock without enabling live theme writes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .db import connect, init_db
from .paths import EXPORT_DIR
from .shopify_collections import ShopifyGraphQLClient, get_connection, get_shopify_token

THEME_READ_SCOPE = "read_themes"
THEMES_QUERY = "query ShopSourceThemes { themes(first: 50) { nodes { id name role } } }"
THEME_FILES_QUERY = "query ShopSourceThemeFiles($id: ID!) { theme(id: $id) { id name role files(first: 250, filenames: [\"templates/index.json\", \"sections/*\", \"config/settings_schema.json\", \"config/settings_data.json\"]) { nodes { filename body { __typename ... on OnlineStoreThemeFileBodyText { content } ... on OnlineStoreThemeFileBodyBase64 { contentBase64 } } } } } }"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _install_schema(db=None) -> None:
    init_db(db)
    with connect(db) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS homepage_theme_state (
          store_id TEXT PRIMARY KEY, theme_id TEXT NOT NULL, template_filename TEXT NOT NULL,
          managed_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS homepage_theme_backups (
          backup_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, theme_id TEXT NOT NULL,
          folder TEXT NOT NULL, before_hash TEXT NOT NULL, proposed_hash TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)


def _section_schema(raw: str) -> dict | None:
    match = re.search(r"\{%[- ]*schema[- ]*%\}(.*?)\{%[- ]*endschema[- ]*%\}", raw, flags=re.S | re.I)
    if not match:
        return None
    try:
        schema = json.loads(match.group(1).strip())
    except (json.JSONDecodeError, TypeError):
        return None
    return schema if isinstance(schema, dict) else None


def detect_featured_collection_schemas(section_files: dict[str, str]) -> list[dict]:
    """Return section types whose real schema can select one collection."""
    found = []
    for filename, raw in sorted(section_files.items()):
        schema = _section_schema(raw)
        if not schema:
            continue
        settings = schema.get("settings") or []
        collection_fields = [field for field in settings if field.get("type") == "collection"]
        collection_list_fields = [field for field in settings if field.get("type") in {"collection_list", "collection_list_picker"}]
        searchable = " ".join([str(schema.get("name", "")), filename, *(str(f.get("label", "")) for f in settings)]).casefold()
        if not collection_fields and not collection_list_fields:
            continue
        # A collection-list-only section is valid as a fallback; prefer single-collection sections.
        featured = any(word in searchable for word in ("featured collection", "collection", "featured"))
        if not featured:
            continue
        ratio_fields = []
        count_fields = []
        for field in settings:
            field_id = str(field.get("id", "")).casefold()
            label = str(field.get("label", "")).casefold()
            if "ratio" in field_id or "image ratio" in label or "image aspect" in label:
                ratio_fields.append(field)
            if "product" in field_id and ("show" in field_id or "count" in field_id or "product" == field_id):
                count_fields.append(field)
        found.append({
            "filename": filename,
            "type": filename.rsplit("/", 1)[-1].removesuffix(".liquid").removesuffix(".json"),
            "name": schema.get("name", filename),
            "schema": schema,
            "collection_field": (collection_fields or collection_list_fields)[0],
            "collection_mode": "SINGLE" if collection_fields else "LIST",
            "ratio_fields": ratio_fields,
            "count_fields": count_fields,
        })
    return sorted(found, key=lambda row: (row["collection_mode"] != "SINGLE", row["filename"]))


def _body_content(body: dict) -> str | None:
    if body.get("content") is not None:
        return str(body["content"])
    encoded = body.get("contentBase64")
    if encoded:
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return None


class ShopifyThemeReader:
    """Shopify 2026-07 theme discovery and read-only file retrieval."""
    def __init__(self, *, db=None, client_factory=ShopifyGraphQLClient):
        self.db, self.client_factory = db, client_factory

    def _client(self, store_id: str):
        config = get_connection(store_id, db=self.db)
        if not config:
            raise RuntimeError("Shopify shop domain is not configured")
        token, _ = get_shopify_token(store_id)
        if not token:
            raise RuntimeError("Shopify credential missing; token is never stored in ShopSource DB")
        return config, self.client_factory(config["shop_domain"], token, config["api_version"])

    def discover(self, store_id: str) -> dict:
        config, client = self._client(store_id)
        data = client.execute("query ShopSourceScopes { currentAppInstallation { accessScopes { handle } } }")
        scopes = sorted({r.get("handle", "") for r in (data.get("currentAppInstallation") or {}).get("accessScopes", [])})
        if THEME_READ_SCOPE not in scopes:
            return {"status": "MISSING_READ_SCOPE", "store_id": store_id, "shop_domain": config["shop_domain"],
                    "api_version": config["api_version"], "scopes": scopes, "theme": None, "template": None,
                    "schemas": [], "manual_patch_mode": True, "warning": "Grant read_themes to inspect the published theme."}
        themes = client.execute(THEMES_QUERY).get("themes", {}).get("nodes", [])
        theme = next((row for row in themes if str(row.get("role", "")).upper() == "MAIN"), None)
        if not theme:
            return {"status": "NO_PUBLISHED_THEME", "store_id": store_id, "scopes": scopes, "theme": None,
                    "template": None, "schemas": [], "manual_patch_mode": True}
        payload = client.execute(THEME_FILES_QUERY, {"id": theme["id"]}).get("theme") or {}
        files = {row["filename"]: _body_content(row.get("body") or {}) for row in payload.get("files", {}).get("nodes", [])}
        files = {name: value for name, value in files.items() if value is not None}
        template_name = "templates/index.json" if "templates/index.json" in files else next(
            (name for name in files if name.startswith("templates/index.") and name.endswith(".json")), None)
        template = None
        if template_name:
            try:
                template = json.loads(files[template_name])
            except json.JSONDecodeError:
                template = None
        sections = {name: raw for name, raw in files.items() if name.startswith("sections/") and name.endswith(".liquid")}
        schemas = detect_featured_collection_schemas(sections)
        return {"status": "CONNECTED", "store_id": store_id, "shop_domain": config["shop_domain"],
                "api_version": config["api_version"], "scopes": scopes,
                "theme": {"id": theme["id"], "name": theme["name"], "role": theme["role"]},
                "template_filename": template_name, "template": template, "schemas": schemas,
                "theme_files": files,
                "manual_patch_mode": True, "warning": None if schemas else "No compatible featured-collection section schema was found."}


def recommend_collections(plan: dict, *, image_ready: set[str] | None = None, maximum: int = 4) -> list[dict]:
    image_ready = image_ready or set()
    rows = [dict(row) for row in plan.get("collections", []) if row.get("enabled", 1)]
    def rank(row):
        warnings = row.get("warnings", [])
        warning_penalty = sum(2 for warning in warnings if any(word in str(warning).upper() for word in ("BROAD", "OVERLAP", "ZERO", "AMBIGUOUS")))
        return (int(row.get("priority", 999)), -int(row.get("estimated_product_count", 0)), warning_penalty,
                0 if row.get("collection_key") in image_ready else 1, str(row.get("collection_key", "")))
    rows.sort(key=rank)
    count = min(5, max(3, maximum), len(rows))
    return rows[:count]


def _section_id(key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")[:12] or "collection"
    digest = hashlib.sha1(str(key).encode()).hexdigest()[:8]
    return f"ss_{slug}_{digest}"


def _schema_setting(schema: dict, identifiers: list[str]) -> dict | None:
    settings = schema.get("schema", schema).get("settings", []) if "schema" in schema else schema.get("settings", [])
    for wanted in identifiers:
        for field in settings:
            if str(field.get("id", "")).casefold() == wanted.casefold():
                return field
    return None


def build_homepage_plan(snapshot: dict, plan: dict, *, collection_handles: dict[str, str], image_ready: set[str] | None = None,
                        maximum: int = 4, db=None) -> dict:
    """Build a minimal-diff proposal from a read-only theme snapshot."""
    _install_schema(db)
    current = snapshot.get("template")
    if not isinstance(current, dict) or not snapshot.get("template_filename"):
        return {"status": "MANUAL_PATCH_MODE", "operations": [{"action": "CONFLICT", "reason": "Homepage JSON template unavailable"}],
                "current": current, "proposed": current, "warnings": ["Export/manual edit required"], "manual_patch_mode": True}
    schemas = snapshot.get("schemas", [])
    if not schemas:
        return {"status": "MANUAL_PATCH_MODE", "operations": [{"action": "CONFLICT", "reason": "Theme has no compatible collection section schema"}],
                "current": current, "proposed": current, "warnings": ["No browser automation or live theme write will be attempted"], "manual_patch_mode": True}
    selected = recommend_collections(plan, image_ready=image_ready, maximum=maximum)
    schema_info = schemas[0]
    section_schema = schema_info.get("schema", {})
    settings_schema = section_schema.get("settings", [])
    collection_field = schema_info["collection_field"]
    current_copy = json.loads(json.dumps(current))
    proposed = json.loads(json.dumps(current))
    sections = proposed.setdefault("sections", {})
    order = proposed.setdefault("order", [])
    operations, warnings = [], []
    # Existing unmanaged sections and all unrelated JSON remain byte-for-byte semantic copies.
    managed_ids = {_section_id(row["collection_key"]): row for row in selected}
    if db and snapshot.get("theme"):
        with connect(db) as con:
            state = con.execute("SELECT managed_json FROM homepage_theme_state WHERE store_id=?", (plan["store_id"],)).fetchone()
        previous = json.loads(state["managed_json"]) if state else {}
    else:
        previous = {}
    prior_order = previous.get("order", [])
    existing_managed_order = [section_id for section_id in order if section_id.startswith("ss_")]
    drifted = bool(prior_order and existing_managed_order != prior_order)
    if drifted:
        operations.append({"action": "CONFLICT", "reason": "Shopify Theme Editor order differs from the last ShopSource-managed order"})
    managed_collection_field = collection_field.get("id")
    for row in selected:
        key = row["collection_key"]
        section_id = _section_id(key)
        handle = collection_handles.get(key) or row.get("handle")
        if not handle:
            operations.append({"action": "CONFLICT", "section_id": section_id, "collection_key": key,
                              "reason": "No Shopify collection handle is mapped; sync the collection first"})
            continue
        desired_settings = {managed_collection_field: handle if schema_info.get("collection_mode") == "SINGLE" else [handle]}
        ratio_field = next((field for field in schema_info.get("ratio_fields", [])
                            if any(option.get("value", "").casefold() == "square" for option in field.get("options", []))), None)
        ratio_status = "THEME_DEFAULT"
        if ratio_field:
            desired_settings[ratio_field["id"]] = "square"
            ratio_status = "SQUARE"
        else:
            warnings.append(f"{row['title']}: Square ratio is unsupported; using theme default")
        # Existing settings outside the managed collection/ratio settings stay untouched.
        old = sections.get(section_id)
        if old:
            previous_section = previous.get("sections", {}).get(section_id)
            if previous_section is not None and _hash(old) != _hash(previous_section):
                operations.append({"action": "CONFLICT", "section_id": section_id, "collection_key": key,
                                  "reason": "Section settings/type were manually edited in Theme Editor"})
                continue
            updated = json.loads(json.dumps(old))
            settings = updated.setdefault("settings", {})
            changed = False
            for setting_id, value in desired_settings.items():
                if settings.get(setting_id) != value:
                    settings[setting_id] = value
                    changed = True
            if changed:
                sections[section_id] = updated
                operations.append({"action": "UPDATE SECTION", "section_id": section_id, "collection_key": key,
                                   "collection": row["title"], "handle": handle, "image_ratio": ratio_status})
            elif drifted:
                operations.append({"action": "CONFLICT", "section_id": section_id, "collection_key": key,
                                   "reason": "Manual section move detected; no automatic overwrite"})
            else:
                operations.append({"action": "NO CHANGE", "section_id": section_id, "collection_key": key,
                                   "collection": row["title"], "handle": handle, "image_ratio": ratio_status})
        else:
            # Only settings that exist in this theme's schema are introduced.
            allowed = {field.get("id") for field in settings_schema}
            desired_settings = {name: value for name, value in desired_settings.items() if name in allowed}
            sections[section_id] = {"type": schema_info["type"], "settings": desired_settings}
            order.append(section_id)
            operations.append({"action": "CREATE SECTION", "section_id": section_id, "collection_key": key,
                               "collection": row["title"], "handle": handle, "image_ratio": ratio_status})
    # Removed recommendations are retained, never deleted; order and unrelated sections are preserved.
    if not any(op["action"] == "CONFLICT" for op in operations):
        selected_order = [_section_id(row["collection_key"]) for row in selected if _section_id(row["collection_key"]) in order]
        untouched = [section_id for section_id in order if section_id not in selected_order]
        desired_order = untouched + selected_order
        if desired_order != order:
            proposed["order"] = desired_order
            operations.append({"action": "MOVE", "section_ids": selected_order, "reason": "Place recommended collections in ranked order"})
        elif not operations:
            operations.append({"action": "NO CHANGE"})
    product_count_default = next((field.get("default") for field in schema_info.get("count_fields", []) if "default" in field), None)
    return {"status": "CONFLICT" if any(op["action"] == "CONFLICT" for op in operations) else "DRY_RUN",
            "store_id": plan["store_id"], "theme": snapshot["theme"], "template_filename": snapshot["template_filename"],
            "section_type": schema_info["type"], "current": current_copy, "proposed": proposed,
            "diff": {"operations": operations, "before_hash": _hash(current_copy), "proposed_hash": _hash(proposed)},
            "operations": operations, "warnings": warnings, "image_ratio": "Square" if all(x.get("image_ratio") != "THEME_DEFAULT" for x in operations if x.get("collection_key")) else "Theme default",
            "products_per_section": product_count_default if product_count_default is not None else "Theme default",
            "manual_patch_mode": True}


class HomepageCollectionService:
    """Local backup and manual patch exporter. No production theme write path."""
    def __init__(self, *, db=None, export_dir: str | Path | None = None, clock: Callable[[], datetime] | None = None):
        self.db, self.export_dir = db, Path(export_dir) if export_dir else EXPORT_DIR
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        _install_schema(db)

    def save_safe_patch(self, result: dict, *, store_id: str | None = None) -> dict:
        if result.get("status") == "CONFLICT":
            raise RuntimeError("Resolve theme drift conflicts before exporting a patch")
        store_id = store_id or result["store_id"]
        stamp = self.clock().strftime("%Y%m%dT%H%M%S%fZ")
        folder = self.export_dir / "theme_backups" / store_id / stamp
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "before.json").write_text(_json(result.get("current")), encoding="utf-8")
        (folder / "proposed.json").write_text(_json(result.get("proposed")), encoding="utf-8")
        ops = result.get("operations", [])
        diff_lines = ["# Shopify homepage collection patch", "", "Mode: MANUAL PATCH / SAFE DRY RUN", "",
                      f"Theme: {result.get('theme', {}).get('name', 'Unknown')}",
                      f"Template: {result.get('template_filename', 'Unknown')}", "",
                      *[f"- {op.get('action')}: {op.get('collection') or op.get('section_id') or op.get('reason', '')}" for op in ops], "",
                      "No Shopify theme write was performed. Apply this configuration manually in Shopify Theme Editor after review.", ""]
        (folder / "diff.md").write_text("\n".join(diff_lines), encoding="utf-8")
        backup_id = "THB_" + hashlib.sha1(f"{store_id}:{stamp}".encode()).hexdigest()[:16]
        with connect(self.db) as con:
            con.execute("INSERT INTO homepage_theme_backups(backup_id,store_id,theme_id,folder,before_hash,proposed_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                        (backup_id, store_id, result.get("theme", {}).get("id", ""), str(folder),
                         result.get("diff", {}).get("before_hash", ""), result.get("diff", {}).get("proposed_hash", ""), _now()))
        return {"backup_id": backup_id, "folder": str(folder), "before": str(folder / "before.json"),
                "proposed": str(folder / "proposed.json"), "diff": str(folder / "diff.md"),
                "mode": "MANUAL_PATCH_MODE", "theme_write_performed": False}

    def record_applied_state(self, result: dict) -> None:
        """Record a verified state after an external/mock apply; never performs that apply."""
        if result.get("status") == "CONFLICT":
            raise RuntimeError("Cannot record a conflicted homepage proposal")
        managed = {"order": [key for key in result.get("proposed", {}).get("order", []) if key.startswith("ss_")],
                   "sections": {key: value for key, value in result.get("proposed", {}).get("sections", {}).items() if key.startswith("ss_")}}
        with connect(self.db) as con:
            con.execute("""INSERT INTO homepage_theme_state(store_id,theme_id,template_filename,managed_json,updated_at)
              VALUES(?,?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET theme_id=excluded.theme_id,
              template_filename=excluded.template_filename,managed_json=excluded.managed_json,updated_at=excluded.updated_at""",
                        (result["store_id"], result.get("theme", {}).get("id", ""), result.get("template_filename", ""), _json(managed), _now()))

    def rollback_plan(self, backup_id: str) -> dict:
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM homepage_theme_backups WHERE backup_id=?", (backup_id,)).fetchone()
        if not row:
            raise KeyError(backup_id)
        folder = Path(row["folder"])
        before = json.loads((folder / "before.json").read_text(encoding="utf-8"))
        proposed = json.loads((folder / "proposed.json").read_text(encoding="utf-8"))
        return {"backup_id": backup_id, "store_id": row["store_id"], "theme_id": row["theme_id"],
                "before": before, "current_proposal": proposed, "action": "MANUAL_ROLLBACK_PLAN",
                "theme_write_performed": False}

    def latest_backup(self, store_id: str) -> dict | None:
        with connect(self.db) as con:
            row = con.execute("SELECT backup_id FROM homepage_theme_backups WHERE store_id=? ORDER BY created_at DESC LIMIT 1", (store_id,)).fetchone()
        return self.rollback_plan(row["backup_id"]) if row else None
