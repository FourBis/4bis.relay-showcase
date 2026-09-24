"""Identidad por Cloudflare Access (fase 1).

Lo que importa que no se rompa:
- localhost sigue andando como siempre (el bot no manda headers de Access)
- el header de mail SIN JWT no alcanza para ser nadie (es el spoof)
- `verify` chequea firma Y `aud`: un JWT válido de OTRA app de Access
  no entra acá
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import jwt as pyjwt
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from cryptography.hazmat.primitives.asymmetric import rsa

from relay import identity


async def _ok_handler(request):
    return web.json_response({"identity": identity.requester(request)})


def _req(headers: dict | None = None):
    return make_mocked_request("GET", "/admin/api/me", headers=headers or {})


# ---------- middleware: los tres casos ----------


async def test_sin_headers_es_owner():
    """El bot, la CLI y localhost: el comportamiento de siempre."""
    req = _req()
    resp = await identity.access_identity(req, _ok_handler)
    assert resp.status == 200
    assert req[identity.IDENTITY_KEY] == identity.OWNER


async def test_email_sin_jwt_es_403():
    """El header de mail solo es texto plano: quien no cruzó el portón
    puede escribirlo a mano. Sin JWT no vale."""
    req = _req({"Cf-Access-Authenticated-User-Email": "intruso@example.test"})
    with pytest.raises(web.HTTPForbidden):
        await identity.access_identity(req, _ok_handler)


async def test_jwt_invalido_es_403(monkeypatch):
    monkeypatch.setattr(
        identity, "verify",
        lambda token: (_ for _ in ()).throw(ValueError("firma mala")))
    req = _req({"Cf-Access-Jwt-Assertion": "no-es-un-jwt"})
    with pytest.raises(web.HTTPForbidden):
        await identity.access_identity(req, _ok_handler)


async def test_jwt_valido_deja_el_mail(monkeypatch):
    monkeypatch.setattr(identity, "verify", lambda token: "someone@example.test")
    req = _req({"Cf-Access-Jwt-Assertion": "un-jwt"})
    resp = await identity.access_identity(req, _ok_handler)
    assert resp.status == 200
    assert req[identity.IDENTITY_KEY] == "someone@example.test"


# ---------- verify: firma + aud de verdad, sin red ----------


@pytest.fixture
def firma(monkeypatch):
    """Sustituye el JWKS de Cloudflare por una clave nuestra."""
    monkeypatch.setattr(identity, "TEAM_DOMAIN", "access.example.com")
    monkeypatch.setattr(identity, "AUD", "audience-example")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        identity, "_jwks",
        lambda: type("C", (), {
            "get_signing_key_from_jwt": staticmethod(
                lambda token: type("K", (), {"key": key.public_key()})())})())

    def sign(claims: dict, signing_key=key) -> str:
        return pyjwt.encode(claims, signing_key, algorithm="RS256")

    return sign


def test_verify_falla_antes_de_red_si_falta_configuracion(monkeypatch):
    monkeypatch.setattr(identity, "TEAM_DOMAIN", "")
    monkeypatch.setattr(identity, "AUD", "")
    monkeypatch.setattr(identity, "_jwks", lambda: pytest.fail("no debe intentar red"))
    with pytest.raises(ValueError, match="CF_ACCESS_TEAM_DOMAIN"):
        identity.verify("token-de-prueba")


def _claims(**over) -> dict:
    now = int(time.time())
    base = {"email": "user@example.test", "aud": identity.AUD,
            "iat": now, "exp": now + 3600}
    base.update(over)
    return base


def test_verify_acepta_el_token_de_la_app(firma):
    assert identity.verify(firma(_claims())) == "user@example.test"


def test_verify_rechaza_otra_aud(firma):
    """Un JWT de Access legítimo pero emitido para OTRA app (ej: crm)
    no puede entrar al relay."""
    with pytest.raises(pyjwt.InvalidAudienceError):
        identity.verify(firma(_claims(aud="otra-app-cualquiera")))


def test_verify_rechaza_firma_ajena(firma):
    otra = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(pyjwt.InvalidSignatureError):
        identity.verify(firma(_claims(), signing_key=otra))


def test_verify_rechaza_expirado(firma):
    now = int(time.time())
    with pytest.raises(pyjwt.ExpiredSignatureError):
        identity.verify(firma(_claims(iat=now - 7200, exp=now - 3600)))


def test_verify_tolera_reloj_atrasado(firma):
    """El bug de la fase 0: esta máquina iba 4s atrás de Cloudflare y
    TODO token recién emitido moría con ImmatureSignatureError."""
    now = int(time.time())
    assert identity.verify(firma(_claims(iat=now + 5, exp=now + 3600)))


# ---------- fase 2: roles ----------


@pytest.fixture
def roles(monkeypatch):
    """Equipo explícito: Access autentica, la tabla autoriza."""
    monkeypatch.setattr(
        identity, "_roles", {"alex@example.test": identity.OWNER_ROLE,
                            "sam@example.test": "member", "taylor@example.test": "member"})
    monkeypatch.setattr(identity, "_disabled", set())


def _routed(method: str, canonical: str, quien: str):
    """Request ya ruteada y con identidad puesta, como la ve require_role."""
    from unittest import mock
    req = make_mocked_request(method, canonical)
    # make_mocked_request ya deja un Mock en match_info.route; le
    # colgamos el patrón de ruta, que es lo único que mira require_role.
    req.match_info.route.resource = mock.Mock(canonical=canonical)
    req[identity.IDENTITY_KEY] = quien
    return req


async def _handler(request):
    return web.json_response({"ok": True})


async def test_localhost_sigue_siendo_owner(roles):
    """El bot y la CLI no mandan headers de Access: nada cambia para
    ellos. Quien llega al socket local ya tiene la máquina."""
    req = _routed("PUT", "/admin/api/config", identity.OWNER)
    assert identity.role_of(req) == identity.OWNER_ROLE
    assert (await identity.require_role(req, _handler)).status == 200


async def test_owner_de_la_tabla_puede_todo(roles):
    req = _routed("PUT", "/admin/api/config", "alex@example.test")
    assert (await identity.require_role(req, _handler)).status == 200


async def test_desconocido_no_recibe_acceso_automatico(roles):
    req = _routed("GET", "/chats", "nuevo@example.test")
    assert identity.role_of(req) == "unregistered"
    assert (await identity.require_role(req, _handler)).status == 403


async def test_member_puede_correr_expertos(roles):
    for method, path in [("POST", "/experts/run"),
                         ("GET", "/experts/status/{chat_id}"),
                         ("POST", "/experts/cancel/{chat_id}"),
                         ("GET", "/chats/{id}/md"),
                         ("POST", "/questions/{q_id}/answer"),
                         # 2026-08-18: sin estas dos un member no puede
                         # ni abrir un chat ni cerrarlo. Una persona se topó
                         # el 403 en POST /conversations el primer día.
                         ("POST", "/conversations"),
                         ("POST", "/conversations/{id}/close")]:
        req = _routed(method, path, "sam@example.test")
        resp = await identity.require_role(req, _handler)
        assert resp.status == 200, f"{method} {path} deberia estar permitido"


async def test_member_ve_el_plan_de_su_propio_hilo(roles):
    """2026-09-07: el panel del plan le daba 403. Podía lanzar trabajo y
    no ver qué estaba haciendo."""
    for method, path in [("GET", "/conversations/{id}/plan"),
                         ("POST", "/graphs/{id}/resume"),
                         ("POST", "/graphs/{id}/cancel")]:
        req = _routed(method, path, "sam@example.test")
        resp = await identity.require_role(req, _handler)
        assert resp.status == 200, f"{method} {path} deberia estar permitido"


async def test_member_puede_contestar_preguntas_de_night(roles):
    """Si el único despierto es un member, un night run que pregunta se
    queda colgado. Contestar sí; arrancar y configurar night, no."""
    for method, path in [("GET", "/admin/api/night/questions"),
                         ("POST", "/admin/api/night/questions/{q_id}/answer"),
                         ("POST", "/admin/api/night/questions/{q_id}/skip")]:
        req = _routed(method, path, "sam@example.test")
        resp = await identity.require_role(req, _handler)
        assert resp.status == 200, f"{method} {path} deberia estar permitido"


async def test_member_no_toca_lo_que_manda(roles):
    """Config, MCPs, modo nocturno, SQL y borrar: nada de eso."""
    for method, path in [("PUT", "/admin/api/config"),
                         ("POST", "/admin/api/mcp"),
                         ("DELETE", "/admin/api/mcp/{name}"),
                         ("POST", "/night-mode/start"),
                         # 2026-09-07: contestar una pregunta de night no
                         # abrió el resto de night ni el disparador. Lo
                         # que se agregó fue el vecino, no el barrio.
                         ("GET", "/night-mode/status"),
                         ("POST", "/graphs"),
                         ("POST", "/mcp"),
                         ("DELETE", "/admin/api/chats/{chat_id}"),
                         ("DELETE", "/projects/{slug}"),
                         ("POST", "/admin/api/restart"),
                         ("POST", "/commands/{name}/run"),
                         ("DELETE", "/conversations/{id}/branch")]:
        req = _routed(method, path, "sam@example.test")
        resp = await identity.require_role(req, _handler)
        assert resp.status == 403, f"{method} {path} NO deberia pasar"


async def test_member_no_escribe_por_una_ruta_que_lee(roles):
    """La allowlist es por (método, ruta): poder leer un proyecto no
    habilita el PATCH sobre el mismo path."""
    assert (await identity.require_role(
        _routed("GET", "/admin/api/projects/{slug}", "sam@example.test"),
        _handler)).status == 200
    assert (await identity.require_role(
        _routed("PATCH", "/admin/api/projects/{slug}", "sam@example.test"),
        _handler)).status == 403


async def test_ruta_nueva_nace_owner_only(roles):
    """El punto de la allowlist: lo que se agregue mañana no se cuela."""
    req = _routed("POST", "/admin/api/lo-que-venga", "sam@example.test")
    assert (await identity.require_role(req, _handler)).status == 403


async def test_path_inexistente_da_404_y_no_403(roles):
    """Sin ruta que matchee no hay nada que autorizar: que conteste el
    404 de siempre. Antes /favicon.ico llenaba el log de rechazos."""
    from unittest import mock
    req = make_mocked_request("GET", "/favicon.ico")
    req.match_info.route.resource = None       # SystemRoute del 404
    req[identity.IDENTITY_KEY] = "sam@example.test"
    assert (await identity.require_role(req, _handler)).status == 200


async def test_el_chat_completo_de_un_member(roles):
    """El flujo de la UI de punta a punta: si alguna cae, el member
    entra y no puede trabajar — que es como se rompió la primera vez."""
    flujo = [("GET", "/admin/api/projects"), ("POST", "/conversations"),
             ("POST", "/attachments"), ("POST", "/experts/run"),
             ("GET", "/experts/status/{chat_id}"),
             ("GET", "/conversations/{id}/messages"),
             ("GET", "/conversations/{id}/branch"),
             ("GET", "/conversations/{id}/diff"),
             ("POST", "/conversations/{id}/close"),
             ("GET", "/conversations/{id}/pr")]
    for method, path in flujo:
        resp = await identity.require_role(
            _routed(method, path, "taylor@example.test"), _handler)
        assert resp.status == 200, f"{method} {path} corta el flujo del chat"


async def test_la_allowlist_existe_de_verdad():
    """Guard contra el peor bug posible acá: que la allowlist nombre
    rutas que ya no existen y el member quede sin poder hacer nada,
    o peor, que se acepte cualquier string sin que nadie se entere."""
    from relay.server import create_app
    app = create_app()
    reales = set()
    for r in app.router.routes():
        if r.resource is not None:
            metodo = "GET" if r.method == "HEAD" else r.method
            reales.add((metodo, r.resource.canonical))
    huerfanas = (identity.MEMBER_ALLOWED | identity._FINANCE_ROUTES | identity._TEAM_ROUTES | identity._ACCOUNT_ROUTES) - reales
    assert not huerfanas, f"la allowlist nombra rutas inexistentes: {huerfanas}"
