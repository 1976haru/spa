from __future__ import annotations

SERVICE_NAME = "ShopSourceStudio"
USERNAME = "keepa-api-key"


def get_api_key(session_key: str | None = None) -> tuple[str | None, str]:
    import os
    if os.environ.get("KEEPA_API_KEY"):
        return os.environ["KEEPA_API_KEY"], "environment"
    try:
        import keyring
        value = keyring.get_password(SERVICE_NAME, USERNAME)
        if value:
            return value, "windows-credential-manager"
    except Exception:
        pass
    return session_key, "session" if session_key else "missing"


def save_api_key(key: str) -> None:
    if not key.strip():
        raise ValueError("Keepa API key is empty")
    try:
        import keyring
        import os
        if os.name == "nt" and "win" not in type(keyring.get_keyring()).__name__.lower():
            raise RuntimeError("Windows Credential Manager backend is unavailable; use KEEPA_API_KEY.")
        keyring.set_password(SERVICE_NAME, USERNAME, key.strip())
    except RuntimeError:
        raise
    except ImportError as exc:
        raise RuntimeError("keyring is not installed. Use KEEPA_API_KEY instead.") from exc


def delete_api_key() -> None:
    try:
        import keyring
        keyring.delete_password(SERVICE_NAME, USERNAME)
    except ImportError as exc:
        raise RuntimeError("keyring is not installed.") from exc
    except Exception:
        return
