from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from shopsource.shopify_collections import save_connection
from shopsource.shopify_theme_cli import ShopifyThemeCLI, TARGET_FILE
from shopsource.theme_write_backend import ThemeFileWriteRequest, raw_sha256


class Runner:
    def __init__(self, root: Path, *, remote=None, version="3.80.0"):
        self.root = root
        self.remote = remote or "/* Shopify header comment */\n{\"sections\":{},\"order\":[]}\n"
        self.version = version
        self.calls = []
        self.fail_push = False

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        if command[1:] == ["version"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{self.version}\n", stderr="")
        if command[1:3] == ["theme", "list"]:
            return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")
        if "theme" not in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        path = Path(command[command.index("--path") + 1])
        file_path = path / TARGET_FILE
        if command[command.index("theme") + 1] == "pull":
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(self.remote.encode("utf-8"))
            return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")
        if self.fail_push:
            return subprocess.CompletedProcess(command, 1, stdout="merchant-password", stderr="merchant-password")
        self.remote = file_path.read_bytes().decode("utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")


def setup(tmp_path, monkeypatch, *, runner=None, password="merchant-password"):
    db = tmp_path / "test.sqlite3"
    save_connection("store-a", "example.myshopify.com", db=db)
    root = tmp_path / "cli-tmp"
    root.mkdir()
    runner = runner or Runner(root)
    cli = ShopifyThemeCLI(db=db, runner=runner, which=lambda _name: "shopify.exe", temp_dir=root,
                          credential_getter=lambda _store: password)
    return cli, runner, db, root


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


def test_cli_read_uses_only_target_file(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
    cli.read_file("store-a", "example.myshopify.com", "123", TARGET_FILE)
    command, _ = next((c, k) for c, k in runner.calls if c[1:3] == ["theme", "pull"])
    only_index = command.index("--only")
    assert command[only_index + 1] == "templates/index.json"
    assert command[1:3] == ["theme", "pull"]


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
    ({"target_is_live": True}, "ALLOW_LIVE_REQUIRED"),
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
    assert runner.remote.startswith("/* Shopify header comment */")


def test_cli_live_requires_allow_live(tmp_path, monkeypatch):
    cli, runner, _, _ = setup(tmp_path, monkeypatch)
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
    assert result.status == "WRITE_ATTEMPTED" and result.write_performed is True
    assert "merchant-password" not in result.reason_code


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
