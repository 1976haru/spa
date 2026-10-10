"""Transport contracts and pre-write backend selection for Shopify theme files.

Business rules, previews, comment-aware JSON handling and backup records stay in
their existing services. Backends only transport exact file contents.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol


def raw_sha256(content: str | bytes) -> str:
    payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ThemeBackendCapabilities:
    backend_name: str
    status: str
    reason_code: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.status == "READY"


@dataclass(frozen=True)
class ThemeFileReadResult:
    filename: str
    content: str
    raw_sha256: str
    backend_name: str
    store_id: str
    shop_domain: str
    theme_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ThemeFileWriteRequest:
    store_id: str
    shop_domain: str
    theme_id: str
    filename: str
    content: str
    expected_remote_raw_hash: str
    confirmed: bool = False
    allow_live: bool = False
    target_is_live: bool = False


@dataclass(frozen=True)
class ThemeFileWriteResult:
    status: str
    backend_name: str
    filename: str
    write_performed: bool
    expected_raw_sha256: str | None = None
    observed_raw_sha256: str | None = None
    reason_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ThemeWriteBackend(Protocol):
    backend_name: str

    def capability_status(self, store_id: str, *, theme_id: str | None = None) -> ThemeBackendCapabilities: ...
    def read_file(self, store_id: str, shop_domain: str, theme_id: str, filename: str) -> ThemeFileReadResult: ...
    def write_file(self, request: ThemeFileWriteRequest) -> ThemeFileWriteResult: ...
    def verify_file(self, store_id: str, shop_domain: str, theme_id: str,
                    filename: str, expected_raw_sha256: str) -> ThemeFileReadResult | None: ...


class AdminGraphQLThemeBackend:
    """Thin adapter for an already-approved GraphQL transport.

    The existing feature apply services remain the owners of their guarded
    workflow. Callbacks make this adapter usable without moving that logic.
    """
    backend_name = "ADMIN_GRAPHQL"

    def __init__(self, *, capability_provider, read_file, write_file, verify_file=None):
        self._capability_provider = capability_provider
        self._read_file = read_file
        self._write_file = write_file
        self._verify_file = verify_file or read_file

    def capability_status(self, store_id: str, *, theme_id: str | None = None) -> ThemeBackendCapabilities:
        value = self._capability_provider(store_id, theme_id=theme_id)
        if isinstance(value, ThemeBackendCapabilities):
            return value
        active = bool(value.get("exemption_active") and value.get("write_themes"))
        return ThemeBackendCapabilities(self.backend_name, "READY" if active else "UNAVAILABLE",
            "GRAPHQL_EXEMPTION_ACTIVE" if active else "GRAPHQL_SCOPE_MISSING", dict(value))

    def read_file(self, store_id, shop_domain, theme_id, filename):
        return self._read_file(store_id, shop_domain, theme_id, filename)

    def write_file(self, request):
        # No fallback occurs here if the chosen GraphQL transport fails.
        return self._write_file(request)

    def verify_file(self, store_id, shop_domain, theme_id, filename, expected_raw_sha256):
        result = self._verify_file(store_id, shop_domain, theme_id, filename)
        return result if result and result.raw_sha256 == expected_raw_sha256 else None


class ThemeWriteBackendRouter:
    """Select once before mutation; a selected transport never silently changes."""
    def __init__(self, admin_graphql: ThemeWriteBackend | None, theme_access_cli: ThemeWriteBackend | None):
        self.admin_graphql = admin_graphql
        self.theme_access_cli = theme_access_cli

    def select(self, store_id: str, *, selection: str = "AUTO", theme_id: str | None = None) -> dict:
        selection = str(selection or "AUTO").upper()
        if selection not in {"AUTO", "ADMIN_GRAPHQL", "THEME_ACCESS_CLI"}:
            raise ValueError("Unsupported theme backend selection")
        graph = (self.admin_graphql.capability_status(store_id, theme_id=theme_id)
                 if self.admin_graphql else ThemeBackendCapabilities("ADMIN_GRAPHQL", "UNAVAILABLE", "NOT_CONFIGURED"))
        cli = (self.theme_access_cli.capability_status(store_id, theme_id=theme_id)
               if self.theme_access_cli else ThemeBackendCapabilities("THEME_ACCESS_CLI", "UNAVAILABLE", "NOT_CONFIGURED"))
        if selection == "ADMIN_GRAPHQL":
            chosen, reason = (self.admin_graphql, "USER_SELECTED_GRAPHQL") if graph.ready else (None, "NO_APPROVED_WRITE_PATH")
        elif selection == "THEME_ACCESS_CLI":
            chosen, reason = (self.theme_access_cli, "USER_SELECTED_CLI") if cli.ready else (None, "NO_APPROVED_WRITE_PATH")
        elif graph.ready:
            chosen, reason = self.admin_graphql, "GRAPHQL_EXEMPTION_ACTIVE"
        elif cli.ready:
            reason = "GRAPHQL_DENIED_CLI_READY" if graph.reason_code in {
                "AUTH_FAILED", "ACCESS_DENIED", "GRAPHQL_DENIED", "ACCESS_DENIED_LAST_ATTEMPT"
            } else "GRAPHQL_SCOPE_MISSING_CLI_READY"
            chosen = self.theme_access_cli
        else:
            chosen, reason = None, "NO_APPROVED_WRITE_PATH"
        return {"backend_name": chosen.backend_name if chosen else None,
                "backend": chosen, "reason_code": reason,
                "capabilities": {"ADMIN_GRAPHQL": graph, "THEME_ACCESS_CLI": cli},
                "selection": selection, "selection_locked": True}

    @staticmethod
    def write(selected: dict, request: ThemeFileWriteRequest) -> ThemeFileWriteResult:
        backend = selected.get("backend")
        if backend is None:
            return ThemeFileWriteResult("NO_WRITE_BACKEND", "NONE", request.filename, False,
                                        reason_code=selected.get("reason_code"))
        # Deliberately call only the backend selected before the write.
        return backend.write_file(request)
