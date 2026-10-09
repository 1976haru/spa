"""Theme Access passwords stored only in the operating system credential store."""
from __future__ import annotations

import os

SERVICE_NAME = "ShopSourceStudio.ShopifyThemeAccess"


def _key(store_id: str) -> str:
    value = str(store_id or "").strip()
    if not value or any(char in value for char in "\r\n\0"):
        raise ValueError("A valid store ID is required")
    return f"{value}:theme-access-password"


def _keyring():
    try:
        import keyring
        if os.name == "nt" and "win" not in type(keyring.get_keyring()).__name__.casefold():
            return None
        return keyring
    except Exception:
        return None


def save_theme_access_password(store_id: str, password: str) -> bool:
    if not isinstance(password, str) or not password.strip():
        raise ValueError("Theme Access password is empty")
    backend = _keyring()
    if backend is None:
        raise RuntimeError("Windows Credential Manager or an approved keyring backend is required")
    try:
        backend.set_password(SERVICE_NAME, _key(store_id), password.strip())
        return True
    except Exception:
        # Suppress provider exceptions because some keyrings include the secret.
        raise RuntimeError("Theme Access password could not be stored securely") from None


def get_theme_access_password(store_id: str) -> str | None:
    backend = _keyring()
    if backend is None:
        return None
    try:
        return backend.get_password(SERVICE_NAME, _key(store_id))
    except Exception:
        return None


def delete_theme_access_password(store_id: str) -> None:
    backend = _keyring()
    if backend is None:
        return
    try:
        backend.delete_password(SERVICE_NAME, _key(store_id))
    except Exception:
        return


def credential_present(store_id: str) -> bool:
    return bool(get_theme_access_password(store_id))
