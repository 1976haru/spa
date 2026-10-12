from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from shopsource.shopify_collections import save_connection
from shopsource.db import connect
from shopsource.shopify_theme_cli import ShopifyThemeCLI, TARGET_FILE
from shopsource.theme_write_backend import (ThemeBackendCapabilities, ThemeFileWriteRequest,
                                            ThemeWriteBackendRouter, raw_sha256)


class Runner:
    def __init__(self, root: Path, *, remote=None, version="3.80.0", role="unpublished", themes=None,
                 reject_pull_json=False, emulate_pull_directory_gate=False,
                 emulate_push_directory_gate=False):
        self.root = root
        self.remote = remote or "/* Shopify header comment */\n{\"sections\":{},\"order\":[]}\n"
        self.version = version
        self.calls = []
        self.fail_push = False
        self.themes = themes if themes is not None else [{"id": "123", "name": "Mock theme", "role": role}]
        self.theme_list_json_override = None
        self.unsupported_id_filter = False
        self.reject_pull_json = reject_pull_json
        self.emulate_pull_directory_gate = emulate_pull_directory_gate
        self.emulate_push_directory_gate = emulate_push_directory_gate
        self.pull_path_entries = []
        self.pull_scaffold_exists = []
        self.push_path_entries = []
        self.push_scaffold_exists = []
        self.push_output_override = None

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        if command[1:] == ["version"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{self.version}\n", stderr="")
        if command[1:3] == ["theme", "list"]:
            if "--id" in command and self.unsupported_id_filter:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="unknown option --id")
            if self.theme_list_json_override is not None:
                payload = self.theme_list_json_override
            elif "--id" in command:
                requested_id = command[command.index("--id") + 1]
                payload = [theme for theme in self.themes if str(theme.get("id")) == requested_id]
            else:
                payload = self.themes
            output = payload if isinstance(payload, str) else json.dumps(payload)
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
        if "theme" not in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        path = Path(command[command.index("--path") + 1])
        file_path = path / TARGET_FILE
        if command[command.index("theme") + 1] == "pull":
            entries = tuple(sorted(item.name for item in path.iterdir()))
            self.pull_path_entries.append(entries)
            self.pull_scaffold_exists.append((path / "config" / "settings_schema.json").exists())
            if self.reject_pull_json and "--json" in command:
                return subprocess.CompletedProcess(command, 1, stdout="unsupported --json", stderr="unsupported --json")
            if self.emulate_pull_directory_gate and entries and not {"config", "layout", "templates"}.issubset(entries):
                return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(self.remote.encode("utf-8"))
            return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")
        self.push_path_entries.append(tuple(sorted(item.name for item in path.iterdir())))
        self.push_scaffold_exists.append((path / "config" / "settings_schema.json").is_file())
        if (self.emulate_push_directory_gate
                and not {"config", "layout", "templates"}.issubset(self.push_path_entries[-1])):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="invalid theme directory")
        if self.fail_push:
            return subprocess.CompletedProcess(command, 1, stdout="merchant-password", stderr="merchant-password")
        if self.push_output_override is not None:
            return subprocess.CompletedProcess(command, 0, stdout=self.push_output_override, stderr="")
        self.remote = file_path.read_bytes().decode("utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")


def setup(tmp_path, monkeypatch, *, runner=None, password="merchant-password", role="unpublished"):
    db = tmp_path / "test.sqlite3"
    save_connection("store-a", "example.myshopify.com", db=db)
    root = tmp_path / "cli-tmp"
    root.mkdir()
    runner = runner or Runner(root, role=role)
    cli = ShopifyThemeCLI(db=db, runner=runner, which=lambda _name: "shopify.exe", temp_dir=root,
                          credential_getter=lambda _store: password)
    return cli, runner, db, root


def set_canonical_domain(db, domain):
    with connect(db) as con:
        con.execute("UPDATE shopify_connections SET canonical_myshopify_domain=? WHERE store_id='store-a'", (domain,))


