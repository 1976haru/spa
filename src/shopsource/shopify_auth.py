"""Central, secret-safe Shopify authentication for local ShopSource clients."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .db import connect

DEV_DASHBOARD_CLIENT_CREDENTIALS = "DEV_DASHBOARD_CLIENT_CREDENTIALS"
LEGACY_ADMIN_TOKEN = "LEGACY_ADMIN_TOKEN"
AUTH_MODES = {DEV_DASHBOARD_CLIENT_CREDENTIALS, LEGACY_ADMIN_TOKEN}
KEYRING_SERVICE = "ShopSourceStudio.Shopify"
_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.RLock()


class ShopifyAuthError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _keyring():
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("keyring optional dependency is required for Shopify credentials") from exc
    if os.name == "nt" and "win" not in type(keyring.get_keyring()).__name__.lower():
        raise RuntimeError("Windows Credential Manager backend unavailable")
    return keyring


def _credential_key(store_id: str, kind: str) -> str:
    return f"{store_id}:{kind}"


def save_dev_credentials(store_id: str, client_id: str, client_secret: str) -> None:
    client_id, client_secret = str(client_id or "").strip(), str(client_secret or "").strip()
    if not client_id or not client_secret:
        raise ShopifyAuthError("MISSING_CREDENTIALS", "Shopify Dev Dashboard의 Client ID와 Client Secret을 입력해야 합니다.")
    bundle = json.dumps({"client_id": client_id, "client_secret": client_secret}, separators=(",", ":"))
    _keyring().set_password(KEYRING_SERVICE, _credential_key(store_id, "dev-dashboard-client"), bundle)
    with _CACHE_LOCK:
        _CACHE.pop(str(store_id), None)


def get_dev_credentials(store_id: str) -> dict | None:
    try:
        raw = _keyring().get_password(KEYRING_SERVICE, _credential_key(store_id, "dev-dashboard-client"))
        value = json.loads(raw) if raw else None
        if value and value.get("client_id") and value.get("client_secret"):
            return {"client_id": value["client_id"], "client_secret": value["client_secret"]}
    except Exception:
        return None
    return None


def delete_dev_credentials(store_id: str) -> None:
    try:
        _keyring().delete_password(KEYRING_SERVICE, _credential_key(store_id, "dev-dashboard-client"))
    except Exception:
        pass
    with _CACHE_LOCK:
        _CACHE.pop(str(store_id), None)


def _connection(db, store_id):
    from .shopify_collections import _install_schema
    _install_schema(db)
    with connect(db) as con:
        row = con.execute("SELECT * FROM shopify_connections WHERE store_id=?", (store_id,)).fetchone()
    return dict(row) if row else None


def _set_expiry(db, store_id, expires_at, scope):
    from .shopify_collections import _install_schema
    _install_schema(db)
    with connect(db) as con:
        scopes=sorted({part.strip() for part in str(scope or "").replace(","," ").split() if part.strip()})
        con.execute("UPDATE shopify_connections SET token_expires_at=?,scopes_json=CASE WHEN ? THEN ? ELSE scopes_json END,updated_at=? WHERE store_id=?",
                    (expires_at,int(bool(scopes)),json.dumps(scopes),datetime.now(timezone.utc).isoformat(timespec="seconds"),store_id))


def credential_present(store_id: str, *, db=None, auth_mode: str | None = None) -> bool:
    mode = auth_mode or ((_connection(db, store_id) or {}).get("auth_mode") or LEGACY_ADMIN_TOKEN)
    if mode == DEV_DASHBOARD_CLIENT_CREDENTIALS:
        return get_dev_credentials(store_id) is not None
    try:
        import keyring
        return bool(keyring.get_password(KEYRING_SERVICE, _credential_key(store_id, "admin-access-token")) or
                    os.environ.get("SHOPIFY_ACCESS_TOKEN"))
    except Exception:
        return bool(os.environ.get("SHOPIFY_ACCESS_TOKEN"))


class ShopifyAccessToken(str):
    """A token string with a private one-shot refresh callback for HTTP 401."""
    def __new__(cls, value: str, refresh):
        instance = super().__new__(cls, value)
        instance._refresh_callback = refresh
        return instance

    def refresh(self):
        return self._refresh_callback()


class ShopifyAuthService:
    SAFETY_WINDOW_SECONDS = 300

    def __init__(self, *, db=None, opener=None, clock=time.time):
        self.db = db
        self.opener = opener or urllib.request.urlopen
        self.clock = clock

    def token_for(self, store_id: str, *, force_refresh=False) -> ShopifyAccessToken:
        from .shopify_collections import _safe_domain
        connection = _connection(self.db, store_id)
        if not connection:
            raise ShopifyAuthError("MISSING_CREDENTIALS", "Shopify 연결 정보가 없습니다.")
        domain = _safe_domain(connection["shop_domain"])
        credentials = get_dev_credentials(store_id)
        if not credentials:
            raise ShopifyAuthError("MISSING_CREDENTIALS", "Shopify Dev Dashboard의 Client ID와 Client Secret을 입력해야 합니다.")
        fingerprint = (domain, credentials["client_id"], credentials["client_secret"])
        now = float(self.clock())
        with _CACHE_LOCK:
            cached = _CACHE.get(str(store_id))
            if (not force_refresh and cached and cached["fingerprint"] == fingerprint and
                    cached["expires_at"] - now > self.SAFETY_WINDOW_SECONDS):
                return self._leased(store_id, cached["token"])
        token, scope, expires_in = self._request_token(domain, credentials)
        if not token or not isinstance(token, str):
            raise ShopifyAuthError("TOKEN_ERROR", "Shopify가 유효한 access token을 반환하지 않았습니다.")
        expires_at = now + max(1, expires_in)
        with _CACHE_LOCK:
            _CACHE[str(store_id)] = {"token": token, "scope": scope, "expires_at": expires_at,
                                     "fingerprint": fingerprint}
        _set_expiry(self.db, store_id, datetime.fromtimestamp(expires_at, timezone.utc).isoformat(timespec="seconds"),scope)
        return self._leased(store_id, token)

    def _leased(self, store_id, token):
        return ShopifyAccessToken(token, lambda: self.token_for(store_id, force_refresh=True))

    def _request_token(self, domain: str, credentials: dict):
        url = f"https://{domain}/admin/oauth/access_token"
        form = urllib.parse.urlencode({"grant_type": "client_credentials", **credentials}).encode("ascii")
        request = urllib.request.Request(url, data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        try:
            with self.opener(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code = self._error_code(exc)
            raise self._mapped_error(code) from None
        except (TimeoutError, urllib.error.URLError):
            raise ShopifyAuthError("NETWORK_ERROR", "Shopify 인증 요청이 시간 초과 또는 네트워크 오류로 실패했습니다. 다시 시도하세요.") from None
        except Exception as exc:
            raise ShopifyAuthError("TOKEN_ERROR", f"Shopify 인증 응답을 안전하게 처리하지 못했습니다 ({type(exc).__name__}).") from None
        code = str(payload.get("error") or "") if isinstance(payload, dict) else ""
        if code:
            raise self._mapped_error(code)
        try:
            expires_in = int(payload.get("expires_in"))
        except (TypeError, ValueError):
            raise ShopifyAuthError("TOKEN_ERROR", "Shopify 인증 응답에 만료 정보가 없습니다.") from None
        return payload.get("access_token"), str(payload.get("scope") or ""), expires_in

    @staticmethod
    def _error_code(exc):
        try:
            body = exc.read(8192).decode("utf-8", "replace")
            value = json.loads(body)
            if isinstance(value, dict):
                return str(value.get("error") or value.get("error_description") or "HTTP_ERROR").split()[0]
        except Exception:
            pass
        return "HTTP_ERROR"

    @staticmethod
    def _mapped_error(code):
        normalized = str(code).casefold()
        if "shop_not_permitted" in normalized:
            return ShopifyAuthError("SHOP_NOT_PERMITTED",
                "현재 앱/스토어 조합에서는 Client Credentials 인증이 허용되지 않습니다. Dev Dashboard의 앱과 대상 스토어 조직/설치를 확인하세요.")
        if "invalid_client" in normalized or "unauthorized_client" in normalized:
            return ShopifyAuthError("BAD_CREDENTIAL", "Shopify Dev Dashboard 자격 증명을 확인하세요.")
        return ShopifyAuthError("AUTH_REJECTED", "Shopify 인증이 거부되었습니다. 앱 설치 상태와 자격 증명을 확인하세요.")

    def status(self, store_id: str) -> dict:
        connection = _connection(self.db, store_id)
        mode = (connection or {}).get("auth_mode") or LEGACY_ADMIN_TOKEN
        present = credential_present(store_id, db=self.db, auth_mode=mode)
        granted = list((connection or {}).get("scopes_json") and json.loads(connection["scopes_json"]) or [])
        if not present:
            token_state, expiry = "ERROR", None
        elif mode == LEGACY_ADMIN_TOKEN:
            token_state, expiry = "VALID", None
        else:
            with _CACHE_LOCK:
                cached = _CACHE.get(str(store_id))
            expiry = (connection or {}).get("token_expires_at")
            token_state = "VALID" if cached and cached["expires_at"] - self.clock() > self.SAFETY_WINDOW_SECONDS else "REFRESH_NEEDED"
        remaining = None
        if expiry and mode == DEV_DASHBOARD_CLIENT_CREDENTIALS:
            try:
                remaining = max(0, int((datetime.fromisoformat(expiry).timestamp() - self.clock()) / 60))
            except ValueError:
                pass
        required = {"read_themes"}
        optional = {"read_products", "write_products", "read_publications", "write_publications", "write_files","read_legal_policies",
                    "write_themes", "read_online_store_navigation", "write_online_store_navigation"}
        return {"auth_mode": mode, "credential_present": present, "token_state": token_state,
                "expires_in_minutes": remaining, "last_verified_at": (connection or {}).get("last_verified_at"),
                "granted_scopes": granted, "missing_required_scopes": sorted(required - set(granted)),
                "missing_optional_scopes": sorted(optional - set(granted))}
