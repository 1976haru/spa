from __future__ import annotations

import sys

import pytest

from shopsource import theme_access_credentials as credentials


class FakeKeyring:
    values = {}
    @staticmethod
    def get_keyring():
        return type("WindowsCredentialManager", (), {})()
    @classmethod
    def set_password(cls, service, key, value):
        cls.values[(service, key)] = value
    @classmethod
    def get_password(cls, service, key):
        return cls.values.get((service, key))
    @classmethod
    def delete_password(cls, service, key):
        del cls.values[(service, key)]


def test_credentials_keyring_only(monkeypatch):
    FakeKeyring.values = {}
    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring)
    monkeypatch.setattr(credentials.os, "name", "nt", raising=False)
    assert credentials.save_theme_access_password("store-a", "merchant-password") is True
    assert credentials.get_theme_access_password("store-a") == "merchant-password"
    assert credentials.credential_present("store-a") is True
    assert list(FakeKeyring.values) == [(credentials.SERVICE_NAME, "store-a:theme-access-password")]
    credentials.delete_theme_access_password("store-a")
    assert credentials.credential_present("store-a") is False


def test_credentials_never_fall_back_to_database_or_environment(monkeypatch):
    monkeypatch.delenv("SHOPIFY_CLI_THEME_TOKEN", raising=False)
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert credentials.get_theme_access_password("store-a") is None
    assert credentials.credential_present("store-a") is False


def test_credential_provider_error_does_not_expose_secret(monkeypatch):
    class Broken(FakeKeyring):
        @staticmethod
        def set_password(*args):
            raise RuntimeError("merchant-password leaked")
    monkeypatch.setitem(sys.modules, "keyring", Broken)
    monkeypatch.setattr(credentials.os, "name", "nt", raising=False)
    with pytest.raises(RuntimeError) as exc:
        credentials.save_theme_access_password("store-a", "merchant-password")
    assert "merchant-password" not in str(exc.value)
