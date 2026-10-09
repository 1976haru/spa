"""Read-only capability checks and bounded Theme Access Shopify CLI transport.

Theme Access is a merchant-controlled operational credential, not a Shopify
security bypass. This module is intentionally not wired to the application UI.
"""
from __future__ import annotations

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


def _numeric_theme_id(value) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    raw = str(value).strip()
    if raw.startswith("gid://shopify/OnlineStoreTheme/"):
        raw = raw.rsplit("/", 1)[-1]
    return raw if re.fullmatch(r"\d+", raw) else None


def _theme_rows(payload) -> list[dict] | None:
    """Read only bounded, documented list wrappers; never recursively search arbitrary data."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("themes"), list):
        return [row for row in payload["themes"] if isinstance(row, dict)]
    for wrapper in ("data", "result"):
        nested = payload.get(wrapper)
        if isinstance(nested, dict) and isinstance(nested.get("themes"), list):
            return [row for row in nested["themes"] if isinstance(row, dict)]
    return None


def _normalize_theme_rows(payload) -> list[dict] | None:
    rows = _theme_rows(payload)
    if rows is None:
        return None
    normalized = []
    role_fields = ("role", "status", "themeRole", "theme_role")
    roles = {"main": "MAIN", "live": "MAIN", "published": "MAIN",
             "unpublished": "UNPUBLISHED", "development": "DEVELOPMENT", "demo": "DEMO"}
    for row in rows[:1000]:
        theme_id = None
        for field in ("id", "theme_id", "themeId"):
            theme_id = _numeric_theme_id(row.get(field))
            if theme_id is not None:
                break
        if theme_id is None:
            continue
        raw_role = next((row.get(field) for field in role_fields
                         if isinstance(row.get(field), str) and row.get(field).strip()), None)
        role = roles.get(str(raw_role or "").strip().casefold(), "unknown")
        name = next((row.get(field) for field in ("name", "theme_name", "themeName")
                     if isinstance(row.get(field), str)), None)
        normalized.append({"id": theme_id, "name": name or "", "role": role})
    return normalized


def _unsupported_id_filter(diagnostic: str) -> bool:
    text = diagnostic.casefold()
    return ("--id" in text and any(term in text for term in
            ("unknown", "unsupported", "unrecognized", "not recognized", "invalid option", "unknown option")))


def _probe_failure_status(diagnostic: str) -> str:
    text = diagnostic.casefold()
    if any(term in text for term in ("not recognized", "unknown command", "unknown option")):
        return "UNSUPPORTED_CLI"
    if any(term in text for term in ("network", "timed out", "dns", "enotfound", "econn")):
        return "NETWORK_ERROR"
    return "AUTH_FAILED"


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
        numeric_id = _numeric_theme_id(theme_id)
        if numeric_id is None:
            return ThemeBackendCapabilities(self.backend_name, "THEME_ID_MISSING", "THEME_ID_MISSING",
                                            {"cli_version": match.group(0), "shop_domain": connection["shop_domain"]})
        base = [executable, "theme", "list", "--store", connection["shop_domain"]]
        env = {**os.environ, "SHOPIFY_CLI_THEME_TOKEN": password}
        try:
            probe = self.runner([*base, "--id", numeric_id, "--json"], capture_output=True,
                                text=True, timeout=30, check=False, env=env)
        except subprocess.TimeoutExpired:
            return ThemeBackendCapabilities(self.backend_name, "NETWORK_ERROR", "NETWORK_ERROR")
        except OSError:
            return ThemeBackendCapabilities(self.backend_name, "CLI_NOT_INSTALLED", "CLI_NOT_INSTALLED")
        if getattr(probe, "returncode", 1) != 0:
            diagnostic = f"{getattr(probe, 'stdout', '')} {getattr(probe, 'stderr', '')}".casefold()
            if _unsupported_id_filter(diagnostic):
                try:
                    probe = self.runner([*base, "--json"], capture_output=True, text=True,
                                        timeout=30, check=False, env=env)
                except subprocess.TimeoutExpired:
                    return ThemeBackendCapabilities(self.backend_name, "NETWORK_ERROR", "NETWORK_ERROR")
                except OSError:
                    return ThemeBackendCapabilities(self.backend_name, "CLI_NOT_INSTALLED", "CLI_NOT_INSTALLED")
            else:
                if "theme" in diagnostic and any(term in diagnostic for term in ("not found", "no theme", "does not exist")):
                    return ThemeBackendCapabilities(self.backend_name, "THEME_ID_NOT_FOUND", "THEME_ID_NOT_FOUND",
                        {"cli_version": match.group(0), "shop_domain": connection["shop_domain"],
                         "verified_theme_id": numeric_id, "identity_verified": False})
                status = _probe_failure_status(diagnostic)
                return ThemeBackendCapabilities(self.backend_name, status, status,
                    {"cli_version": match.group(0), "shop_domain": connection["shop_domain"]})
        if getattr(probe, "returncode", 1) != 0:
            diagnostic = f"{getattr(probe, 'stdout', '')} {getattr(probe, 'stderr', '')}"
            status = _probe_failure_status(diagnostic)
            return ThemeBackendCapabilities(self.backend_name, status, status,
                {"cli_version": match.group(0), "shop_domain": connection["shop_domain"]})
        try:
            payload = json.loads(getattr(probe, "stdout", ""))
        except (TypeError, json.JSONDecodeError):
            return ThemeBackendCapabilities(self.backend_name, "THEME_LIST_INVALID_JSON", "THEME_LIST_INVALID_JSON",
                {"cli_version": match.group(0), "shop_domain": connection["shop_domain"]})
        themes = _normalize_theme_rows(payload)
        if themes is None:
            return ThemeBackendCapabilities(self.backend_name, "THEME_LIST_INVALID_JSON", "THEME_LIST_INVALID_JSON",
                {"cli_version": match.group(0), "shop_domain": connection["shop_domain"]})
        matched = [theme for theme in themes if theme["id"] == numeric_id]
        if not matched:
            return ThemeBackendCapabilities(self.backend_name, "THEME_ID_NOT_FOUND", "THEME_ID_NOT_FOUND",
                {"cli_version": match.group(0), "shop_domain": connection["shop_domain"],
                 "verified_theme_id": numeric_id, "identity_verified": False})
        if len(matched) != 1:
            return ThemeBackendCapabilities(self.backend_name, "THEME_ID_AMBIGUOUS", "THEME_ID_AMBIGUOUS",
                {"cli_version": match.group(0), "shop_domain": connection["shop_domain"],
                 "verified_theme_id": numeric_id, "identity_verified": False})
        target = matched[0]
        if target["role"] == "unknown":
            return ThemeBackendCapabilities(self.backend_name, "THEME_ROLE_UNKNOWN", "THEME_ROLE_UNKNOWN",
                {"cli_version": match.group(0), "shop_domain": connection["shop_domain"],
                 "verified_theme_id": numeric_id, "verified_theme_name": target["name"],
                 "verified_theme_role": "unknown", "identity_verified": True})
        return ThemeBackendCapabilities(self.backend_name, "READY", "READY",
            {"cli_version": match.group(0), "shop_domain": connection["shop_domain"], "theme_id": numeric_id,
             "verified_theme_id": numeric_id, "verified_theme_name": target["name"],
             "verified_theme_role": target["role"], "target_is_live": target["role"] == "MAIN",
             "identity_verified": True, "credential_present": True, "merchant_controlled_auth": True})

    @staticmethod
    def _theme_id(theme_id: str) -> str:
        value = _numeric_theme_id(theme_id)
        if value is None:
            raise ValueError("A valid Shopify theme ID is required")
        return value

    def _check_connection(self, store_id, shop_domain, theme_id):
        capability = self.capability_status(store_id, theme_id=theme_id)
        if not capability.ready:
            return None, capability
        configured = capability.details.get("shop_domain", "").casefold()
        if str(shop_domain or "").casefold() != configured:
            return None, ThemeBackendCapabilities(self.backend_name, "STORE_NOT_CONFIGURED", "STORE_MISMATCH")
        if capability.details.get("identity_verified") is not True or (
                capability.details.get("verified_theme_id") != _numeric_theme_id(theme_id)):
            return None, ThemeBackendCapabilities(self.backend_name, "THEME_ID_NOT_FOUND", "THEME_ID_NOT_FOUND")
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

    def _pull(self, store_id, shop_domain, theme_id, filename, *, capability=None):
        if not _safe_target(filename):
            raise ValueError("Only templates/index.json is allowed in Phase A")
        if capability is None:
            password, capability = self._check_connection(store_id, shop_domain, theme_id)
        else:
            if (capability.ready is not True or capability.details.get("shop_domain", "").casefold()
                    != str(shop_domain or "").casefold()
                    or capability.details.get("verified_theme_id") != _numeric_theme_id(theme_id)
                    or capability.details.get("identity_verified") is not True
                    or capability.details.get("verified_theme_role") not in {"MAIN", "UNPUBLISHED", "DEVELOPMENT", "DEMO"}
                    or capability.details.get("target_is_live") is not
                    (capability.details.get("verified_theme_role") == "MAIN")):
                raise RuntimeError("Verified theme target identity is required")
            try:
                password = self.credential_getter(store_id)
            except Exception:
                password = None
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

    def _push(self, request: ThemeFileWriteRequest, password: str, content: str, *, actual_live: bool):
        temp = Path(tempfile.mkdtemp(prefix="shopsource-theme-push-", dir=self.temp_dir))
        try:
            self._scaffold(temp)
            target = temp / TARGET_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
            command = [self._executable(), "theme", "push", "--store", request.shop_domain,
                       "--theme", self._theme_id(request.theme_id), "--only", TARGET_FILE,
                       "--path", str(temp), "--nodelete", "--strict", "--json"]
            if actual_live:
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
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(request.expected_remote_raw_hash or "")):
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="EXPECTED_REMOTE_HASH_REQUIRED")
        password, capability = self._check_connection(request.store_id, request.shop_domain, request.theme_id)
        if not password:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code=capability.reason_code)
        actual_live = capability.details.get("target_is_live") is True
        if request.target_is_live != actual_live:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="TARGET_ROLE_MISMATCH")
        if actual_live and request.allow_live is not True:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="ALLOW_LIVE_REQUIRED")
        if not actual_live and request.allow_live is True:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="TARGET_ROLE_MISMATCH")
        try:
            before_raw, _metadata = self._pull(request.store_id, request.shop_domain, request.theme_id,
                                               request.filename, capability=capability)
            before = ThemeFileReadResult(request.filename, before_raw, raw_sha256(before_raw), self.backend_name,
                request.store_id, request.shop_domain, capability.details["verified_theme_id"], {})
        except Exception:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="REMOTE_READ_FAILED")
        if before.raw_sha256 != request.expected_remote_raw_hash:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                expected_raw_sha256=request.expected_remote_raw_hash, observed_raw_sha256=before.raw_sha256,
                reason_code="REMOTE_CHANGED_ABORT")
        # Re-probe immediately before pushing; role and identity are backend-derived.
        password, latest = self._check_connection(request.store_id, request.shop_domain, request.theme_id)
        if not password:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code=latest.reason_code)
        latest_live = latest.details.get("target_is_live") is True
        if (latest.details.get("verified_theme_id") != capability.details.get("verified_theme_id")
                or latest_live != actual_live or request.target_is_live != latest_live):
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="TARGET_ROLE_MISMATCH")
        if latest_live and request.allow_live is not True:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="ALLOW_LIVE_REQUIRED")
        expected = raw_sha256(request.content)
        pushed = self._push(request, password, request.content, actual_live=latest_live)
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
        password, capability = self._check_connection(request.store_id, request.shop_domain, request.theme_id)
        if not password:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code=capability.reason_code)
        actual_live = capability.details.get("target_is_live") is True
        if request.target_is_live != actual_live:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="TARGET_ROLE_MISMATCH")
        if actual_live and request.allow_live is not True:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="ALLOW_LIVE_REQUIRED")
        if not actual_live and request.allow_live is True:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="TARGET_ROLE_MISMATCH")
        try:
            current_raw, _metadata = self._pull(request.store_id, request.shop_domain, request.theme_id,
                                                request.filename, capability=capability)
            current = ThemeFileReadResult(request.filename, current_raw, raw_sha256(current_raw), self.backend_name,
                request.store_id, request.shop_domain, capability.details["verified_theme_id"], {})
        except Exception:
            return ThemeFileWriteResult("ROLLBACK_REQUIRED", self.backend_name, request.filename, False,
                                        reason_code="ROLLBACK_READ_FAILED")
        if current.raw_sha256 != expected_current_raw_hash:
            return ThemeFileWriteResult("ROLLBACK_REQUIRED", self.backend_name, request.filename, False,
                observed_raw_sha256=current.raw_sha256, reason_code="REMOTE_CHANGED_ROLLBACK_ABORT")
        password, latest = self._check_connection(request.store_id, request.shop_domain, request.theme_id)
        latest_live = latest.details.get("target_is_live") is True
        if (not password or latest.details.get("verified_theme_id") != capability.details.get("verified_theme_id")
                or latest_live != actual_live or request.target_is_live != latest_live):
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="TARGET_ROLE_MISMATCH" if password else latest.reason_code)
        if latest_live and request.allow_live is not True:
            return ThemeFileWriteResult("PRECONDITION_FAILED", self.backend_name, request.filename, False,
                                        reason_code="ALLOW_LIVE_REQUIRED")
        if not self._push(request, password, original_content, actual_live=latest_live):
            return ThemeFileWriteResult("ROLLBACK_REQUIRED", self.backend_name, request.filename, True,
                                        expected_raw_sha256=raw_sha256(original_content), reason_code="ROLLBACK_PUSH_FAILED")
        expected = raw_sha256(original_content)
        try:
            restored_raw, _metadata = self._pull(request.store_id, request.shop_domain, request.theme_id,
                                                 request.filename)
            verified = raw_sha256(restored_raw) == expected
        except Exception:
            verified = False
        if verified:
            return ThemeFileWriteResult("ROLLBACK_VERIFIED", self.backend_name, request.filename, True,
                                        expected_raw_sha256=expected, observed_raw_sha256=expected)
        return ThemeFileWriteResult("ROLLBACK_REQUIRED", self.backend_name, request.filename, True,
                                    expected_raw_sha256=expected, reason_code="ROLLBACK_VERIFY_FAILED")
