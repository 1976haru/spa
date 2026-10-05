"""Shared, secret-free profiles for production source-safety providers."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .db import connect

_KEYRING_SERVICE = "ShopSourceStudio.SourceProviders"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SourceProviderProfiles:
    """Persist provider metadata in SQLite and credentials only in Credential Manager."""

    def __init__(self, db=None):
        self.db = db
        with connect(db) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS source_provider_profiles (
                  profile_id TEXT PRIMARY KEY, provider_type TEXT NOT NULL,
                  display_name TEXT NOT NULL, credential_present INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'NOT_CHECKED', last_health_checked_at TEXT,
                  metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS source_provider_store_bindings (
                  store_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES source_provider_profiles(profile_id),
                  updated_at TEXT NOT NULL);
            """)

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:
            raise RuntimeError("Credential Manager support requires the optional 'credentials' dependencies.") from exc
        if os.name == "nt" and "win" not in type(keyring.get_keyring()).__name__.lower():
            raise RuntimeError("Windows Credential Manager backend is unavailable")
        return keyring

    def save_keepa_profile(self, profile_id: str, display_name: str, api_key: str, *, store_id: str | None = None):
        profile_id = str(profile_id).strip()
        display_name = str(display_name).strip() or "Keepa Source Safety"
        if not profile_id or not api_key.strip():
            raise ValueError("Profile ID and Keepa API key are required")
        self._keyring().set_password(_KEYRING_SERVICE, f"keepa:{profile_id}", api_key.strip())
        now = _now()
        with connect(self.db) as con:
            con.execute("""INSERT INTO source_provider_profiles
                (profile_id,provider_type,display_name,credential_present,status,metadata_json,created_at,updated_at)
                VALUES(?, 'KEEPA', ?, 1, 'NOT_CHECKED', '{}', ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET provider_type='KEEPA',display_name=excluded.display_name,
                credential_present=1,status='NOT_CHECKED',last_health_checked_at=NULL,metadata_json='{}',updated_at=excluded.updated_at""",
                (profile_id, display_name, now, now))
            if store_id is not None:
                con.execute("INSERT INTO source_provider_store_bindings(store_id,profile_id,updated_at) VALUES(?,?,?) "
                            "ON CONFLICT(store_id) DO UPDATE SET profile_id=excluded.profile_id,updated_at=excluded.updated_at",
                            (str(store_id), profile_id, now))
        return self.profile(profile_id)

    def bind(self, store_id: str, profile_id: str):
        with connect(self.db) as con:
            if not con.execute("SELECT 1 FROM source_provider_profiles WHERE profile_id=?", (profile_id,)).fetchone():
                raise KeyError(profile_id)
            con.execute("INSERT INTO source_provider_store_bindings(store_id,profile_id,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(store_id) DO UPDATE SET profile_id=excluded.profile_id,updated_at=excluded.updated_at",
                        (str(store_id), str(profile_id), _now()))
        return self.profile(profile_id)

    def profile(self, profile_id: str):
        with connect(self.db) as con:
            row = con.execute("SELECT * FROM source_provider_profiles WHERE profile_id=?", (profile_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["credential_present"] = bool(result["credential_present"])
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    def profiles(self):
        with connect(self.db) as con:
            rows = con.execute("SELECT * FROM source_provider_profiles WHERE provider_type='KEEPA' ORDER BY display_name,profile_id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["credential_present"] = bool(item["credential_present"])
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            result.append(item)
        return result

    def for_store(self, store_id: str):
        with connect(self.db) as con:
            row = con.execute("""SELECT p.* FROM source_provider_store_bindings b
                JOIN source_provider_profiles p ON p.profile_id=b.profile_id WHERE b.store_id=?""", (str(store_id),)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["credential_present"] = bool(result["credential_present"])
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    def credential(self, profile_id: str):
        try:
            value = self._keyring().get_password(_KEYRING_SERVICE, f"keepa:{profile_id}")
            if value:
                return value, "windows-credential-manager-profile"
        except Exception:
            pass
        # The explicitly registered compatibility profile may use the old
        # environment-variable precedence. A normal selected profile must not
        # silently switch identities when its profile credential is absent.
        from .sourcing.credentials import SERVICE_NAME as LEGACY_SERVICE, USERNAME as LEGACY_USERNAME
        if profile_id == "keepa-env-compat":
            from .sourcing.credentials import get_api_key
            return get_api_key()
        try:
            legacy = self._keyring().get_password(LEGACY_SERVICE, LEGACY_USERNAME)
            if legacy:
                return legacy, "windows-credential-manager-legacy"
        except Exception:
            pass
        return None, "missing"

    def register_env_compatibility(self, store_id: str | None = None):
        """Expose the legacy environment key as configured without copying it to SQLite."""
        from .sourcing.credentials import get_api_key
        value, source = get_api_key()
        if not value:
            return None
        profile_id = "keepa-env-compat"
        now = _now()
        with connect(self.db) as con:
            con.execute("""INSERT INTO source_provider_profiles
                (profile_id,provider_type,display_name,credential_present,status,metadata_json,created_at,updated_at)
                VALUES(?, 'KEEPA', 'Keepa (legacy environment compatibility)', 1, 'NOT_CHECKED', '{}', ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET credential_present=1,updated_at=excluded.updated_at""",
                (profile_id, now, now))
            if store_id is not None:
                con.execute("INSERT INTO source_provider_store_bindings(store_id,profile_id,updated_at) VALUES(?,?,?) "
                            "ON CONFLICT(store_id) DO NOTHING", (str(store_id), profile_id, now))
        return self.profile(profile_id)

    def create_keepa_provider(self, profile_id: str):
        key, source = self.credential(profile_id)
        if not key:
            raise RuntimeError("Source provider credential is not configured")
        from .sourcing.providers.keepa import KeepaProvider
        return KeepaProvider(api_key=key), source

    def record_health(self, profile_id: str, result: dict, *, status: str):
        safe = {name: result.get(name) for name in ("tokensLeft", "refillIn", "refillRate")}
        safe["tokens_consumed_since_health"] = 0
        now = _now()
        with connect(self.db) as con:
            con.execute("UPDATE source_provider_profiles SET status=?,last_health_checked_at=?,metadata_json=?,updated_at=? WHERE profile_id=?",
                        (status, now, json.dumps(safe, sort_keys=True), now, profile_id))
        return self.profile(profile_id)

    def record_usage(self, profile_id: str, tokens_consumed: int):
        with connect(self.db) as con:
            row = con.execute("SELECT metadata_json FROM source_provider_profiles WHERE profile_id=?", (profile_id,)).fetchone()
            if not row:
                return
            metadata = json.loads(row[0] or "{}")
            metadata["tokens_consumed_since_health"] = int(metadata.get("tokens_consumed_since_health", 0)) + max(0, int(tokens_consumed))
            con.execute("UPDATE source_provider_profiles SET metadata_json=?,updated_at=? WHERE profile_id=?",
                        (json.dumps(metadata, sort_keys=True), _now(), profile_id))

    def health_check(self, store_id: str, *, provider=None):
        profile = self.for_store(store_id)
        if not profile or not profile["credential_present"]:
            return {"status": "NOT_CONFIGURED", "health": "NOT_CHECKED", "credential_present": False,
                    "provider": "Keepa", "profile": profile}
        try:
            client = provider
            if client is None:
                client, _ = self.create_keepa_provider(profile["profile_id"])
            result = client.health()
            ok = bool(result.get("ok")) and result.get("tokensLeft") is not None
            updated = self.record_health(profile["profile_id"], result, status="PASS" if ok else "FAIL")
            return {"status": "READY" if ok else "HEALTH_FAILED", "health": "PASS" if ok else "FAIL",
                    "credential_present": True, "provider": "Keepa", "profile": updated,
                    **{name: result.get(name) for name in ("tokensLeft", "refillIn", "refillRate")}}
        except Exception as exc:
            updated = self.record_health(profile["profile_id"], {}, status="FAIL")
            # Exception details may contain provider data; never return them to UI/logs.
            return {"status": "HEALTH_FAILED", "health": "FAIL", "credential_present": True,
                    "provider": "Keepa", "profile": updated, "error_type": type(exc).__name__}

    def preflight(self, store_id: str, *, target_count: int, batch_size: int = 100):
        profile = self.for_store(store_id) or self.register_env_compatibility(store_id)
        batches = (int(target_count) + int(batch_size) - 1) // int(batch_size) if target_count else 0
        if not profile:
            return {"provider": "Keepa", "provider_status": "NOT CONFIGURED", "credential_present": False,
                    "health": "NOT_CHECKED", "target_count": int(target_count), "batch_size": batch_size,
                    "batch_count": batches, "estimated_tokens": int(target_count), "estimated_cost": "UNKNOWN",
                    "last_health_check": None, "usable": False, "status": "WAITING_FOR_INPUT"}
        key, _ = self.credential(profile["profile_id"])
        present = bool(key)
        # An environment/legacy compatibility key may be available after binding was created.
        health = profile.get("status", "NOT_CHECKED")
        meta = profile.get("metadata", {})
        tokens = meta.get("tokensLeft")
        available_tokens = max(0, int(tokens) - int(meta.get("tokens_consumed_since_health", 0))) if tokens is not None else None
        usable = present and health == "PASS" and (available_tokens is None or int(target_count) <= available_tokens)
        status = "READY" if usable else ("BLOCKED" if present and health == "PASS" and available_tokens is not None and int(target_count) > available_tokens else "WAITING_FOR_INPUT")
        return {"provider": "Keepa", "profile_id": profile["profile_id"], "provider_status": profile["display_name"],
                "credential_present": present, "health": health if present else "FAIL", "target_count": int(target_count),
                "batch_size": batch_size, "batch_count": batches, "estimated_tokens": int(target_count),
                "available_tokens": available_tokens, "tokensLeft": available_tokens, "refillIn": meta.get("refillIn"),
                "refillRate": meta.get("refillRate"), "estimated_cost": "UNKNOWN",
                "last_health_check": profile.get("last_health_checked_at"), "usable": usable, "status": status,
                "cost_note": "Plan pricing is not available from provider health; no monetary estimate is made."}
