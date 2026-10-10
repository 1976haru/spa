from __future__ import annotations

import json

from shopsource.db import init_db, connect
from shopsource.featured_theme_backend import FeaturedThemeBackendCoordinator
from shopsource.theme_write_backend import ThemeBackendCapabilities, ThemeFileWriteRequest, ThemeFileReadResult, ThemeFileWriteResult


THEME_ID = "gid://shopify/OnlineStoreTheme/55"
RAW = '/* preserved */\n{"sections":{},"order":[]}\n'


class MockGraphQL:
    def __init__(self, scopes=(), *, access_denied=False):
        self.scopes = set(scopes)
        self.access_denied = access_denied
        self.raw = RAW
        self.mutations = 0
        self.queries = []

    def execute(self, query, variables=None):
        self.queries.append(query)
        if "FeaturedScopes" in query or "FeaturedThemeEvidence" in query:
            data = {"currentAppInstallation": {"accessScopes": [{"handle": s} for s in self.scopes]}}
            if "theme(" in query:
                data["theme"] = {"id": THEME_ID, "name": "Main", "role": "MAIN", "files": {"nodes": [
                    {"filename": "templates/index.json", "body": {"content": self.raw}}]}}
            return data
        if "FeaturedThemeIdentity" in query:
            return {"theme": {"id": THEME_ID, "name": "Main", "role": "MAIN"}}
        if "themeFilesUpsert" in query:
            self.mutations += 1
            if self.access_denied:
                raise RuntimeError("Shopify GraphQL: access denied")
            self.raw = variables["files"][0]["body"]["value"]
            return {"themeFilesUpsert": {"userErrors": [], "upsertedThemeFiles": [
                {"filename": variables["files"][0]["filename"]}]}}
        raise AssertionError("Unexpected GraphQL operation")


class MockBackend:
    def __init__(self, name, reason="READY"):
        self.backend_name = name
        self.reason = reason
        self.raw = RAW
        self.reads = 0
        self.writes = 0

    def capability_status(self, store_id, *, theme_id=None):
        ready = self.reason == "READY"
        return ThemeBackendCapabilities(self.backend_name, "READY" if ready else "UNAVAILABLE", self.reason,
            {"identity_verified": ready, "verified_theme_id": theme_id if ready else None,
             "verified_theme_role": "MAIN" if ready else None, "target_is_live": True if ready else None,
             "state": self.reason})

    def read_file(self, store_id, shop_domain, theme_id, filename):
        self.reads += 1
        return ThemeFileReadResult(filename, self.raw, __import__("hashlib").sha256(self.raw.encode()).hexdigest(),
            self.backend_name, store_id, shop_domain, theme_id, {"verified_theme_role": "MAIN"})

    def write_file(self, request):
        self.writes += 1
        self.raw = request.content
        return ThemeFileWriteResult("WRITE_ATTEMPTED", self.backend_name, request.filename, True)


def _coordinator(tmp_path, monkeypatch, graph, cli):
    db = tmp_path / "featured-backend.sqlite3"
    init_db(db)
    monkeypatch.setattr("shopsource.featured_theme_backend.get_connection",
                        lambda *_a, **_k: {"shop_domain": "sample.myshopify.com", "api_version": "2026-07"})
    monkeypatch.setattr("shopsource.featured_theme_backend.get_shopify_token", lambda *_a, **_k: ("mock-token", "test"))
    coordinator = FeaturedThemeBackendCoordinator(db=db, client=graph, cli_backend=cli)
    return db, coordinator


def _seed_historical_success(db):
    with connect(db) as con:
        con.execute("""INSERT INTO theme_write_backend_evidence
            (store_id,theme_id,backend_name,status,evidence_source,last_attempt_at,last_success_at,updated_at)
            VALUES('s1',?,'ADMIN_GRAPHQL','VERIFIED_ACTIVE','TEST_HISTORICAL_SUCCESS','test','test','test')""",
                    (THEME_ID,))


def test_featured_auto_uses_cli_when_graphql_scope_missing_and_cli_ready(tmp_path, monkeypatch):
    graph = MockGraphQL({"read_themes"})
    cli = MockBackend("THEME_ACCESS_CLI")
    _db, coordinator = _coordinator(tmp_path, monkeypatch, graph, cli)
    selected = coordinator.select_backend("s1", THEME_ID, "AUTO")
    assert selected["backend_name"] == "THEME_ACCESS_CLI"
    assert selected["capabilities"]["ADMIN_GRAPHQL"].details["state"] == "SCOPE_MISSING"
    assert graph.mutations == cli.writes == 0


def test_featured_auto_no_backend_when_graphql_missing_and_cli_credential_missing(tmp_path, monkeypatch):
    graph = MockGraphQL({"read_themes"})
    cli = MockBackend("THEME_ACCESS_CLI", "CREDENTIAL_MISSING")
    _db, coordinator = _coordinator(tmp_path, monkeypatch, graph, cli)
    selected = coordinator.select_backend("s1", THEME_ID, "AUTO")
    assert selected["backend_name"] is None
    assert selected["reason_code"] == "NO_APPROVED_WRITE_PATH"


