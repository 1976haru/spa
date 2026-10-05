"""Central, secret-safe Shopify authentication for local ShopSource clients."""
from __future__ import annotations

import json
import hashlib
import hmac
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

SHOPIFY_APP_IDENTITY_QUERY = """query ShopSourceAppIdentity {
  app { id title apiKey requestedAccessScopes { handle } optionalAccessScopes { handle } }
  currentAppInstallation { id accessScopes { handle } }
  shop { id name myshopifyDomain primaryDomain { host } }
}"""


class ShopifyAuthError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
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


def _profile_credential_key(profile_id: str) -> str:
    return f"app-profile:{profile_id}:dev-dashboard-client"


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


def get_profile_credentials(profile_id: str) -> dict | None:
    try:
        raw = _keyring().get_password(KEYRING_SERVICE, _profile_credential_key(profile_id))
        value = json.loads(raw) if raw else None
        if value and value.get("client_id") and value.get("client_secret"):
            return {"client_id": value["client_id"], "client_secret": value["client_secret"]}
    except Exception:
        return None
    return None


def _app_profile(db, profile_id: str) -> dict | None:
    from .shopify_collections import _install_schema
    _install_schema(db)
    with connect(db) as con:
        row = con.execute("SELECT * FROM shopify_app_profiles WHERE profile_id=?", (profile_id,)).fetchone()
    return dict(row) if row else None


def list_app_profiles(db=None) -> list[dict]:
    from .shopify_collections import _install_schema
    _install_schema(db)
    with connect(db) as con:
        rows = con.execute("SELECT profile_id,display_name,expected_app_gid,expected_app_title,client_id_fingerprint,api_version,status,last_verified_at FROM shopify_app_profiles ORDER BY display_name,profile_id").fetchall()
    return [dict(row) for row in rows]


def _identity_result(domain: str, api_version: str, token: str, client_id: str, *, db=None,
                     expected_app_gid: str | None = None, expected_shop_gid: str | None = None,
                     client_factory=None) -> dict:
    """Read authoritative app/store identity; never infer it from dashboard labels."""
    if client_factory is None:
        from .shopify_collections import ShopifyGraphQLClient
        client_factory = ShopifyGraphQLClient
    identity = client_factory(domain, token, api_version).execute(SHOPIFY_APP_IDENTITY_QUERY)
    app = identity.get("app") or {}
    installation = identity.get("currentAppInstallation") or {}
    shop = identity.get("shop") or {}
    key_matches = bool(app.get("apiKey")) and hmac.compare_digest(str(app.get("apiKey")), str(client_id))
    app_id_matches = not expected_app_gid or app.get("id") == expected_app_gid
    shop_id_matches = not expected_shop_gid or shop.get("id") == expected_shop_gid
    configured = str(domain or "").casefold().rstrip(".")
    canonical = str(shop.get("myshopifyDomain") or "").casefold().rstrip(".")
    primary = str(((shop.get("primaryDomain") or {}).get("host")) or "").casefold().rstrip(".")
    domain_matches = bool(configured and configured in {canonical, primary})
    safe_identity = {"authenticated_app_id": app.get("id"), "authenticated_app_title": app.get("title"),
                     "shop_id": shop.get("id"), "shop_name": shop.get("name"),
                     "myshopify_domain": shop.get("myshopifyDomain"), "primary_domain": primary,
                     "installation_id": installation.get("id")}
    if not key_matches or not app_id_matches:
        actual_label = f"{app.get('title') or '이름 미확인'} [{app.get('id') or 'GID 미확인'}]"
        raise ShopifyAuthError("APP_IDENTITY_MISMATCH", f"인증된 Shopify 앱 {actual_label}이 입력한 Client ID 또는 선택한 Production App과 일치하지 않습니다.",
                               {**safe_identity, "expected_app_gid": expected_app_gid})
    if not shop_id_matches or not shop.get("id") or not domain_matches:
        raise ShopifyAuthError("STORE_IDENTITY_MISMATCH", "인증된 Shopify 스토어가 이 연결의 스토어와 일치하지 않습니다.",
                               {**safe_identity, "expected_shop_gid": expected_shop_gid, "configured_domain": domain})
    handles = lambda rows: sorted({str(row.get("handle")) for row in rows or [] if isinstance(row, dict) and row.get("handle")})
    return {"app_id": app.get("id"), "app_title": app.get("title"), "client_id_fingerprint": hashlib.sha256(str(client_id).encode()).hexdigest(),
            "shop_id": shop.get("id"), "shop_name": shop.get("name"), "myshopify_domain": shop.get("myshopifyDomain"),
            "primary_domain": (shop.get("primaryDomain") or {}).get("host"), "installation_id": installation.get("id"),
            "requested_scopes": handles(app.get("requestedAccessScopes")),
            "optional_scopes": handles(app.get("optionalAccessScopes")), "granted_scopes": handles(installation.get("accessScopes"))}