def test_cli_backend_password_only_in_env(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    result = cli.read_file("store-a", "example.myshopify.com", "123", TARGET_FILE)
    command, kwargs = next((c, k) for c, k in runner.calls if c[1:3] == ["theme", "pull"])
    assert kwargs["env"]["SHOPIFY_CLI_THEME_TOKEN"] == "merchant-password"
    assert "merchant-password" not in " ".join(command)
    assert result.backend_name == "THEME_ACCESS_CLI"


def test_cli_backend_secret_not_in_logs(tmp_path, monkeypatch, caplog):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        cli.read_file("store-a", "example.myshopify.com", "123", TARGET_FILE)
    assert all("merchant-password" not in " ".join(command) for command, _ in runner.calls)
    assert "merchant-password" not in caplog.text


def test_cli_prefers_canonical_myshopify_domain(tmp_path, monkeypatch):
    cli, runner, db, _ = setup(tmp_path, monkeypatch)
    set_canonical_domain(db, " KGXBPI-IT.MYSHOPIFY.COM ")
    capability = cli.capability_status("store-a", theme_id="123")
    command, _ = next((c, k) for c, k in runner.calls if c[1:3] == ["theme", "list"])
    assert command[command.index("--store") + 1] == "kgxbpi-it.myshopify.com"
    assert capability.ready
    assert capability.details["shop_domain"] == "example.myshopify.com"
    assert capability.details["configured_shop_domain"] == "example.myshopify.com"
    assert capability.details["cli_store_domain"] == "kgxbpi-it.myshopify.com"
    assert capability.details["cli_store_domain_source"] == "CANONICAL_MYSHOPIFY_DOMAIN"


def test_cli_falls_back_to_configured_domain_when_canonical_missing(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    capability = cli.capability_status("store-a", theme_id="123")
    command, _ = next((c, k) for c, k in runner.calls if c[1:3] == ["theme", "list"])
    assert command[command.index("--store") + 1] == "example.myshopify.com"
    assert capability.details["cli_store_domain_source"] == "CONFIGURED_SHOP_DOMAIN_FALLBACK"


def test_cli_list_uses_canonical_domain_for_renamed_store(tmp_path, monkeypatch):
    cli, runner, db, _ = setup(tmp_path, monkeypatch)
    set_canonical_domain(db, "kgxbpi-it.myshopify.com")
    capability = cli.capability_status("store-a", theme_id="123")
    list_commands = [command for command, _ in runner.calls if command[1:3] == ["theme", "list"]]
    assert capability.ready and list_commands
    assert all(command[command.index("--store") + 1] == "kgxbpi-it.myshopify.com" for command in list_commands)


def test_pull_uses_same_canonical_domain_as_capability(tmp_path, monkeypatch):
    cli, runner, db, _ = setup(tmp_path, monkeypatch)
    set_canonical_domain(db, "kgxbpi-it.myshopify.com")
    result = cli.read_file("store-a", "example.myshopify.com", "123", TARGET_FILE)
    capability = cli.capability_status("store-a", theme_id="123")
    pull_commands = [command for command, _ in runner.calls if command[1:3] == ["theme", "pull"]]
    assert result.backend_name == "THEME_ACCESS_CLI"
    assert capability.details["cli_store_domain"] == "kgxbpi-it.myshopify.com"
    assert pull_commands and all(command[command.index("--store") + 1] == capability.details["cli_store_domain"]
                                 for command in pull_commands)


def test_push_uses_same_canonical_domain_as_capability(tmp_path, monkeypatch):
    cli, runner, db, _ = setup(tmp_path, monkeypatch)
    set_canonical_domain(db, "kgxbpi-it.myshopify.com")
    before = runner.remote
    request = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
                                    "{}", raw_sha256(before), confirmed=True)
    result = cli.write_file(request)
    capability = cli.capability_status("store-a", theme_id="123")
    remote_commands = [command for command, _ in runner.calls
                       if len(command) > 2 and command[1:3] in (["theme", "list"], ["theme", "pull"], ["theme", "push"])]
    assert result.status == "REMOTE_JSON_VERIFIED"
    assert remote_commands and all(command[command.index("--store") + 1] == capability.details["cli_store_domain"]
                                   for command in remote_commands)


def test_request_configured_domain_can_bind_to_same_store_canonical_cli_domain(tmp_path, monkeypatch):
    cli, _, db, _ = setup(tmp_path, monkeypatch)
    set_canonical_domain(db, "kgxbpi-it.myshopify.com")
    password, capability = cli._check_connection("store-a", "example.myshopify.com", "123")
    assert password == "merchant-password"
    assert capability.ready
    assert capability.details["configured_shop_domain"] == "example.myshopify.com"
    assert capability.details["cli_store_domain"] == "kgxbpi-it.myshopify.com"


def test_malformed_canonical_domain_fails_closed(tmp_path, monkeypatch):
    cli, runner, db, _ = setup(tmp_path, monkeypatch)
    set_canonical_domain(db, "https://wrong.example/path")
    capability = cli.capability_status("store-a", theme_id="123")
    assert capability.status == "DOMAIN_INVALID"
    assert not any(command[1:3] == ["theme", "list"] for command, _ in runner.calls)


def test_store_id_isolation_unchanged(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    password, capability = cli._check_connection("store-a", "another.myshopify.com", "123")
    assert password is None and capability.reason_code == "STORE_MISMATCH"
    assert not any(command[1:3] == ["theme", "pull"] for command, _ in runner.calls)


def test_no_secret_in_capability_details(tmp_path, monkeypatch):
    cli, _, db, _ = setup(tmp_path, monkeypatch)
    set_canonical_domain(db, "kgxbpi-it.myshopify.com")
    capability = cli.capability_status("store-a", theme_id="123")
    assert "merchant-password" not in repr(capability.details)
    assert capability.details["cli_store_domain"] == "kgxbpi-it.myshopify.com"


def test_cli_read_uses_only_target_file(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    result = cli.read_file("store-a", "example.myshopify.com", "123", TARGET_FILE)
    command, _ = next((c, k) for c, k in runner.calls if c[1:3] == ["theme", "pull"])
    only_index = command.index("--only")
    assert command[only_index + 1] == "templates/index.json"
    assert command[1:3] == ["theme", "pull"]
    assert command[command.index("--store") + 1] == "example.myshopify.com"
    assert command[command.index("--theme") + 1] == "123"
    assert command[command.index("--path") + 1]
    assert "--json" not in command
    assert "--no-input" in command
    assert not any(command[1:3] == ["theme", "list"] for command, _ in runner.calls)
    assert result.metadata["capability_state"] == "READ_ONLY_READY"
    assert result.metadata["live_role_verified"] is False


def test_push_json_theme_errors_are_not_treated_as_success(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    runner.push_output_override = json.dumps({"theme": {"errors": [{"asset": "templates/index.json", "code": "invalid_json"}]}})
    before = runner.remote
    request = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        "{\"sections\":{},\"order\":[]}", raw_sha256(before), confirmed=True)
    result = cli.write_file(request)
    assert result.status == "VERIFY_FAILED"
    assert result.reason_code == "CLI_PUSH_ASSET_REJECTED"
    assert result.metadata["error_count"] == 1
    assert result.metadata["affected_asset_names"] == [TARGET_FILE]


def test_push_json_invalid_output_is_sanitized_and_postread_wins(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    runner.push_output_override = "not json merchant-password"
    before = runner.remote
    request = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        "{\"sections\":{},\"order\":[\"changed\"]}", raw_sha256(before), confirmed=True)
    result = cli.write_file(request)
    assert result.reason_code == "POST_WRITE_NOT_APPLIED"
    assert "merchant-password" not in repr(result)


def test_capability_ready_pull_without_json_flag_preserves_file_bytes(tmp_path, monkeypatch):
    payload = "/* Shopify header */\r\n{\"sections\":{\"featured\":{}},\"order\":[\"featured\"]}\r\n"
    runner = Runner(tmp_path, remote=payload, reject_pull_json=True)
    cli, runner, _, _ = setup(tmp_path, monkeypatch, runner=runner)

    capability = cli.capability_status("store-a", theme_id="123")
    assert capability.ready
    result = cli.read_file("store-a", "example.myshopify.com", "123", TARGET_FILE)

    command, kwargs = next((command, kwargs) for command, kwargs in runner.calls
                           if command[1:3] == ["theme", "pull"])
    assert result.content == payload
    assert result.raw_sha256 == raw_sha256(payload)
    assert result.metadata["read_only"] is True
    assert command[command.index("--store") + 1] == "example.myshopify.com"
    assert command[command.index("--theme") + 1] == "123"
    assert command[command.index("--only") + 1] == TARGET_FILE
    assert "--path" in command and "--no-input" in command
    assert "--json" not in command
    assert kwargs["env"]["SHOPIFY_CLI_THEME_TOKEN"] == "merchant-password"
    assert "merchant-password" not in command


def test_pull_starts_with_empty_temp_and_passes_mock_shopify_directory_gate(tmp_path, monkeypatch):
    runner = Runner(tmp_path, emulate_pull_directory_gate=True)
    cli, runner, _, _ = setup(tmp_path, monkeypatch, runner=runner)

    result = cli.read_file("store-a", "example.myshopify.com", "123", TARGET_FILE)

    command = next(command for command, _ in runner.calls if command[1:3] == ["theme", "pull"])
    assert result.content == runner.remote
    assert runner.pull_path_entries == [()]
    assert runner.pull_scaffold_exists == [False]
    assert "--no-input" in command and "--json" not in command and "--force" not in command


def test_cli_read_uses_isolated_temp_theme(tmp_path, monkeypatch):
    source_repo = tmp_path / "repo"
    source_repo.mkdir()
    cli, runner, _, root = setup(tmp_path, monkeypatch)
    cli.read_file("store-a", "example.myshopify.com", "123", TARGET_FILE)
    command, _ = next((c, k) for c, k in runner.calls if c[1:3] == ["theme", "pull"])
    isolated = Path(command[command.index("--path") + 1])
    assert isolated != source_repo and isolated.parent == root
    assert not isolated.exists()


@pytest.mark.parametrize("request_overrides,reason", [
    ({"target_is_live": True}, "TARGET_ROLE_MISMATCH"),
    ({"confirmed": False}, "CONFIRMATION_REQUIRED"),
    ({"expected_remote_raw_hash": ""}, "EXPECTED_REMOTE_HASH_REQUIRED"),
])
def test_cli_write_default_refuses_live(tmp_path, monkeypatch, request_overrides, reason):
    cli, _, _, _ = setup(tmp_path, monkeypatch)
    values = {"content": "{}", "expected_remote_raw_hash": "0" * 64,
              "confirmed": True, **request_overrides}
    request = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE, **values)
    result = cli.write_file(request)
    assert result.status == "PRECONDITION_FAILED" and result.reason_code == reason
    assert result.write_performed is False


def test_cli_write_requires_confirmed(tmp_path, monkeypatch):
    cli, _, _, _ = setup(tmp_path, monkeypatch)
    request = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
                                    "{}", "hash", confirmed=False)
    assert cli.write_file(request).reason_code == "CONFIRMATION_REQUIRED"


@pytest.mark.parametrize("filename", ["sections/main.liquid", "snippets/x.liquid", "assets/x.css",
                                       "config/settings_data.json", "layout/theme.liquid", "../templates/index.json"])
def test_cli_write_allows_only_index_json(tmp_path, monkeypatch, filename):
    cli, _, _, _ = setup(tmp_path, monkeypatch)
    request = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", filename,
                                    "{}", "hash", confirmed=True)
    assert cli.write_file(request).reason_code == "FILE_NOT_ALLOWLISTED"


