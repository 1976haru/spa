"""Read-only multi-store theme transport operations with bounded fan-out."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
import threading
import time
from typing import Callable

from .db import connect, init_db
from .featured_theme_backend import FeaturedThemeBackendCoordinator
from .shopify_collections import get_connection, get_shopify_token
from .shopify_auth import ShopifyAuthService
from .theme_access_credentials import credential_present


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _registered_stores(db=None) -> list[dict]:
    # Reuse the store registry that already feeds the operator's store selector.
    from .ui.v2_service import list_stores
    return list_stores(db)


class MultiStoreThemeOperationsService:
    """Present registered stores and run isolated, read-only capability checks."""

    MAX_WORKERS = 4
    CACHE_MAX_AGE = timedelta(minutes=15)

    def __init__(self, *, db=None, store_provider: Callable | None = None,
                 connection_provider: Callable | None = None,
                 token_provider: Callable | None = None,
                 auth_status_provider: Callable | None = None,
                 theme_credential_provider: Callable | None = None,
                 coordinator_factory: Callable | None = None,
                 max_workers: int = MAX_WORKERS, per_store_timeout: float = 95.0):
        self.db = db
        self.store_provider = store_provider or (lambda: _registered_stores(self.db))
        self.connection_provider = connection_provider or (lambda store_id: get_connection(store_id, db=self.db))
        self.token_provider = token_provider or (lambda store_id: get_shopify_token(store_id, db=self.db)[0])
        self.auth_status_provider = auth_status_provider or (lambda store_id: ShopifyAuthService(db=self.db).status(store_id))
        self.theme_credential_provider = theme_credential_provider or credential_present
        self.coordinator_factory = coordinator_factory or (lambda: FeaturedThemeBackendCoordinator(db=self.db))
        self._coordinator_instance = None
        self._coordinator_lock = threading.Lock()
        self.max_workers = max(1, min(self.MAX_WORKERS, int(max_workers)))
        self.per_store_timeout = max(1.0, float(per_store_timeout))
        self._install_schema()

    def _install_schema(self):
        init_db(self.db)
        with connect(self.db) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS multistore_theme_status_cache(
              store_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, shop_domain TEXT,
              graphql_state TEXT, theme_access_cli_state TEXT,
              theme_access_credential_present INTEGER NOT NULL DEFAULT 0,
              main_theme_id TEXT, main_theme_name TEXT, main_theme_role TEXT,
              auto_backend TEXT, selection_reason TEXT, check_status TEXT NOT NULL,
              diagnostic_code TEXT)""")

    def _cache(self) -> dict[str, dict]:
        with connect(self.db) as con:
            rows = con.execute("SELECT * FROM multistore_theme_status_cache").fetchall()
        return {str(row["store_id"]): dict(row) for row in rows}

    def _coordinator(self):
        if self._coordinator_instance is None:
            with self._coordinator_lock:
                if self._coordinator_instance is None:
                    self._coordinator_instance = self.coordinator_factory()
        return self._coordinator_instance

    def _base_row(self, store: dict, cache: dict | None = None, *, verify_auth=False) -> dict:
        store_id = str(store.get("store_id") or "")
        cached = cache or {}
        try:
            connection = self.connection_provider(store_id)
        except Exception:
            connection = None
        has_connection = bool(connection and connection.get("shop_domain"))
        connected = bool(has_connection and (connection or {}).get("credential_present"))
        if verify_auth and has_connection:
            try:
                connected = bool(self.token_provider(store_id))
            except Exception:
                connected = False
        try:
            has_theme_credential = bool(self.theme_credential_provider(store_id))
        except Exception:
            has_theme_credential = False
        try:
            auth = self.auth_status_provider(store_id) if connected else {}
        except Exception:
            auth = {}
        profile_id = (connection or {}).get("app_profile_id") if connection else None
        app_binding = "NOT_CONFIGURED"
        if connected:
            if profile_id:
                app_binding = "VERIFIED" if auth.get("production_app_bound") and (
                    auth.get("expected_app_gid") and auth.get("authenticated_app_gid") and
                    auth.get("expected_app_gid") == auth.get("authenticated_app_gid")) else "NOT_VERIFIED"
            else:
                app_binding = "LEGACY_OR_UNBOUND"
        checked_at = cached.get("checked_at")
        stale = True
        if checked_at:
            try:
                stale = datetime.now(timezone.utc) - datetime.fromisoformat(checked_at) > self.CACHE_MAX_AGE
            except (TypeError, ValueError):
                stale = True
        return {
            "store_id": store_id,
            "store_name": str(store.get("store_name") or store.get("profile", {}).get("store_name") or store_id),
            "shop_domain": (connection or {}).get("shop_domain"),
            "shopify_connection": "CONNECTED" if connected else ("CREDENTIAL_MISSING" if has_connection else "NOT_CONFIGURED"),
            "app_binding": app_binding,
            "graphql_state": cached.get("graphql_state") or "NOT_CHECKED",
            "theme_access_credential": "YES" if has_theme_credential else "NO",
            "theme_access_cli_state": cached.get("theme_access_cli_state") or (
                "NOT_CHECKED" if has_theme_credential else "CREDENTIAL_MISSING"),
            "main_theme_id": cached.get("main_theme_id"),
            "main_theme_name": cached.get("main_theme_name"),
            "main_theme_role": cached.get("main_theme_role"),
            "auto_backend": cached.get("auto_backend") or "NO_WRITE_BACKEND",
            "selection_reason": cached.get("selection_reason") or "NOT_CHECKED",
            "last_checked_at": checked_at,
            "check_status": (cached.get("check_status") if checked_at and not stale else "NOT_CHECKED"),
            "diagnostic_code": cached.get("diagnostic_code"),
            "cache_stale": bool(stale),
            "write_performed": False,
            "_connected": connected,
        }

    def list_rows(self, *, query: str = "", status_filter: str = "ALL") -> list[dict]:
        """Local-only initial render; never creates a Shopify client or runs CLI."""
        cache = self._cache()
        rows = [self._base_row(store, cache.get(str(store.get("store_id") or "")))
                for store in self.store_provider()]
        term = str(query or "").strip().casefold()
        if term:
            rows = [row for row in rows if any(term in str(row.get(key) or "").casefold()
                     for key in ("store_id", "store_name", "shop_domain"))]
        selected = str(status_filter or "ALL").upper()
        if selected == "READY":
            rows = [row for row in rows if row["check_status"] == "READY"]
        elif selected == "NEEDS_SETUP":
            rows = [row for row in rows if row["check_status"] == "NEEDS_SETUP"]
        elif selected == "GRAPHQL_ISSUE":
            rows = [row for row in rows if row["graphql_state"] not in {"NOT_CHECKED", "VERIFIED_ACTIVE"}]
        elif selected == "THEME_ACCESS_ISSUE":
            rows = [row for row in rows if row["theme_access_cli_state"] not in {"NOT_CHECKED", "READY"}]
        return sorted(rows, key=lambda row: row["store_id"])

    def _store(self, store_id: str) -> dict | None:
        wanted = str(store_id)
        return next((row for row in self.store_provider()
                     if str(row.get("store_id")) == wanted), None)

    def check_store(self, store_id: str, *, _persist=True) -> dict:
        """Refresh one known store using read-only GraphQL/CLI capability probes."""
        store = self._store(store_id)
        if not store:
            return {"store_id": str(store_id), "store_name": str(store_id), "check_status": "NEEDS_SETUP",
                    "diagnostic_code": "STORE_NOT_REGISTERED", "write_performed": False}
        row = self._base_row(store, verify_auth=True)
        if not row["_connected"]:
            row.update({"check_status": "NEEDS_SETUP", "graphql_state": "SCOPE_MISSING",
                        "theme_access_cli_state": "CREDENTIAL_MISSING" if row["theme_access_credential"] == "NO" else "NOT_CHECKED",
                        "selection_reason": "SHOPIFY_CONNECTION_REQUIRED", "diagnostic_code": "SHOPIFY_CONNECTION_REQUIRED",
                        "last_checked_at": _now(), "cache_stale": False})
            row.pop("_connected", None)
            if _persist:
                self._save(row)
            return row
        try:
            summary = self._coordinator().capability_summary(str(store_id), selection="AUTO")
            selected_backend = summary.get("selected_backend")
            backend_ready = ((selected_backend == "ADMIN_GRAPHQL" and
                              summary.get("graphql_state") == "VERIFIED_ACTIVE") or
                             (selected_backend == "THEME_ACCESS_CLI" and
                              summary.get("theme_access_cli_state") == "READY"))
            row.update({
                "graphql_state": str(summary.get("graphql_state") or "UNKNOWN"),
                "theme_access_cli_state": str(summary.get("theme_access_cli_state") or "UNKNOWN"),
                "main_theme_id": summary.get("verified_theme_id"),
                "main_theme_name": summary.get("verified_theme_name"),
                "main_theme_role": summary.get("verified_theme_role"),
                "auto_backend": summary.get("selected_backend") or "NO_WRITE_BACKEND",
                "selection_reason": summary.get("selection_reason") or "NO_APPROVED_WRITE_PATH",
                "diagnostic_code": summary.get("selection_reason"),
                "check_status": "READY" if backend_ready and
                    summary.get("verified_theme_id") and summary.get("verified_theme_role") == "MAIN" else "NEEDS_SETUP",
            })
        except Exception as exc:
            # Exception text can contain remote details; expose only its type.
            row.update({"check_status": "ERROR", "diagnostic_code": f"CHECK_FAILED_{type(exc).__name__.upper()}",
                        "selection_reason": "READ_ONLY_CHECK_FAILED"})
        row.update({"last_checked_at": _now(), "cache_stale": False, "write_performed": False})
        row.pop("_connected", None)
        if _persist:
            self._save(row)
        return row

    def _save(self, row: dict):
        with connect(self.db) as con:
            con.execute("""INSERT INTO multistore_theme_status_cache(
              store_id,checked_at,shop_domain,graphql_state,theme_access_cli_state,
              theme_access_credential_present,main_theme_id,main_theme_name,main_theme_role,
              auto_backend,selection_reason,check_status,diagnostic_code)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET
              checked_at=excluded.checked_at,shop_domain=excluded.shop_domain,
              graphql_state=excluded.graphql_state,theme_access_cli_state=excluded.theme_access_cli_state,
              theme_access_credential_present=excluded.theme_access_credential_present,
              main_theme_id=excluded.main_theme_id,main_theme_name=excluded.main_theme_name,
              main_theme_role=excluded.main_theme_role,auto_backend=excluded.auto_backend,
              selection_reason=excluded.selection_reason,check_status=excluded.check_status,
              diagnostic_code=excluded.diagnostic_code""", (
                row.get("store_id"), row.get("last_checked_at") or _now(), row.get("shop_domain"),
                row.get("graphql_state"), row.get("theme_access_cli_state"),
                int(row.get("theme_access_credential") == "YES"), row.get("main_theme_id"),
                row.get("main_theme_name"), row.get("main_theme_role"), row.get("auto_backend"),
                row.get("selection_reason"), row.get("check_status"), row.get("diagnostic_code")))

    def check_all(self, *, store_ids: list[str] | None = None,
                  progress_callback: Callable[[dict], None] | None = None) -> dict:
        """Refresh stores with bounded concurrency and per-store failure isolation."""
        known = {str(row.get("store_id")): row for row in self.store_provider()}
        ids = sorted(known if store_ids is None else (str(value) for value in store_ids))
        ids = [store_id for store_id in ids if store_id in known]
        if not ids:
            return {"results": [], "counts": {"total": 0, "READY": 0, "NEEDS_SETUP": 0, "ERROR": 0},
                    "write_performed": False, "max_concurrency": self.max_workers}
        executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="theme-read-check")
        started_at: dict[str, float] = {}
        started_lock = threading.Lock()

        def run_one(store_id):
            with started_lock:
                started_at[store_id] = time.monotonic()
            return self.check_store(store_id, _persist=False)

        pending = {executor.submit(run_one, store_id): store_id for store_id in ids}
        results: dict[str, dict] = {}
        try:
            while pending:
                now = time.monotonic()
                with started_lock:
                    expired = [future for future, store_id in pending.items()
                               if store_id in started_at and now - started_at[store_id] >= self.per_store_timeout]
                for future in expired:
                    store_id = pending.pop(future)
                    future.cancel()
                    store = known[store_id]
                    row = {"store_id": store_id, "store_name": str(store.get("store_name") or store_id),
                           "check_status": "ERROR", "diagnostic_code": "CHECK_TIMEOUT",
                           "last_checked_at": _now(), "write_performed": False}
                    self._save(row)
                    results[store_id] = row
                    if progress_callback:
                        progress_callback({"completed": len(results), "total": len(ids), "store_id": store_id})
                if not pending:
                    break
                with started_lock:
                    remaining = [self.per_store_timeout - (time.monotonic() - started_at[store_id])
                                 for future, store_id in pending.items() if store_id in started_at]
                nearest = min(remaining) if remaining else None
                done, _ = wait(tuple(pending), timeout=max(0.0, nearest) if nearest is not None else None,
                               return_when=FIRST_COMPLETED)
                for future in done:
                    store_id = pending.pop(future)
                    try:
                        results[store_id] = future.result()
                        self._save(results[store_id])
                    except Exception as exc:
                        row = {"store_id": store_id, "store_name": str(known[store_id].get("store_name") or store_id),
                               "check_status": "ERROR", "diagnostic_code": f"CHECK_FAILED_{type(exc).__name__.upper()}",
                               "last_checked_at": _now(), "write_performed": False}
                        self._save(row)
                        results[store_id] = row
                    if progress_callback:
                        progress_callback({"completed": len(results), "total": len(ids), "store_id": store_id})
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        ordered = [results[key] for key in sorted(results)]
        counts = {name: sum(row.get("check_status") == name for row in ordered)
                  for name in ("READY", "NEEDS_SETUP", "ERROR")}
        counts["total"] = len(ordered)
        return {"results": ordered, "counts": counts, "write_performed": False,
                "max_concurrency": self.max_workers}