def verify_and_bind_app_profile(store_id: str, shop_domain: str, client_id: str, client_secret: str, *,
                                profile_id: str | None = None, display_name: str | None = None,
                                api_version: str = "2026-07", db=None, opener=None,
                                client_factory=None, clock=time.time) -> dict:
    """Validate a candidate app and store before persisting credentials or binding."""
    from .shopify_collections import _safe_domain, _install_schema, _now
    client_id, client_secret = str(client_id or "").strip(), str(client_secret or "").strip()
    if not client_id or not client_secret:
        raise ShopifyAuthError("MISSING_CREDENTIALS", "Client ID와 Client Secret을 입력해야 합니다.")
    domain = _safe_domain(shop_domain)
    connection = _connection(db, store_id)
    expected_shop_gid = (connection or {}).get("shopify_shop_gid")
    candidate_profile_id = profile_id or ("app-" + hashlib.sha256(client_id.encode()).hexdigest()[:20])
    prior = _app_profile(db, candidate_profile_id)
    credentials = {"client_id": client_id, "client_secret": client_secret}
    service = ShopifyAuthService(db=db, opener=opener, clock=clock)
    token, _, expires_in = service._request_token(domain, credentials)
    if not token:
        raise ShopifyAuthError("TOKEN_ERROR", "Shopify 인증 응답에 access token이 없습니다.")
    identity = _identity_result(domain, api_version, token, client_id, db=db,
                                expected_app_gid=(prior or {}).get("expected_app_gid"),
                                expected_shop_gid=expected_shop_gid, client_factory=client_factory)
    # Only after authoritative identity checks pass do credentials and bindings persist.
    bundle = json.dumps(credentials, separators=(",", ":"))
    _keyring().set_password(KEYRING_SERVICE, _profile_credential_key(candidate_profile_id), bundle)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    expires_at = float(clock()) + max(1, int(expires_in))
    _install_schema(db)
    with connect(db) as con:
        con.execute("""INSERT INTO shopify_app_profiles(profile_id,display_name,expected_app_gid,expected_app_title,
          client_id_fingerprint,api_version,required_scopes_json,optional_scopes_json,status,last_verified_at,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(profile_id) DO UPDATE SET display_name=excluded.display_name,
          expected_app_gid=excluded.expected_app_gid,expected_app_title=excluded.expected_app_title,
          client_id_fingerprint=excluded.client_id_fingerprint,api_version=excluded.api_version,
          required_scopes_json=excluded.required_scopes_json,optional_scopes_json=excluded.optional_scopes_json,
          status=excluded.status,last_verified_at=excluded.last_verified_at,updated_at=excluded.updated_at""",
          (candidate_profile_id, display_name or identity["app_title"], identity["app_id"], identity["app_title"],
           identity["client_id_fingerprint"], api_version, json.dumps(identity["requested_scopes"]),
           json.dumps(identity["optional_scopes"]), "VERIFIED", now_iso, now_iso, now_iso))
        con.execute("""INSERT INTO shopify_connections(store_id,shop_domain,api_version,status,scopes_json,
          publications_json,last_verified_at,updated_at,auth_mode,shopify_shop_gid,app_profile_id,
          shopify_installation_gid,canonical_myshopify_domain,primary_domain)
          VALUES(?,?,?,'IDENTITY_VERIFIED',?,'[]',?,?,?,?,?,?,?,?)
          ON CONFLICT(store_id) DO UPDATE SET shop_domain=excluded.shop_domain,api_version=excluded.api_version,
          auth_mode=excluded.auth_mode,app_profile_id=excluded.app_profile_id,shopify_shop_gid=excluded.shopify_shop_gid,
          shopify_installation_gid=excluded.shopify_installation_gid,canonical_myshopify_domain=excluded.canonical_myshopify_domain,
          primary_domain=excluded.primary_domain,status=excluded.status,scopes_json=excluded.scopes_json,
          last_verified_at=excluded.last_verified_at,updated_at=excluded.updated_at""",
          (store_id, domain, api_version, json.dumps(identity["granted_scopes"]), now_iso, now_iso,
           DEV_DASHBOARD_CLIENT_CREDENTIALS, identity["shop_id"], candidate_profile_id, identity["installation_id"],
          identity["myshopify_domain"], identity["primary_domain"]))
        con.execute("""UPDATE shopify_connections SET authenticated_app_gid=?,authenticated_app_title=?,
          authenticated_requested_scopes_json=?,authenticated_optional_scopes_json=?,client_id_fingerprint=?
          WHERE store_id=?""", (identity["app_id"], identity["app_title"],
          json.dumps(identity["requested_scopes"]), json.dumps(identity["optional_scopes"]),
          identity["client_id_fingerprint"], store_id))
    with _CACHE_LOCK:
        _CACHE[str(store_id)] = {"token": token, "scope": " ".join(identity["granted_scopes"]),
                                 "expires_at": expires_at,
                                 "fingerprint": (domain, client_id, client_secret, candidate_profile_id,
                                                 identity["app_id"], identity["shop_id"]),
                                 "identity": identity}
    _set_expiry(db, store_id, datetime.fromtimestamp(expires_at, timezone.utc).isoformat(timespec="seconds"),
                " ".join(identity["granted_scopes"]))
    return {**identity, "profile_id": candidate_profile_id, "status": "VERIFIED", "expires_in": expires_in,
            "secret_values_exposed": False}


