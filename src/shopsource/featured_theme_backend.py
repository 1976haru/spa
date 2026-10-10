"""Featured Products transport selection and durable, non-secret GraphQL evidence."""
from __future__ import annotations

from datetime import datetime, timezone

from .db import connect
from .shopify_collections import ShopifyGraphQLClient, get_connection, get_shopify_token
from .shopify_theme_cli import ShopifyThemeCLI
from .theme_access_credentials import credential_present
from .theme_write_backend import (AdminGraphQLThemeBackend, ThemeBackendCapabilities,
                                  ThemeFileReadResult, ThemeFileWriteResult,
                                  ThemeWriteBackendRouter, raw_sha256)

THEME_IDENTITY_QUERY = "query FeaturedThemeIdentity($id: ID!) { theme(id: $id) { id name role } }"
FEATURED_THEME_EVIDENCE_QUERY = "query FeaturedThemeEvidence($id: ID!, $filenames: [String!]!) { currentAppInstallation { accessScopes { handle } } theme(id: $id) { id name role files(first: 1, filenames: $filenames) { nodes { filename body { __typename ... on OnlineStoreThemeFileBodyText { content } ... on OnlineStoreThemeFileBodyBase64 { contentBase64 } } } userErrors { code filename } } } }"
FEATURED_SCOPES_QUERY = "query FeaturedScopes { currentAppInstallation { accessScopes { handle } } }"
FEATURED_FILENAME = "templates/index.json"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FeaturedThemeBackendCoordinator:
    """Create adapters and select a single transport before any Featured write."""

    def __init__(self, *, db=None, client_factory=ShopifyGraphQLClient, client=None,
                 cli_backend=None):
        self.db = db
        self.client_factory = client_factory
        self.client_override = client
        self.cli_backend = cli_backend or ShopifyThemeCLI(db=db)
        self._install_schema()

    def _install_schema(self):
        with connect(self.db) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS theme_write_backend_evidence(
              store_id TEXT NOT NULL, theme_id TEXT NOT NULL, backend_name TEXT NOT NULL,
              status TEXT NOT NULL, evidence_source TEXT NOT NULL,
              last_attempt_at TEXT, last_success_at TEXT, updated_at TEXT NOT NULL,
              PRIMARY KEY(store_id,theme_id,backend_name))""")

    def _client(self, store_id):
        if self.client_override is not None:
            config = get_connection(store_id, db=self.db)
            return config, self.client_override
        config = get_connection(store_id, db=self.db)
        token = get_shopify_token(store_id, db=self.db)[0]
        if not config or not token:
            return config, None
        return config, self.client_factory(config["shop_domain"], token, config["api_version"])

    def _evidence(self, store_id, theme_id):
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM theme_write_backend_evidence WHERE store_id=? AND theme_id=? AND backend_name='ADMIN_GRAPHQL'",
                              (str(store_id), str(theme_id))).fetchone()
        return dict(row) if row else None

    def _record(self, store_id, theme_id, status, evidence_source):
        now = _now()
        with connect(self.db) as con:
            previous = con.execute("SELECT last_success_at FROM theme_write_backend_evidence WHERE store_id=? AND theme_id=? AND backend_name='ADMIN_GRAPHQL'",
                                   (str(store_id), str(theme_id))).fetchone()
            last_success = now if status == "VERIFIED_ACTIVE" else (previous["last_success_at"] if previous else None)
            con.execute("""INSERT INTO theme_write_backend_evidence
              (store_id,theme_id,backend_name,status,evidence_source,last_attempt_at,last_success_at,updated_at)
              VALUES(?,?,'ADMIN_GRAPHQL',?,?,?,?,?)
              ON CONFLICT(store_id,theme_id,backend_name) DO UPDATE SET
              status=excluded.status,evidence_source=excluded.evidence_source,
              last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,
              updated_at=excluded.updated_at""",
                        (str(store_id), str(theme_id), status, evidence_source, now, last_success, now))

    def build_graphql_backend(self, *, client=None):
        """Build a conservative adapter; scope-only GraphQL remains unavailable."""
        return self._build_graphql_backend(client=client, allow_unverified_probe=False)

    def _build_graphql_backend(self, *, client=None, allow_unverified_probe=False):
        fixed_client = client

        def client_for(store_id):
            if fixed_client is not None:
                return get_connection(store_id, db=self.db), fixed_client
            return self._client(store_id)

        def scope_set(gql):
            data = gql.execute(FEATURED_SCOPES_QUERY).get("currentAppInstallation") or {}
            return {str(item.get("handle") or "") for item in data.get("accessScopes", [])}

        def capability(store_id, *, theme_id=None):
            config, gql = client_for(store_id)
            if not config or not gql:
                return ThemeBackendCapabilities("ADMIN_GRAPHQL", "UNAVAILABLE", "CREDENTIAL_MISSING",
                                                {"state": "SCOPE_MISSING"})
            try:
                scopes = scope_set(gql)
            except Exception:
                evidence = self._evidence(store_id, theme_id) if theme_id else None
                state = evidence["status"] if evidence and evidence["status"] == "ACCESS_DENIED_LAST_ATTEMPT" else "SCOPE_MISSING"
                return ThemeBackendCapabilities("ADMIN_GRAPHQL", "UNAVAILABLE", "GRAPHQL_SCOPE_CHECK_FAILED",
                                                {"state": state})
            evidence = self._evidence(store_id, theme_id) if theme_id else None
            state = "SCOPE_MISSING" if "write_themes" not in scopes else (
                evidence["status"] if evidence else "SCOPE_GRANTED_UNVERIFIED")
            if not theme_id or "read_themes" not in scopes or "write_themes" not in scopes:
                return ThemeBackendCapabilities("ADMIN_GRAPHQL", "UNAVAILABLE", state,
                                                {"state": "SCOPE_MISSING" if "read_themes" not in scopes or "write_themes" not in scopes else state,
                                                 "granted_scopes": sorted(scopes)})
            try:
                identity = gql.execute(THEME_IDENTITY_QUERY, {"id": theme_id}).get("theme") or {}
            except Exception:
                identity = {}
            if identity.get("id") != theme_id or identity.get("role") != "MAIN":
                return ThemeBackendCapabilities("ADMIN_GRAPHQL", "UNAVAILABLE", "MAIN_THEME_IDENTITY_UNVERIFIED",
                    {"state": state, "verified_theme_id": identity.get("id"), "verified_theme_name": identity.get("name"),
                     "verified_theme_role": identity.get("role")})
            details = {"state": state, "granted_scopes": sorted(scopes), "verified_theme_id": identity["id"],
                       "verified_theme_name": identity.get("name"), "verified_theme_role": "MAIN",
                       "target_is_live": True, "identity_verified": True}
            if state == "VERIFIED_ACTIVE":
                return ThemeBackendCapabilities("ADMIN_GRAPHQL", "READY", "GRAPHQL_EXEMPTION_VERIFIED", details)
            if state == "SCOPE_GRANTED_UNVERIFIED" and allow_unverified_probe:
                details["verification_attempt"] = True
                return ThemeBackendCapabilities("ADMIN_GRAPHQL", "READY",
                    "USER_SELECTED_GRAPHQL_VERIFICATION_ATTEMPT", details)
            return ThemeBackendCapabilities("ADMIN_GRAPHQL", "UNAVAILABLE", state, details)

        def read(store_id, shop_domain, theme_id, filename):
            if filename != FEATURED_FILENAME:
                raise ValueError("FILE_NOT_ALLOWLISTED")
            config, gql = client_for(store_id)
            if not config or not gql or str(shop_domain).casefold() != str(config.get("shop_domain", "")).casefold():
                raise RuntimeError("STORE_NOT_CONFIGURED")
            if "read_themes" not in scope_set(gql):
                raise RuntimeError("READ_THEMES_SCOPE_MISSING")
            try:
                result = gql.execute(FEATURED_THEME_EVIDENCE_QUERY,
                                     {"id": theme_id, "filenames": [filename]})
            except Exception:
                raise RuntimeError("GRAPHQL_THEME_READ_FAILED") from None
            theme = result.get("theme") or {}
            if theme.get("id") != theme_id or theme.get("role") != "MAIN":
                raise RuntimeError("MAIN_THEME_IDENTITY_UNVERIFIED")
            from .homepage_featured_products import FeaturedProductThemeApplyService
            raw, _document = FeaturedProductThemeApplyService._remote_document(theme, filename)
            return ThemeFileReadResult(filename, raw, raw_sha256(raw), "ADMIN_GRAPHQL", str(store_id),
                                       str(shop_domain), str(theme_id), {"verified_theme_role": "MAIN"})

        def write(request):
            if (request.filename != FEATURED_FILENAME or request.confirmed is not True
                    or request.target_is_live is not True or request.allow_live is not True):
                return ThemeFileWriteResult("PRECONDITION_FAILED", "ADMIN_GRAPHQL", request.filename, False,
                                            reason_code="MAIN_CONFIRMATION_OR_ALLOWLIST_REQUIRED")
            approved = capability(request.store_id, theme_id=request.theme_id)
            if not approved.ready:
                return ThemeFileWriteResult("PRECONDITION_FAILED", "ADMIN_GRAPHQL", request.filename, False,
                                            reason_code=approved.reason_code)
            try:
                current = read(request.store_id, request.shop_domain, request.theme_id, request.filename)
            except Exception:
                return ThemeFileWriteResult("PRECONDITION_FAILED", "ADMIN_GRAPHQL", request.filename, False,
                                            reason_code="REMOTE_READ_FAILED")
            if current.raw_sha256 != request.expected_remote_raw_hash:
                return ThemeFileWriteResult("PRECONDITION_FAILED", "ADMIN_GRAPHQL", request.filename, False,
                    expected_raw_sha256=request.expected_remote_raw_hash, observed_raw_sha256=current.raw_sha256,
                    reason_code="REMOTE_CHANGED_ABORT")
            config, gql = client_for(request.store_id)
            if not config or not gql:
                return ThemeFileWriteResult("PRECONDITION_FAILED", "ADMIN_GRAPHQL", request.filename, False,
                                            reason_code="CREDENTIAL_MISSING")
            if "write_themes" not in scope_set(gql):
                return ThemeFileWriteResult("PRECONDITION_FAILED", "ADMIN_GRAPHQL", request.filename, False,
                                            reason_code="WRITE_THEMES_SCOPE_MISSING")
            from .homepage_automation import UPSERT_THEME_FILES
            try:
                payload = gql.execute(UPSERT_THEME_FILES, {"themeId": request.theme_id, "files": [
                    {"filename": request.filename, "body": {"type": "TEXT", "value": request.content}}]})
            except Exception as exc:
                denied = "access denied" in str(exc).casefold() or "forbidden" in str(exc).casefold()
                if denied:
                    self._record(request.store_id, request.theme_id, "ACCESS_DENIED_LAST_ATTEMPT", "THEME_FILES_UPSERT_ACCESS_DENIED")
                return ThemeFileWriteResult("ACCESS_DENIED" if denied else "FAILED", "ADMIN_GRAPHQL",
                    request.filename, True, reason_code="ACCESS_DENIED" if denied else "GRAPHQL_WRITE_FAILED")
            result = payload.get("themeFilesUpsert") or {}
            errors = result.get("userErrors") or []
            denied = any("access" in str(error).casefold() or "forbidden" in str(error).casefold() for error in errors)
            if denied:
                self._record(request.store_id, request.theme_id, "ACCESS_DENIED_LAST_ATTEMPT", "THEME_FILES_UPSERT_ACCESS_DENIED")
                return ThemeFileWriteResult("ACCESS_DENIED", "ADMIN_GRAPHQL", request.filename, True,
                                            reason_code="ACCESS_DENIED")
            if errors:
                return ThemeFileWriteResult("FAILED", "ADMIN_GRAPHQL", request.filename, True,
                                            reason_code="THEME_FILES_UPSERT_USER_ERRORS")
            returned = result.get("upsertedThemeFiles") or []
            if not any(item.get("filename") == request.filename for item in returned if isinstance(item, dict)):
                return ThemeFileWriteResult("FAILED", "ADMIN_GRAPHQL", request.filename, True,
                                            reason_code="THEME_FILES_UPSERT_UNVERIFIED")
            self._record(request.store_id, request.theme_id, "VERIFIED_ACTIVE", "SUCCESSFUL_THEME_FILES_UPSERT")
            return ThemeFileWriteResult("WRITE_ATTEMPTED", "ADMIN_GRAPHQL", request.filename, True,
                                        expected_raw_sha256=raw_sha256(request.content))

        def verify(store_id, shop_domain, theme_id, filename):
            return read(store_id, shop_domain, theme_id, filename)

        return AdminGraphQLThemeBackend(capability_provider=capability, read_file=read,
                                        write_file=write, verify_file=verify)

    def build_cli_backend(self):
        return self.cli_backend

    def _router(self, *, client=None, allow_unverified_probe=False):
        return ThemeWriteBackendRouter(self._build_graphql_backend(
            client=client, allow_unverified_probe=allow_unverified_probe), self.build_cli_backend())

    def select_backend(self, store_id, theme_id, selection="AUTO", *, client=None):
        explicit_graphql = str(selection or "AUTO").upper() == "ADMIN_GRAPHQL"
        selected = self._router(client=client, allow_unverified_probe=explicit_graphql).select(
            store_id, theme_id=theme_id, selection=selection)
        graph = selected["capabilities"]["ADMIN_GRAPHQL"]
        if (explicit_graphql and selected.get("backend_name") == "ADMIN_GRAPHQL"
                and graph.reason_code == "USER_SELECTED_GRAPHQL_VERIFICATION_ATTEMPT"):
            selected["reason_code"] = graph.reason_code
        return selected

    def capability_summary(self, store_id, theme_id=None, selection="AUTO"):
        identity = {"id": theme_id, "name": None, "role": None}
        if not theme_id:
            config, gql = self._client(store_id)
            if gql:
                try:
                    scopes = {x.get("handle") for x in (gql.execute(FEATURED_SCOPES_QUERY).get("currentAppInstallation") or {}).get("accessScopes", [])}
                    if "read_themes" in scopes:
                        rows = gql.execute("query FeaturedThemes { themes(first: 100) { nodes { id name role } } }").get("themes", {}).get("nodes", [])
                        identity = next(({"id": row.get("id"), "name": row.get("name"), "role": row.get("role")}
                                         for row in rows if row.get("role") == "MAIN"), identity)
                except Exception:
                    pass
            theme_id = identity.get("id")
        selected = self.select_backend(store_id, theme_id, selection)
        graph = selected["capabilities"]["ADMIN_GRAPHQL"]
        cli = selected["capabilities"]["THEME_ACCESS_CLI"]
        has_credential = credential_present(store_id)
        main = graph.details if graph.details.get("identity_verified") else cli.details
        if not identity.get("id"):
            identity = {"id": theme_id, "name": main.get("verified_theme_name"),
                        "role": main.get("verified_theme_role")}
        return {"graphql_state": graph.details.get("state", graph.status),
                "graphql_reason_code": graph.reason_code,
                "verification_attempt": bool(graph.details.get("verification_attempt")),
                "theme_access_cli_state": "READY" if cli.ready else ("CREDENTIAL_MISSING" if not has_credential else cli.reason_code),
                "theme_access_cli_reason_code": cli.reason_code,
                "selected_backend": selected.get("backend_name"), "selection_reason": selected.get("reason_code"),
                "verified_theme_id": identity.get("id") or main.get("verified_theme_id"),
                "verified_theme_name": identity.get("name") or main.get("verified_theme_name"),
                "verified_theme_role": identity.get("role") or main.get("verified_theme_role"),
                "credential_present": has_credential,
                "selection_locked": selected.get("selection_locked", True)}
