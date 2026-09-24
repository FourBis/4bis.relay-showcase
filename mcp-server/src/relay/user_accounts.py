"""Conexiones personales: tokens cifrados y actor heredado por asyncio."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

GOOGLE_SCOPES = frozenset({
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
})
PROVIDERS = {
    "github": {"label": "GitHub", "authorize": "https://github.com/login/oauth/authorize",
               "token": "https://github.com/login/oauth/access_token",
               "profile": "https://api.github.com/user"},
    "google": {"label": "Google / Gmail", "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
               "token": "https://oauth2.googleapis.com/token",
               "profile": "https://openidconnect.googleapis.com/v1/userinfo"},
}


class AccountError(RuntimeError):
    """Mensaje seguro para UI: nunca contiene respuestas ni tokens remotos."""

    def __init__(self, message: str, *, reauth: bool = False):
        super().__init__(message)
        self.reauth = reauth


current_actor: ContextVar[tuple["AccountStore", str] | None] = ContextVar(
    "relay_account_actor", default=None)
OAUTH_CONFIG_NAMES = frozenset({
    "RELAY_PUBLIC_URL", "RELAY_OAUTH_KEY",
    "RELAY_GITHUB_CLIENT_ID", "RELAY_GITHUB_CLIENT_SECRET",
    "RELAY_GOOGLE_CLIENT_ID", "RELAY_GOOGLE_CLIENT_SECRET",
})
_OAUTH_SECRET_NAMES = frozenset({
    "RELAY_OAUTH_KEY", "RELAY_GITHUB_CLIENT_SECRET", "RELAY_GOOGLE_CLIENT_SECRET",
})
_oauth_config: dict[str, str] = {}


def _read_user_environment() -> dict[str, str]:
    """Lee configuración User de Windows sin copiarla al environment del proceso."""
    if os.name != "nt":
        return {}
    import winreg
    values = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in OAUTH_CONFIG_NAMES:
                try:
                    value, _kind = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if isinstance(value, str):
                    values[name] = value
    except OSError:
        return {}
    return values


def load_oauth_config() -> None:
    """Cachea solo OAuth permitido y quita claves privadas del env heredable."""
    global _oauth_config
    registry = _read_user_environment()
    _oauth_config = {
        name: os.environ[name] if name in os.environ else registry.get(name, "")
        for name in OAUTH_CONFIG_NAMES
    }
    for name in _OAUTH_SECRET_NAMES:
        os.environ.pop(name, None)


def _config_value(name: str) -> str:
    if name not in OAUTH_CONFIG_NAMES:
        return ""
    return os.environ[name] if name in os.environ else _oauth_config.get(name, "")


def store_for(db) -> "AccountStore":
    if not hasattr(db, "_account_store"):
        db._account_store = AccountStore(db)
    return db._account_store


@contextmanager
def bind_actor(db, email: str | None):
    token = current_actor.set((store_for(db), (email or "").strip().lower()))
    try:
        yield
    finally:
        current_actor.reset(token)


async def require_account(provider: str) -> dict:
    actor = current_actor.get()
    if actor is None:
        raise AccountError("Esta operación requiere una persona autenticada y su cuenta conectada.")
    store, email = actor
    return await store.token(provider, email)


def provider_config(provider: str) -> dict:
    if provider not in PROVIDERS:
        raise AccountError("Proveedor no permitido.")
    prefix = f"RELAY_{provider.upper()}_"
    origin = _config_value("RELAY_PUBLIC_URL").rstrip("/")
    try:
        parsed = urlsplit(origin)
    except ValueError:
        raise AccountError("RELAY_PUBLIC_URL no es un origen HTTPS válido.") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise AccountError("El administrador debe configurar RELAY_PUBLIC_URL con el origen HTTPS de Relay.")
    client_id = _config_value(prefix + "CLIENT_ID").strip()
    secret = _config_value(prefix + "CLIENT_SECRET").strip()
    if not client_id or not secret:
        raise AccountError(f"El administrador debe configurar OAuth de {PROVIDERS[provider]['label']}.")
    return {**PROVIDERS[provider], "client_id": client_id, "client_secret": secret,
            "redirect_uri": f"{origin}/admin/api/account/{provider}/callback"}


def _cipher() -> Fernet:
    try:
        return Fernet(_config_value("RELAY_OAUTH_KEY").encode("ascii"))
    except (ValueError, UnicodeError):
        raise AccountError("Falta la clave de cifrado OAuth del servidor o no es válida.") from None


async def oauth_request(method: str, url: str, **kwargs) -> dict:
    """Endpoints fijos; no seguir redirects ni mostrar cuerpos con secretos."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.request(method, url, **kwargs)
        data = response.json()
        reauth = response.status_code in {401, 403} or (isinstance(data, dict) and data.get("error") in {
            "invalid_grant", "bad_refresh_token", "invalid_token", "bad_verification_code"})
        if response.status_code != 200:
            raise AccountError("El proveedor rechazó la autorización. Conecta tu cuenta nuevamente.", reauth=reauth)
        if not isinstance(data, dict) or data.get("error"):
            raise AccountError("No se pudo completar la autorización del proveedor.", reauth=reauth)
        return data
    except (httpx.HTTPError, ValueError):
        raise AccountError("No se pudo contactar al proveedor de la cuenta.") from None