def bind_existing_app_profile(store_id: str, shop_domain: str, profile_id: str, *, db=None,
                              opener=None, client_factory=None, clock=time.time) -> dict:
    profile = _app_profile(db, profile_id)
    credentials = get_profile_credentials(profile_id)
    if not profile or not credentials:
        raise ShopifyAuthError("MISSING_CREDENTIALS", "선택한 Production App 프로필의 자격 증명을 찾을 수 없습니다.")
    return verify_and_bind_app_profile(store_id, shop_domain, credentials["client_id"], credentials["client_secret"],
        profile_id=profile_id, display_name=profile.get("display_name"), api_version=profile.get("api_version") or "2026-07",
        db=db, opener=opener, client_factory=client_factory, clock=clock)


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
    connection = _connection(db, store_id) or {}
    mode = auth_mode or connection.get("auth_mode") or LEGACY_ADMIN_TOKEN
    if mode == DEV_DASHBOARD_CLIENT_CREDENTIALS:
        profile_id = connection.get("app_profile_id")
        return (get_profile_credentials(profile_id) is not None if profile_id else get_dev_credentials(store_id) is not None)
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

    def __init__(self, *, db=None, opener=None, clock=time.time, client_factory=None):
        self.db, self.client_factory = db, client_factory
        self.opener = opener or urllib.request.urlopen
        self.clock = clock

    def token_for(self, store_id: str, *, force_refresh=False) -> ShopifyAccessToken:
        from .shopify_collections import _safe_domain
        connection = _connection(self.db, store_id)
        if not connection:
            raise ShopifyAuthError("MISSING_CREDENTIALS", "Shopify 연결 정보가 없습니다.")
        domain = _safe_domain(connection["shop_domain"])
        profile_id = connection.get("app_profile_id")
        profile = _app_profile(self.db, profile_id) if profile_id else None
        credentials = get_profile_credentials(profile_id) if profile_id else get_dev_credentials(store_id)
        if not credentials:
            raise ShopifyAuthError("MISSING_CREDENTIALS", "Shopify Dev Dashboard의 Client ID와 Client Secret을 입력해야 합니다.")
        fingerprint = (domain, credentials["client_id"], credentials["client_secret"], profile_id,
                       (profile or {}).get("expected_app_gid"), connection.get("shopify_shop_gid"))
        now = float(self.clock())
        with _CACHE_LOCK:
            cached = _CACHE.get(str(store_id))
            if (not force_refresh and cached and cached["fingerprint"] == fingerprint and
                    cached["expires_at"] - now > self.SAFETY_WINDOW_SECONDS):
                return self._leased(store_id, cached["token"])
        token, scope, expires_in = self._request_token(domain, credentials)
        if not token or not isinstance(token, str):
            raise ShopifyAuthError("TOKEN_ERROR", "Shopify가 유효한 access token을 반환하지 않았습니다.")
        # A token is not considered valid until Shopify identifies the app and store.
        try:
            identity = _identity_result(domain, connection.get("api_version") or "2026-07", token,
                credentials["client_id"], db=self.db,
                expected_app_gid=(profile or {}).get("expected_app_gid"),
                expected_shop_gid=connection.get("shopify_shop_gid"), client_factory=self.client_factory)
        except ShopifyAuthError as exc:
            if exc.code in {"APP_IDENTITY_MISMATCH", "STORE_IDENTITY_MISMATCH"}:
                with _CACHE_LOCK:
                    _CACHE.pop(str(store_id), None)
                from .shopify_collections import _install_schema
                _install_schema(self.db)
                with connect(self.db) as con:
                    con.execute("UPDATE shopify_connections SET status=? WHERE store_id=?", (exc.code, store_id))
            raise
        fingerprint = (domain, credentials["client_id"], credentials["client_secret"], profile_id,
                       (profile or {}).get("expected_app_gid"), identity["shop_id"])
        expires_at = now + max(1, expires_in)
        with _CACHE_LOCK:
            _CACHE[str(store_id)] = {"token": token, "scope": " ".join(identity["granted_scopes"]), "expires_at": expires_at,
                                     "fingerprint": fingerprint, "identity": identity}
        _set_expiry(self.db, store_id, datetime.fromtimestamp(expires_at, timezone.utc).isoformat(timespec="seconds"),
                    " ".join(identity["granted_scopes"]))
        from .shopify_collections import _install_schema
        _install_schema(self.db)
        with connect(self.db) as con:
            con.execute("""UPDATE shopify_connections SET authenticated_app_gid=?,authenticated_app_title=?,
              authenticated_requested_scopes_json=?,authenticated_optional_scopes_json=?,
              client_id_fingerprint=?,shopify_installation_gid=COALESCE(shopify_installation_gid,?),
              canonical_myshopify_domain=COALESCE(canonical_myshopify_domain,?),
              primary_domain=COALESCE(primary_domain,?),shopify_shop_gid=COALESCE(shopify_shop_gid,?)
              WHERE store_id=?""", (identity["app_id"],identity["app_title"],json.dumps(identity["requested_scopes"]),
              json.dumps(identity["optional_scopes"]),identity["client_id_fingerprint"],
              identity["installation_id"],identity["myshopify_domain"],identity["primary_domain"],identity["shop_id"],store_id))
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
        with _CACHE_LOCK:
            cached = _CACHE.get(str(store_id))
        identity = (cached or {}).get("identity") or {}
        profile_id = (connection or {}).get("app_profile_id")
        profile = _app_profile(self.db, profile_id) if profile_id else None
        from .shopify_scope_contract import scope_preflight
        scope_info = scope_preflight(granted, gate="G0_READ_ONLY")
        return {"auth_mode": mode, "credential_present": present, "token_state": token_state,
                "expires_in_minutes": remaining, "last_verified_at": (connection or {}).get("last_verified_at"),
                "granted_scopes": granted, "missing_required_scopes": scope_info["missing_for_current_gate"],
                "missing_optional_scopes": sorted(set(scope_info["declared_optional"]) - set(granted)),
                "scope_preflight": scope_info,
                "app_profile_id": profile_id, "production_app_bound": bool(profile),
                "expected_app_gid": (profile or {}).get("expected_app_gid"),
                "expected_app_title": (profile or {}).get("display_name"),
                "authenticated_app_gid": (connection or {}).get("authenticated_app_gid") or identity.get("app_id"),
                "authenticated_app_title": (connection or {}).get("authenticated_app_title") or identity.get("app_title"),
                "authenticated_requested_scopes": json.loads((connection or {}).get("authenticated_requested_scopes_json") or "[]") or identity.get("requested_scopes", []),
                "authenticated_optional_scopes": json.loads((connection or {}).get("authenticated_optional_scopes_json") or "[]") or identity.get("optional_scopes", []),
                "client_id_fingerprint": (connection or {}).get("client_id_fingerprint") or identity.get("client_id_fingerprint"),
                "shopify_shop_gid": (connection or {}).get("shopify_shop_gid"),
                "shopify_installation_gid": (connection or {}).get("shopify_installation_gid"),
                "declared_required_scopes": json.loads((profile or {}).get("required_scopes_json") or json.dumps(scope_info["declared_required"])),
                "declared_optional_scopes": json.loads((profile or {}).get("optional_scopes_json") or "[]")}