def test_cli_write_uses_nodelete_strict_json(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    before = runner.remote
    request = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        "/* Shopify header comment */\n{\"sections\":{\"x\":{}},\"order\":[\"x\"]}\n",
        raw_sha256(before), confirmed=True)
    result = cli.write_file(request)
    command, _ = next((c, k) for c, k in runner.calls if len(c) > 2 and c[1:3] == ["theme", "push"])
    assert result.status == "REMOTE_JSON_VERIFIED"
    assert {"--nodelete", "--strict", "--json", "--only"}.issubset(set(command))
    assert runner.push_path_entries
    assert {"config", "layout", "templates"}.issubset(set(runner.push_path_entries[-1]))
    assert runner.push_scaffold_exists[-1]
    pull_commands = [c for c, _ in runner.calls if len(c) > 2 and c[1:3] == ["theme", "pull"]]
    assert pull_commands and all("--json" not in c and "--no-input" in c for c in pull_commands)
    assert runner.remote.startswith("/* Shopify header comment */")


def test_push_scaffold_satisfies_shopify_required_theme_directories(tmp_path, monkeypatch):
    runner = Runner(tmp_path, emulate_push_directory_gate=True)
    cli, runner, _, _ = setup(tmp_path, monkeypatch, runner=runner)
    before = runner.remote

    result = cli.write_file(ThemeFileWriteRequest(
        "store-a", "example.myshopify.com", "123", TARGET_FILE, "{}",
        raw_sha256(before), confirmed=True))

    assert result.status == "REMOTE_JSON_VERIFIED"
    assert runner.push_path_entries == [("config", "layout", "templates")]
    assert runner.push_scaffold_exists == [True]
    assert runner.pull_path_entries == [(), ()]
    assert runner.pull_scaffold_exists == [False, False]
    push = next(command for command, _ in runner.calls if command[1:3] == ["theme", "push"])
    assert {"--json", "--nodelete", "--strict"}.issubset(push)
    assert "--force" not in push


