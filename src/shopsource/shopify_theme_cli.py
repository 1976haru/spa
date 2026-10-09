"""Read-only capability checks and bounded Theme Access Shopify CLI transport.

Theme Access is a merchant-controlled operational credential, not a Shopify
security bypass. This module is intentionally not wired to the application UI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable

from .shopify_collections import get_connection
from .theme_access_credentials import get_theme_access_password
from .theme_write_backend import (ThemeBackendCapabilities, ThemeFileReadResult,
                                  ThemeFileWriteRequest, ThemeFileWriteResult,
                                  raw_sha256)

TARGET_FILE = "templates/index.json"
MINIMUM_THEME_ACCESS_CLI_MAJOR = 3


def _safe_target(filename: str) -> bool:
    try:
        path = PurePosixPath(str(filename))
        return (str(path) == TARGET_FILE and not path.is_absolute()
                and all(part not in {"", ".", ".."} for part in path.parts))
    except (TypeError, ValueError):
        return False


class ShopifyThemeCLI:
    backend_name = "THEME_ACCESS_CLI"

    def __init__(self, *, db=None, runner: Callable = subprocess.run, which: Callable = shutil.which,
                 temp_dir: str | Path | None = None, retain_diagnostics: bool = False,
                 credential_getter: Callable = get_theme_access_password):
        self.db, self.runner, self.which = db, runner, which
        self.temp_dir = Path(temp_dir) if temp_dir else None
        self.retain_diagnostics = retain_diagnostics
        self.credential_getter = credential_getter

    def _executable(self):
        return self.which("shopify")

    def capability_status(self, store_id: str, *, theme_id: str | None = None) -> ThemeBackendCapabilities:
        executable = self._executable()
        if not executable:
            return ThemeBackendCapabilities(self.backend_name, "CLI_NOT_INSTALLED", "CLI_NOT_INSTALLED")
        try:
            result = self.runner([executable, "version"], capture_output=True, text=True,
                                 timeout=15, check=False, env={**os.environ, "SHOPIFY_CLI_THEME_TOKEN": ""})
        except (OSError, subprocess.TimeoutExpired):
            return ThemeBackendCapabilities(self.backend_name, "UNSUPPORTED_CLI", "UNSUPPORTED_CLI")
        version_text = f"{getattr(result, 'stdout', '')} {getattr(result, 'stderr', '')}".strip()
        match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?", version_text)
        if getattr(result, "returncode", 1) != 0 or not match or int(match.group(1)) < MINIMUM_THEME_ACCESS_CLI_MAJOR:
            return ThemeBackendCapabilities(self.backend_name, "UNSUPPORTED_CLI", "UNSUPPORTED_CLI")
        password = self.credential_getter(store_id)
        if not password:
            return ThemeBackendCapabilities(self.backend_name, "CREDENTIAL_MISSING", "CREDENTIAL_MISSING",
                                            {"cli_version": match.group(0)})
        connection = get_connection(store_id, db=self.db)
        if not connection or not connection.get("shop_domain"):
            return ThemeBackendCapabilities(self.backend_name, "STORE_NOT_CONFIGURED", "STORE_NOT_CONFIGURED",
                                            {"cli_version": match.group(0)})
        if not theme_id:
            return ThemeBackendCapabilities(self.backend_name, "THEME_ID_MISSING", "THEME_ID_MISSING",
                                            {"cli_version": match.group(0), "shop_domain": connection["shop_domain"]})
        try:
            probe = self.runner([executable, "theme", "list", "--store", connection["shop_domain"], "--json"],
                                capture_output=True, text=True, timeout=30, check=False,
                                env={**os.environ, "SHOPIFY_CLI_THEME_TOKEN": password})
        except subprocess.TimeoutExpired:
            return ThemeBackendCapabilities(self.backend_name, "NETWORK_ERROR", "NETWORK_ERROR")
        except OSError:
            return ThemeBackendCapabilities(self.backend_name, "CLI_NOT_INSTALLED", "CLI_NOT_INSTALLED")
        if getattr(probe, "returncode", 1) != 0:
            diagnostic = f"{getattr(probe, 'stdout', '')} {getattr(probe, 'stderr', '')}".casefold()
            if any(term in diagnostic for term in ("not recognized", "unknown command", "unknown option")):
                status = "UNSUPPORTED_CLI"
            elif any(term in diagnostic for term in ("network", "timed out", "dns", "enotfound", "econn")):
                status = "NETWORK_ERROR"
            else:
                status = "AUTH_FAILED"
            return ThemeBackendCapabilities(self.backend_name, status, status,
                                            {"cli_version": match.group(0), "shop_domain": connection["shop_domain"]})
        return ThemeBackendCapabilities(self.backend_name, "READY", "READY",
            {"cli_version": match.group(0), "shop_domain": connection["shop_domain"], "theme_id": str(theme_id),
             "credential_present": True, "merchant_controlled_auth": True})

    @staticmethod
    def _theme_id(theme_id: str) -> str:
        value = str(theme_id or "")
        if not re.fullmatch(r"(?:gid://shopify/OnlineStoreTheme/)?\d+", value):
            raise ValueError("A valid Shopify theme ID is required")
        return value.rsplit("/", 1)[-1]

    def _check_connection(self, store_id, shop_domain, theme_id):
        capability = self.capability_status(store_id, theme_id=theme_id)
        if not capability.ready:
            return None, capability
        configured = capability.details.get("shop_domain", "").casefold()
        if str(shop_domain or "").casefold() != configured:
            return None, ThemeBackendCapabilities(self.backend_name, "STORE_NOT_CONFIGURED", "STORE_MISMATCH")
        try:
            password = self.credential_getter(store_id)
        except Exception:
            password = None
        return password, capability

    @staticmethod
    def _scaffold(root: Path):
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "templates").mkdir(parents=True, exist_ok=True)
        schema = root / "config" / "settings_schema.json"
        if not schema.exists():
            schema.write_text("[]\n", encoding="utf-8")

    def _invoke(self, command, password):
        env = os.environ.copy()
        env["SHOPIFY_CLI_THEME_TOKEN"] = password
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=120, check=False, env=env)
        except Exception:
            return None
        # Never return or log raw subprocess output; it can echo environment values.
        return result if getattr(result, "returncode", 1) == 0 else None

    def _pull(self, store_id, shop_domain, theme_id, filename):
        if not _safe_target(filename):
            raise ValueError("Only templates/index.json is allowed in Phase A")
        password, capability = self._check_connection(store_id, shop_domain, theme_id)
        if not password:
            raise RuntimeError(capability.reason_code)
        temp = Path(tempfile.mkdtemp(prefix="shopsource-theme-pull-", dir=self.temp_dir))
        try:
            self._scaffold(temp)
            command = [self._executable(), "theme", "pull", "--store", shop_domain,
                       "--theme", self._theme_id(theme_id), "--only", TARGET_FILE,
                       "--path", str(temp), "--json"]
            if self._invoke(command, password) is None:
                raise RuntimeError("Theme Access CLI pull failed")
            target = temp / TARGET_FILE
            if not target.is_file():
                raise RuntimeError("Theme Access CLI did not return the requested file")
            content = target.read_bytes().decode("utf-8")
            return content, {"isolated_path": str(temp), "cli_version": capability.details.get("cli_version")}
        finally:
            if not self.retain_diagnostics:
                shutil.rmtree(temp, ignore_errors=True)

    def read_file(self, store_id: str, shop_domain: str, theme_id: str, filename: str) -> ThemeFileReadResult:
        content, metadata = self._pull(store_id, shop_domain, theme_id, filename)
        return ThemeFileReadResult(filename, content, raw_sha256(content), self.backend_name,
                                   str(store_id), str(shop_domain), str(theme_id),
                                   {**metadata, "read_only": True})

    def verify_file(self, store_id, shop_domain, theme_id, filename, expected_raw_sha256):
        try:
            result = self.read_file(store_id, shop_domain, theme_id, filename)
        except Exception:
            return None
        return result if result.raw_sha256 == expected_raw_sha256 else None

    def _push(self, request: ThemeFileWriteRequest, password: str, content: str):
        temp = Path(tempfile.mkdtemp(prefix="shopsource-theme-push-", dir=self.temp_dir))
        try:
            self._scaffold(temp)
            target = temp / TARGET_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
            command = [self._executable(), "theme", "push", "--store", request.shop_domain,
                       "--theme", self._theme_id(request.theme_id), "--only", TARGET_FILE,
                       "--path", str(temp), "--nodelete", "--strict", "--json"]
            if request.target_is_live:
                command.append("--allow-live")
            result = self._invoke(command, password)
            return result is not None
        finally:
            if not self.retain_diagnostics:
                shutil.rmtree(temp, ignore_errors=True)

    def write_file(self, request: ThemeFileWriteRequest) -> ThemeFileWriteResult:
        if not _safe_target(request.filename):
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="FILE_NOT_ALLOWLISTED")
        if request.confirmed is not True:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="CONFIRMATION_REQUIRED")
        if request.target_is_live and request.allow_live is not True:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="ALLOW_LIVE_REQUIRED")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(request.expected_remote_raw_hash or "")):
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="EXPECTED_REMOTE_HASH_REQUIRED")
        password, capability = self._check_connection(request.store_id, request.shop_domain, request.theme_id)
        if not password:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code=capability.reason_code)
        try:
            before = self.read_file(request.store_id, request.shop_domain, request.theme_id, request.filename)
        except Exception:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="REMOTE_READ_FAILED")
        if before.raw_sha256 != request.expected_remote_raw_hash:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                expected_raw_sha256=request.expected_remote_raw_hash, observed_raw_sha256=before.raw_sha256,
                reason_code="REMOTE_CHANGED_ABORT")
        expected = raw_sha256(request.content)
        pushed = self._push(request, password, request.content)
        try:
            observed = self.read_file(request.store_id, request.shop_domain, request.theme_id, request.filename)
        except Exception:
            return ThemeFileWriteResult("ROLLBACK_REQUIRED" if pushed else "WRITE_ATTEMPTED",
                self.backend_name, request.filename, True, expected_raw_sha256=expected,
                reason_code="POST_WRITE_READ_FAILED")
        if observed.raw_sha256 == expected:
            return ThemeFileWriteResult("REMOTE_JSON_VERIFIED", self.backend_name, request.filename, True,
                                        expected_raw_sha256=expected, observed_raw_sha256=observed.raw_sha256,
                                        reason_code=None if pushed else "CLI_PUSH_EXIT_NONZERO_BUT_CONTENT_VERIFIED")
        if not pushed:
            return ThemeFileWriteResult("WRITE_ATTEMPTED", self.backend_name, request.filename, True,
                expected_raw_sha256=expected, observed_raw_sha256=observed.raw_sha256,
                reason_code="CLI_PUSH_FAILED_CONTENT_NOT_VERIFIED")
        if observed.raw_sha256 == before.raw_sha256:
            return ThemeFileWriteResult("VERIFY_FAILED", self.backend_name, request.filename, True,
                                        expected_raw_sha256=expected, observed_raw_sha256=observed.raw_sha256,
                                        reason_code="REMOTE_CONTENT_UNCHANGED")
        # A different post-write value may be a merchant edit; never overwrite it automatically.
        return ThemeFileWriteResult("ROLLBACK_REQUIRED", self.backend_name, request.filename, True,
                                    expected_raw_sha256=expected, observed_raw_sha256=observed.raw_sha256,
                                    reason_code="VERIFY_FAILED_REVIEW_BEFORE_ROLLBACK")

    def rollback_file(self, request: ThemeFileWriteRequest, *, original_content: str,
                      expected_current_raw_hash: str) -> ThemeFileWriteResult:
        """Restore exact bytes only while the remote file still matches the attempted write."""
        if not _safe_target(request.filename) or request.confirmed is not True:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="ROLLBACK_CONFIRMATION_OR_ALLOWLIST_REQUIRED")
        if request.target_is_live and request.allow_live is not True:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="ALLOW_LIVE_REQUIRED")
        password, capability = self._check_connection(request.store_id, request.shop_domain, request.theme_id)
        if not password:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code=capability.reason_code)
        try:
            current = self.read_file(request.store_id, request.shop_domain, request.theme_id, request.filename)
        except Exception:
            return ThemeFileWriteResult("ROLLBACK_REQUIRED", self.backend_name, request.filename, False,
                                        reason_code="ROLLBACK_READ_FAILED")
        if current.raw_sha256 != expected_current_raw_hash:
            return ThemeFileWriteResult("ROLLBACK_REQUIRED", self.backend_name, request.filename, False,
                observed_raw_sha256=current.raw_sha256, reason_code="REMOTE_CHANGED_ROLLBACK_ABORT")
        if not self._push(request, password, original_content):
            return ThemeFileWriteResult("ROLLBACK_REQUIRED", self.backend_name, request.filename, True,
                                        expected_raw_sha256=raw_sha256(original_content), reason_code="ROLLBACK_PUSH_FAILED")
        expected = raw_sha256(original_content)
        verified = self.verify_file(request.store_id, request.shop_domain, request.theme_id, request.filename, expected)
        if verified:
            return ThemeFileWriteResult("ROLLBACK_VERIFIED", self.backend_name, request.filename, True,
                                        expected_raw_sha256=expected, observed_raw_sha256=verified.raw_sha256)
        return ThemeFileWriteResult("ROLLBACK_REQUIRED", self.backend_name, request.filename, True,
                                    expected_raw_sha256=expected, reason_code="ROLLBACK_VERIFY_FAILED")
