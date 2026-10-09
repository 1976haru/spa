from __future__ import annotations

from shopsource.theme_write_backend import (AdminGraphQLThemeBackend, ThemeBackendCapabilities,
    ThemeFileWriteRequest, ThemeFileWriteResult, ThemeWriteBackendRouter)


class FakeBackend:
    def __init__(self, name, status="READY", reason="READY", result=None):
        self.backend_name = name
        self.status, self.reason = status, reason
        self.calls = 0
        self.result = result or ThemeFileWriteResult("REMOTE_JSON_VERIFIED", name, "templates/index.json", True)

    def capability_status(self, store_id, *, theme_id=None):
        return ThemeBackendCapabilities(self.backend_name, self.status, self.reason)

    def write_file(self, request):
        self.calls += 1
        return self.result


def test_router_prefers_graphql_when_active():
    graph = FakeBackend("ADMIN_GRAPHQL")
    cli = FakeBackend("THEME_ACCESS_CLI")
    selected = ThemeWriteBackendRouter(graph, cli).select("s1", theme_id="1")
    assert selected["backend_name"] == "ADMIN_GRAPHQL"
    assert selected["reason_code"] == "GRAPHQL_EXEMPTION_ACTIVE"


def test_router_uses_cli_when_graphql_unavailable_and_cli_ready():
    graph = FakeBackend("ADMIN_GRAPHQL", "UNAVAILABLE", "SCOPE_MISSING")
    cli = FakeBackend("THEME_ACCESS_CLI")
    selected = ThemeWriteBackendRouter(graph, cli).select("s1", theme_id="1")
    assert selected["backend_name"] == "THEME_ACCESS_CLI"
    assert selected["reason_code"] == "GRAPHQL_SCOPE_MISSING_CLI_READY"


def test_router_no_backend_when_both_unavailable():
    graph = FakeBackend("ADMIN_GRAPHQL", "UNAVAILABLE", "SCOPE_MISSING")
    cli = FakeBackend("THEME_ACCESS_CLI", "CREDENTIAL_MISSING", "CREDENTIAL_MISSING")
    selected = ThemeWriteBackendRouter(graph, cli).select("s1", theme_id="1")
    assert selected["backend_name"] is None and selected["reason_code"] == "NO_APPROVED_WRITE_PATH"


def test_router_does_not_switch_after_write_failure():
    failed = ThemeFileWriteResult("WRITE_ATTEMPTED", "ADMIN_GRAPHQL", "templates/index.json", True)
    graph = FakeBackend("ADMIN_GRAPHQL", result=failed)
    cli = FakeBackend("THEME_ACCESS_CLI")
    router = ThemeWriteBackendRouter(graph, cli)
    selected = router.select("s1", theme_id="1")
    request = ThemeFileWriteRequest("s1", "shop.myshopify.com", "1", "templates/index.json", "{}", "hash", True)
    assert router.write(selected, request) is failed
    assert graph.calls == 1 and cli.calls == 0


def test_admin_graphql_adapter_requires_exemption_and_write_scope():
    backend = AdminGraphQLThemeBackend(capability_provider=lambda *_a, **_k:
        {"exemption_active": True, "write_themes": True}, read_file=lambda *_a: None,
        write_file=lambda request: request)
    assert backend.capability_status("s1").ready is True


def test_no_shopify_live_write():
    # Router test only: no live Shopify client, credential, or backend write runs.
    graph = FakeBackend("ADMIN_GRAPHQL", "UNAVAILABLE", "SCOPE_MISSING")
    cli = FakeBackend("THEME_ACCESS_CLI", "CREDENTIAL_MISSING", "CREDENTIAL_MISSING")
    selected = ThemeWriteBackendRouter(graph, cli).select("s1", theme_id="1")
    request = ThemeFileWriteRequest("s1", "shop.myshopify.com", "1", "templates/index.json", "{}", "0" * 64, True)
    result = ThemeWriteBackendRouter.write(selected, request)
    assert result.status == "NO_WRITE_BACKEND" and result.write_performed is False
    assert graph.calls == 0 and cli.calls == 0