def test_cli_live_requires_allow_live(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch, role="main")
    before = runner.remote
    blocked = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
                                    "{}", raw_sha256(before), confirmed=True, target_is_live=True)
    assert cli.write_file(blocked).reason_code == "ALLOW_LIVE_REQUIRED"
    allowed = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        "{}", raw_sha256(before), confirmed=True, allow_live=True, target_is_live=True)
    assert cli.write_file(allowed).status == "REMOTE_JSON_VERIFIED"
    command, _ = next((c, k) for c, k in runner.calls if len(c) > 2 and c[1:3] == ["theme", "push"])
    assert "--allow-live" in command


def test_cli_precondition_remote_changed_aborts(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    request = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
                                    "{}", "0" * 64, confirmed=True)
    result = cli.write_file(request)
    assert result.status == "PRECONDITION_FAILED" and result.reason_code == "REMOTE_CHANGED_ABORT"
    assert not any(command[1:3] == ["theme", "push"] for command, _ in runner.calls)


def test_cli_verify_refetch(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    content = "{\"sections\":{},\"order\":[]}"
    result = cli.write_file(ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        content, raw_sha256(runner.remote), confirmed=True))
    assert result.status == "REMOTE_JSON_VERIFIED"
    assert sum(1 for command, _ in runner.calls if command[1:3] == ["theme", "pull"]) == 2