class AccountStore:
    def __init__(self, db):
        self.db = db
        # ponytail: un proceso/unos pocos integrantes; locks locales por cuenta.
        # Si Relay pasa a varios workers, rotar tokens con exclusión en DB.
        self.locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.pending: dict[str, dict] = {}
        self.generations: dict[tuple[str, str], int] = {}
        self.mail_drafts: dict[str, dict] = {}

    def lock(self, provider, email):
        return self.locks.setdefault((provider, email), asyncio.Lock())

    def encrypt(self, value: dict) -> str:
        return _cipher().encrypt(json.dumps(value).encode()).decode("ascii")

    def decrypt(self, value: str) -> dict:
        try:
            return json.loads(_cipher().decrypt(value.encode("ascii")))
        except (InvalidToken, ValueError, UnicodeError):
            raise AccountError("No se pudo abrir la conexión guardada; vuelve a conectar la cuenta.") from None

    async def check_user(self, provider: str, email: str) -> dict:
        if provider not in PROVIDERS or not email or email == "owner":
            raise AccountError("Entra a Relay con tu correo autenticado para usar cuentas personales.")
        rows = await self.db.run("SELECT role, enabled FROM users WHERE email=?", (email,))
        if not rows or not rows[0]["enabled"]:
            raise AccountError("Tu cuenta de Relay no está habilitada.")
        role = rows[0]["role"]
        allowed = {"owner", "subadmin", "member"}
        if provider == "google":
            allowed.add("finance")
        if role not in allowed:
            raise AccountError("Tu rol no permite esta conexión.")
        return dict(rows[0])

    async def row(self, provider, email):
        rows = await self.db.run(
            "SELECT * FROM user_accounts WHERE email=? AND provider=?", (email, provider))
        return dict(rows[0]) if rows else None

    async def save(self, provider, email, profile, tokens):
        await self.check_user(provider, email)
        if not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
            raise AccountError("El proveedor no entregó un token de acceso.")
        if str(tokens.get("token_type", "bearer")).lower() != "bearer":
            raise AccountError("El proveedor devolvió un tipo de credencial no admitido.")
        if provider == "github":
            subject, login = str(profile.get("id") or ""), profile.get("login") or ""
            address = f"{subject}+{login}@users.noreply.github.com"
            if (not subject.isdecimal() or not isinstance(login, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login)):
                raise AccountError("GitHub no confirmó la identidad de la cuenta.")
        else:
            subject = str(profile.get("sub") or "")
            address = str(profile.get("email") or "").lower()
            login = address
            if not subject or profile.get("email_verified") is not True or address != email:
                raise AccountError("Conecta la cuenta Google del mismo correo con el que entraste a Relay.")
            if (not isinstance(tokens.get("scope"), str)
                    or not GOOGLE_SCOPES <= set(tokens["scope"].split())):
                raise AccountError("Debes autorizar lectura y envío de Gmail para conectar esta cuenta.")
        try:
            for field in ("expires_in", "refresh_token_expires_in"):
                if field in tokens and (isinstance(tokens[field], bool) or int(tokens[field]) <= 0):
                    raise ValueError(field)
            expires_at = time.time() + int(tokens["expires_in"]) if tokens.get("expires_in") else 0
            refresh_expires_at = (time.time() + int(tokens["refresh_token_expires_in"])
                                  if tokens.get("refresh_token_expires_in") else 0)
        except (TypeError, ValueError):
            raise AccountError("El proveedor devolvió una vigencia inválida.") from None
        blob = self.encrypt({**tokens, "provider": provider, "relay_email": email})
        try:
            await self.db.run(
                "INSERT INTO user_accounts(email,provider,subject,login,account_email,token_blob,"
                "expires_at,refresh_expires_at,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(email,provider) DO UPDATE SET subject=excluded.subject,"
                "login=excluded.login,account_email=excluded.account_email,token_blob=excluded.token_blob,"
                "expires_at=excluded.expires_at,refresh_expires_at=excluded.refresh_expires_at,"
                "status=excluded.status,updated_at=excluded.updated_at",
                (email, provider, subject, login, address, blob, expires_at,
                 refresh_expires_at, "connected", time.time()))
        except sqlite3.IntegrityError:
            raise AccountError("Esa cuenta ya está vinculada a otro integrante de Relay.") from None

    async def invalidate(self, provider, email):
        await self.db.run("UPDATE user_accounts SET status='reconnect',token_blob='',updated_at=? "
                          "WHERE email=? AND provider=?", (time.time(), email, provider))

    async def token(self, provider, email):
        await self.check_user(provider, email)
        async with self.lock(provider, email):
            await self.check_user(provider, email)
            row = await self.row(provider, email)
            if not row or row["status"] != "connected":
                raise AccountError(f"Conecta tu cuenta de {PROVIDERS[provider]['label']} en Mi cuenta.")
            data = self.decrypt(row["token_blob"])
            if data.get("provider") != provider or data.get("relay_email") != email:
                raise AccountError("La conexión guardada no pertenece a este usuario.")
            if row["expires_at"] and row["expires_at"] <= time.time() + 60:
                if (not data.get("refresh_token") or (row["refresh_expires_at"]
                        and row["refresh_expires_at"] <= time.time())):
                    await self.invalidate(provider, email)
                    raise AccountError("La conexión venció. Vuelve a conectar tu cuenta.")
                cfg = provider_config(provider)
                try:
                    fresh = await oauth_request("POST", cfg["token"], headers={"Accept": "application/json"},
                        data={"client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                              "grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
                except AccountError as exc:
                    if exc.reauth:
                        await self.invalidate(provider, email)
                    raise
                if not fresh.get("access_token") or not fresh.get("expires_in"):
                    await self.invalidate(provider, email)
                    raise AccountError("La renovación no entregó una credencial válida. Conecta nuevamente.")
                merged = {**data, **fresh}
                if "refresh_token_expires_in" not in fresh and row["refresh_expires_at"]:
                    merged["refresh_token_expires_in"] = max(1, int(row["refresh_expires_at"] - time.time()))
                profile = {"id": row["subject"], "login": row["login"]} if provider == "github" else {
                    "sub": row["subject"], "email": row["account_email"], "email_verified": True}
                await self.save(provider, email, profile, merged)
                data = merged
            return {"access_token": data["access_token"], "subject": row["subject"],
                    "login": row["login"], "email": row["account_email"],
                    "scopes": data.get("scope", "").split()}

    async def disconnect(self, provider, email):
        await self.check_user(provider, email)
        async with self.lock(provider, email):
            row = await self.row(provider, email)
            key = (provider, email)
            self.generations[key] = self.generations.get(key, 0) + 1
            await self.db.run("DELETE FROM user_accounts WHERE email=? AND provider=?", (email, provider))
            self.pending = {k: v for k, v in self.pending.items()
                            if (v["provider"], v["email"]) != (provider, email)}
            if provider == "google":
                self.mail_drafts = {k: v for k, v in self.mail_drafts.items() if v["email"] != email}
        return row