def test_graphql_scope_does_not_equal_verified_exemption(tmp_path, monkeypatch):
    graph = MockGraphQL({"read_themes", "write_themes"})
    cli = MockBackend("THEME_ACCESS_CLI", "CREDENTIAL_MISSING")
    db, coordinator = _coordinator(tmp_path, monkeypatch, graph, cli)
    selected = coordinator.select_backend("s1", THEME_ID, "AUTO")
    assert selected["backend_name"] is None
    assert selected["capabilities"]["ADMIN_GRAPHQL"].reason_code == "SCOPE_GRANTED_UNVERIFIED"
    explicitly_selected = coordinator.select_backend("s1", THEME_ID, "ADMIN_GRAPHQL")
    assert explicitly_selected["backend_name"] is None
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM theme_write_backend_evidence").fetchone()[0] == 0
    assert graph.mutations == 0
    request = ThemeFileWriteRequest("s1", "sample.myshopify.com", THEME_ID, "templates/index.json", RAW,
        __import__("hashlib").sha256(RAW.encode()).hexdigest(), confirmed=True, allow_live=True, target_is_live=True)
    denied = coordinator.build_graphql_backend().write_file(request)
    assert not denied.write_performed and graph.mutations == 0


def test_graphql_success_records_verified_active(tmp_path, monkeypatch):
    graph = MockGraphQL({"read_themes", "write_themes"})
    db, coordinator = _coordinator(tmp_path, monkeypatch, graph, MockBackend("THEME_ACCESS_CLI", "CREDENTIAL_MISSING"))
    _seed_historical_success(db)
    backend = coordinator.build_graphql_backend()
    request = ThemeFileWriteRequest("s1", "sample.myshopify.com", THEME_ID, "templates/index.json",
        '{"sections":{},"order":[]}\n', __import__("hashlib").sha256(RAW.encode()).hexdigest(),
        confirmed=True, allow_live=True, target_is_live=True)
    result = backend.write_file(request)
    assert result.write_performed and graph.mutations == 1
    with connect(db) as con:
        evidence = con.execute("SELECT status,evidence_source FROM theme_write_backend_evidence").fetchone()
    assert tuple(evidence) == ("VERIFIED_ACTIVE", "SUCCESSFUL_THEME_FILES_UPSERT")


def test_graphql_access_denied_records_denied_evidence(tmp_path, monkeypatch):
    graph = MockGraphQL({"read_themes", "write_themes"}, access_denied=True)
    cli = MockBackend("THEME_ACCESS_CLI")
    db, coordinator = _coordinator(tmp_path, monkeypatch, graph, cli)
    _seed_historical_success(db)
    result = coordinator.build_graphql_backend().write_file(ThemeFileWriteRequest(
        "s1", "sample.myshopify.com", THEME_ID, "templates/index.json", RAW,
        __import__("hashlib").sha256(RAW.encode()).hexdigest(), confirmed=True, allow_live=True, target_is_live=True))
    assert result.status == "ACCESS_DENIED"
    with connect(db) as con:
        evidence = con.execute("SELECT status FROM theme_write_backend_evidence").fetchone()[0]
    assert evidence == "ACCESS_DENIED_LAST_ATTEMPT"
    selected = coordinator.select_backend("s1", THEME_ID, "AUTO")
    assert selected["backend_name"] == "THEME_ACCESS_CLI"
    assert selected["reason_code"] == "GRAPHQL_DENIED_CLI_READY"


def test_backend_capability_check_is_read_only(tmp_path, monkeypatch):
    graph = MockGraphQL({"read_themes"})
    _db, coordinator = _coordinator(tmp_path, monkeypatch, graph, MockBackend("THEME_ACCESS_CLI", "CREDENTIAL_MISSING"))
    summary = coordinator.capability_summary("s1", THEME_ID)
    assert summary["graphql_state"] == "SCOPE_MISSING"
    assert summary["theme_access_cli_state"] == "CREDENTIAL_MISSING"
    assert summary["selected_backend"] is None
    assert graph.mutations == 0
    assert not any("themeFilesUpsert" in query for query in graph.queries)


def test_theme_access_password_ui_never_echoes_secret_and_clears_after_save():
    from pathlib import Path
    source = Path("src/shopsource/ui/v2.py").read_text(encoding="utf-8")
    assert "save_theme_access_password(self.current_store, theme_access_password.value or \"\")" in source
    assert "theme_access_password.value = \"\"" in source
    assert "Theme Access credential: {'YES' if present else 'NO'}" in source
    assert "password.value}" not in source


def test_current_cabin_without_theme_access_would_show_no_backend(tmp_path, monkeypatch):
    graph = MockGraphQL({"read_themes"})
    cli = MockBackend("THEME_ACCESS_CLI", "CREDENTIAL_MISSING")
    _db, coordinator = _coordinator(tmp_path, monkeypatch, graph, cli)
    monkeypatch.setattr("shopsource.featured_theme_backend.credential_present", lambda _store: False)
    summary = coordinator.capability_summary("001", THEME_ID)
    assert summary["graphql_state"] == "SCOPE_MISSING"
    assert summary["theme_access_cli_state"] == "CREDENTIAL_MISSING"
    assert summary["selected_backend"] is None
    assert summary["credential_present"] is False