def test_cli_comment_prefix_exact_preservation(tmp_path, monkeypatch):
    comment_raw = "/* Shopify comment with braces { } */\n{\"sections\":{},\"order\":[]}\n"
    runner = Runner(tmp_path, remote=comment_raw)
    cli, runner, _, _ = setup(tmp_path, monkeypatch, runner=runner)
    assert cli.read_file("store-a", "example.myshopify.com", "123", TARGET_FILE).content == comment_raw
    assert raw_sha256(comment_raw) == raw_sha256(runner.remote)


def test_cli_temp_cleanup(tmp_path, monkeypatch):
    cli, _, _, root = setup(tmp_path, monkeypatch)
    cli.read_file("store-a", "example.myshopify.com", "123", TARGET_FILE)
    cli.write_file(ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        "{}", raw_sha256("/* Shopify header comment */\n{\"sections\":{},\"order\":[]}\n"), confirmed=True))
    assert list(root.iterdir()) == []


def test_cli_write_verify_failure_requires_reviewed_rollback(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    runner.fail_push = True
    before = runner.remote
    result = cli.write_file(ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        "{}", raw_sha256(before), confirmed=True))
    assert result.status == "VERIFY_FAILED" and result.write_performed is True
    assert result.reason_code == "POST_WRITE_NOT_APPLIED"


def test_cli_rollback_restores_exact_raw_only_after_current_hash_check(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    original = runner.remote
    proposed = "{\"sections\":{\"new\":{}},\"order\":[\"new\"]}"
    request = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        proposed, raw_sha256(original), confirmed=True)
    assert cli.write_file(request).status == "REMOTE_JSON_VERIFIED"
    result = cli.rollback_file(request, original_content=original,
                               expected_current_raw_hash=raw_sha256(proposed))
    assert result.status == "ROLLBACK_VERIFIED"
    assert runner.remote == original


def test_capability_requires_exact_theme_id_match(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    runner.themes = [{"id": "1234", "name": "Lookalike", "role": "unpublished"}]
    assert cli.capability_status("store-a", theme_id="123").status == "THEME_ID_NOT_FOUND"


def test_capability_theme_id_not_found(tmp_path, monkeypatch):
    cli, _, _, _ = setup(tmp_path, monkeypatch)
    assert cli.capability_status("store-a", theme_id="999").reason_code == "THEME_ID_NOT_FOUND"


def test_capability_rejects_ambiguous_theme_id(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    runner.themes = [{"id": "123", "name": "One", "role": "main"},
                     {"id": 123, "name": "Two", "role": "unpublished"}]
    assert cli.capability_status("store-a", theme_id="123").status == "THEME_ID_AMBIGUOUS"


def test_capability_parses_top_level_theme_list_json(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    runner.theme_list_json_override = '[{"id":123,"name":"Top level","status":"live"}]'
    capability = cli.capability_status("store-a", theme_id="123")
    assert capability.ready and capability.details["verified_theme_name"] == "Top level"


def test_capability_parses_wrapped_themes_json(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    runner.theme_list_json_override = {"data": {"themes": [
        {"theme_id": "gid://shopify/OnlineStoreTheme/123", "themeName": "Wrapped", "themeRole": "development"}]}}
    capability = cli.capability_status("store-a", theme_id="123")
    assert capability.ready and capability.details["verified_theme_role"] == "DEVELOPMENT"


def test_capability_rejects_invalid_json(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    runner.theme_list_json_override = "not json"
    assert cli.capability_status("store-a", theme_id="123").status == "THEME_LIST_INVALID_JSON"


def test_capability_requires_known_theme_role(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    runner.themes = [{"id": "123", "name": "Roleless"}]
    assert cli.capability_status("store-a", theme_id="123").status == "THEME_ROLE_UNKNOWN"


def test_capability_marks_main_theme_live(tmp_path, monkeypatch):
    cli, _, _, _ = setup(tmp_path, monkeypatch, role="published")
    details = cli.capability_status("store-a", theme_id="123").details
    assert details["verified_theme_role"] == "MAIN"
    assert details["target_is_live"] is True and details["identity_verified"] is True


def test_capability_marks_unpublished_theme_not_live(tmp_path, monkeypatch):
    cli, _, _, _ = setup(tmp_path, monkeypatch, role="unpublished")
    details = cli.capability_status("store-a", theme_id="123").details
    assert details["verified_theme_role"] == "UNPUBLISHED"
    assert details["target_is_live"] is False


def test_write_live_role_cannot_be_bypassed_by_false_request_flag(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch, role="main")
    result = cli.write_file(ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        "{}", raw_sha256(runner.remote), confirmed=True, allow_live=True, target_is_live=False))
    assert result.reason_code == "TARGET_ROLE_MISMATCH"
    assert not any(command[1:3] == ["theme", "push"] for command, _ in runner.calls)


def test_write_nonlive_role_rejects_true_request_flag(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch, role="development")
    result = cli.write_file(ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        "{}", raw_sha256(runner.remote), confirmed=True, allow_live=True, target_is_live=True))
    assert result.reason_code == "TARGET_ROLE_MISMATCH"
    assert not any(command[1:3] == ["theme", "push"] for command, _ in runner.calls)


def test_write_main_requires_allow_live(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch, role="main")
    result = cli.write_file(ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        "{}", raw_sha256(runner.remote), confirmed=True, target_is_live=True))
    assert result.reason_code == "ALLOW_LIVE_REQUIRED"
    assert not any(command[1:3] == ["theme", "push"] for command, _ in runner.calls)


def test_write_main_adds_allow_live_only_after_verified_role(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch, role="main")
    request = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        "{}", raw_sha256(runner.remote), confirmed=True, allow_live=True, target_is_live=True)
    assert cli.write_file(request).status == "REMOTE_JSON_VERIFIED"
    command, _ = next((command, kwargs) for command, kwargs in runner.calls if command[1:3] == ["theme", "push"])
    assert "--allow-live" in command


def test_rollback_main_requires_verified_live_gate(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch, role="main")
    request = ThemeFileWriteRequest("store-a", "example.myshopify.com", "123", TARGET_FILE,
        "{}", raw_sha256(runner.remote), confirmed=True, target_is_live=True)
    result = cli.rollback_file(request, original_content="{}", expected_current_raw_hash="0" * 64)
    assert result.reason_code == "ALLOW_LIVE_REQUIRED"
    assert not any(command[1:3] == ["theme", "push"] for command, _ in runner.calls)


def test_secret_not_leaked_in_theme_list_parse_errors(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    runner.theme_list_json_override = "merchant-password { malformed json"
    capability = cli.capability_status("store-a", theme_id="123")
    assert capability.status == "THEME_LIST_INVALID_JSON"
    assert "merchant-password" not in repr(capability.details)


def test_wrong_theme_id_never_pushes(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    request = ThemeFileWriteRequest("store-a", "example.myshopify.com", "999", TARGET_FILE,
        "{}", raw_sha256(runner.remote), confirmed=True)
    result = cli.write_file(request)
    assert result.reason_code == "THEME_ID_NOT_FOUND"
    assert not any(command[1:3] == ["theme", "push"] for command, _ in runner.calls)


def test_cli_id_filter_fallback_uses_exact_local_filter(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    runner.unsupported_id_filter = True
    capability = cli.capability_status("store-a", theme_id="123")
    assert capability.ready is True
    assert any("--id" not in command for command, _ in runner.calls if command[1:3] == ["theme", "list"])


def test_cli_probes_decode_utf8_and_pull_push_capture_binary(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    runner.themes = [{"id": "123", "name": "主題 🏷️ Collection", "role": "main"}]
    runner.unsupported_id_filter = True
    capability = cli.capability_status("store-a", theme_id="123")
    assert capability.ready and capability.details["verified_theme_name"] == "主題 🏷️ Collection"
    version_call = next(kwargs for command, kwargs in runner.calls if command[1:] == ["version"])
    theme_list_calls = [kwargs for command, kwargs in runner.calls if command[1:3] == ["theme", "list"]]
    assert version_call["encoding"] == "utf-8" and version_call["errors"] == "replace"
    assert len(theme_list_calls) == 2
    assert all(call["encoding"] == "utf-8" and call["errors"] == "replace" for call in theme_list_calls)
    cli.read_file("store-a", "example.myshopify.com", "123", TARGET_FILE)
    invoke_calls = [(command, kwargs) for command, kwargs in runner.calls
                    if command[1:3] in (["theme", "pull"], ["theme", "push"])]
    assert invoke_calls and all(kwargs["capture_output"] is True and kwargs["text"] is False
                                for _, kwargs in invoke_calls)


def test_router_cli_not_ready_when_theme_identity_unverified(tmp_path, monkeypatch):
    cli, _, _, _ = setup(tmp_path, monkeypatch)
    graph = type("NoGraphQL", (), {"backend_name": "ADMIN_GRAPHQL",
        "capability_status": lambda self, store_id, *, theme_id=None:
        ThemeBackendCapabilities("ADMIN_GRAPHQL", "UNAVAILABLE", "SCOPE_MISSING")})()
    selected = ThemeWriteBackendRouter(graph, cli).select("store-a", theme_id="999")
    assert selected["backend_name"] is None
    assert selected["capabilities"]["THEME_ACCESS_CLI"].reason_code == "THEME_ID_NOT_FOUND"


@pytest.mark.parametrize("diagnostic,expected", [
    ("unauthorized access token", "AUTH_FAILED"),
    ("network connection timed out", "NETWORK_ERROR"),
    ("unknown command theme list", "UNSUPPORTED_CLI"),
])
def test_cli_capability_reports_probe_failures(tmp_path, monkeypatch, diagnostic, expected):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    original_runner = runner.__call__
    def failing(command, **kwargs):
        if command[1:3] == ["theme", "list"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr=diagnostic)
        return original_runner(command, **kwargs)
    runner.__call__ = failing
    # Special methods are looked up on the class, so use a runner wrapper.
    cli.runner = failing
    status = cli.capability_status("store-a", theme_id="123")
    assert status.status == expected


def test_cli_capability_missing_executable_and_credential(tmp_path, monkeypatch):
    cli, _, _, _ = setup(tmp_path, monkeypatch, password=None)
    cli.which = lambda _name: None
    assert cli.capability_status("store-a", theme_id="1").status == "CLI_NOT_INSTALLED"
    cli.which = lambda _name: "shopify.exe"
    assert cli.capability_status("store-a", theme_id="1").status == "CREDENTIAL_MISSING"
