"""Quién pidió esto y qué puede hacer (ADR-037).

Fase 1: identidad. Fase 2: roles. Access pone un portón delante
de un dominio de Access configurado e inyecta dos headers en cada request que lo cruza:

  Cf-Access-Authenticated-User-Email   el mail, en texto plano
  Cf-Access-Jwt-Assertion              la misma identidad, firmada

Solo el segundo vale. El de texto plano se usa para loguear y nada más:
quien llegue al origen sin pasar por el túnel puede escribirlo a mano.

El punto que define el diseño: el relay escucha en 127.0.0.1:8413 ADEMÁS
del túnel, y cloudflared le habla desde 127.0.0.1. Medido en la fase 0,
`request.remote` es "127.0.0.1" tanto para un request que cruzó Access
como para uno del bot en esta máquina — el peer no distingue nada y el
JWT es el único discriminador que hay.

Tres casos, y el tercero es el único que huele mal:

  JWT válido              → ese mail.
  sin headers de Access   → OWNER. Es el bot, la CLI o vos en tu máquina:
                            el comportamiento de siempre, intacto.
  headers + JWT inválido  → 403. Alguien llegó al origen sin cruzar el
                            portón y se escribió la identidad a mano.

ponytail: PyJWT ya viene instalado (transitivo de pydantic-ai) y su
PyJWKClient cachea el JWKS 300s solo. No hace falta dependencia nueva ni
cliente de certs propio.
"""
from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web

logger = logging.getLogger("relay.identity")

# Sentinela para "sin identidad de Access": localhost, el bot, la CLI.
# La fase 2 lo mapea al rol owner; hoy es solo una etiqueta para saber
# que el pedido no vino de una persona logueada por el túnel.
OWNER = "owner"

# Optional Access identity belongs to the operator's own installation.
# Missing configuration rejects signed requests before any network lookup.
TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip()
AUD = os.environ.get("CF_ACCESS_AUD", "").strip()

_JWT_HEADER = "Cf-Access-Jwt-Assertion"
_EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"

# Mismo motivo que los AppKey de server.py: sin esto aiohttp tira
# NotAppKeyWarning en cada request.
IDENTITY_KEY: web.RequestKey[str] = web.RequestKey("identity", str)

_jwks_client = None


def _jwks():
    global _jwks_client
    if _jwks_client is None:
        import jwt as pyjwt
        _jwks_client = pyjwt.PyJWKClient(
            f"https://{TEAM_DOMAIN}/cdn-cgi/access/certs")
    return _jwks_client


def verify(token: str) -> str:
    """Mail del JWT de Access, o revienta. Bloqueante: usar en to_thread.

    Chequea firma, `aud` y expiración. El fetch del JWKS es urllib
    bloqueante y por eso esto no se llama derecho desde el loop.
    """
    import jwt as pyjwt
    if not TEAM_DOMAIN:
        raise ValueError("CF_ACCESS_TEAM_DOMAIN no configurado")
    if not AUD:
        raise ValueError("CF_ACCESS_AUD no configurado")
    key = _jwks().get_signing_key_from_jwt(token)
    # leeway: medido en la fase 0, el reloj de esta máquina iba 4s atrás
    # del de Cloudflare y TODO token recién emitido moría con
    # ImmatureSignatureError (iat en el futuro). 60s cubre el drift de un
    # Windows sin NTP apretado sin aflojar el exp de verdad.
    claims = pyjwt.decode(
        token, key.key, algorithms=["RS256"], audience=AUD, leeway=60)
    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise ValueError("JWT de Access sin claim `email`")
    return email


async def resolve(request: web.Request) -> str:
    """OWNER, o el mail verificado. Levanta HTTPForbidden si el JWT miente."""
    token = request.headers.get(_JWT_HEADER, "").strip()
    if not token:
        # El header de mail SIN JWT es exactamente la firma del spoof:
        # nadie que cruce Access llega con uno y no el otro.
        if request.headers.get(_EMAIL_HEADER):
            logger.warning(
                "header %s sin JWT desde %s — lo ignoro y corto",
                _EMAIL_HEADER, request.remote)
            raise web.HTTPForbidden(
                text='{"error": "identidad no verificable"}',
                content_type="application/json")
        return OWNER
    try:
        return await asyncio.to_thread(verify, token)
    except Exception as e:  # noqa: BLE001 — cualquier fallo acá es un 403
        # Incluye el caso "no pude hablar con Cloudflare para traer los
        # certs": fail closed. Preferimos dejar afuera a alguien legítimo
        # antes que dejar entrar a alguien sin verificar.
        logger.warning("JWT de Access rechazado (%s): %s", type(e).__name__, e)
        raise web.HTTPForbidden(
            text='{"error": "identidad no verificable"}',
            content_type="application/json")


# ---------- roles (fase 2) ----------
#
# Dos, no cinco. `viewer` (solo lectura) se agrega si aparece alguien que
# de verdad lo necesite, no antes.
#
#   owner  — todo, como siempre.
#   member — correr expertos y ver lo que salió. Sin config, sin MCPs,
#            sin modo nocturno, sin SQL con escritura, sin borrar nada.
#
# La tabla `users` NOMBRA A LOS OWNERS: quien no está es member. No hace
# falta dar de alta a nadie para que entre, porque para llegar hasta acá
# ya tuvo que pasar la policy de Access — el alta de verdad se hace ahí,
# y sacar a alguien también.
OWNER_ROLE = "owner"
MEMBER_ROLE = "member"

# Cargado en el startup desde `users`. Global de módulo por el mismo
# motivo que `_jwks_client`: una app por proceso, y así el chequeo de rol
# es un lookup en un dict y no una consulta a SQLite por request.
_roles: dict[str, str] = {}


def load_roles(rows) -> None:
    """Refresca el cache de roles. `rows` son filas con email/role."""
    global _roles
    _roles = {r["email"].strip().lower(): r["role"] for r in rows}
    logger.info("roles cargados: %d", len(_roles))


def role_of(request: web.Request) -> str:
    who = requester(request)
    # Sin identidad de Access es el bot / la CLI / esta máquina: owner,
    # el comportamiento de siempre. Quien llega hasta el socket local ya
    # tiene la máquina, un rol no lo va a frenar.
    if who == OWNER:
        return OWNER_ROLE
    return _roles.get(who, MEMBER_ROLE)


# Lo que puede tocar un member. Es allowlist y no blocklist a propósito:
# hay 245 rutas y con una blocklist alcanza con olvidarse de UNA para
# regalarla. Así, lo que se agregue mañana nace owner-only hasta que
# alguien lo piense.
#
# Las entradas son (método, patrón de ruta) — el patrón, no el path
# concreto: `/chats/{id}` cubre cualquier id sin parsear nada.
MEMBER_ALLOWED = frozenset({
    # la UI en sí
    ("GET", "/admin"), ("GET", "/admin/"),
    ("GET", "/admin/static/{filename}"),
    ("GET", "/admin/api/me"), ("GET", "/admin/api/health"),
    # el selector de modelos del composer: sin esto el member no puede
    # ni elegir con qué corre
    ("GET", "/admin/api/models"),
    ("GET", "/health"), ("GET", "/system/active"), ("GET", "/stats"),
    # elegir proyecto (solo leer: el alta y el borrado son del owner)
    ("GET", "/admin/api/projects"), ("GET", "/admin/api/projects/{slug}"),
    ("GET", "/admin/api/search"),
    # correr expertos, y poder frenarlos o corregirlos
    ("POST", "/experts/run"),
    ("GET", "/experts/status/{chat_id}"),
    ("POST", "/experts/cancel/{chat_id}"),
    ("POST", "/experts/steer/{chat_id}"),
    # responder las preguntas que hace el experto en modo interactivo:
    # sin esto un run suyo se queda colgado esperando
    ("GET", "/questions"),
    ("POST", "/questions/{q_id}/answer"),
    ("POST", "/questions/{q_id}/skip"),
    # abrir el hilo y cerrarlo. `POST /conversations` es la MISMA ruta que
    # usa el /nuevo del bot, pero también es la única forma de arrancar un
    # chat desde la UI: sin esto un member no puede hacer lo único que un
    # member existe para hacer. Abre una rama git, sí — pero el run que
    # habilita escribe archivos igual, así que la rama es lo de menos.
    ("POST", "/conversations"),
    # Cerrar es la otra mitad de abrir, no un extra: hay UNA conversación
    # abierta por repo, así que un hilo que nadie puede cerrar deja el
    # proyecto trabado para todo el mundo. Abre un PR, no mergea.
    ("POST", "/conversations/{id}/close"),
    ("POST", "/conversations/{id}/compact"),
    # Ver el plan del hilo que él mismo lanzó. Sin esto el member puede
    # arrancar trabajo pero no mirar qué está haciendo, que es la mitad
    # que hace útil a la otra. Reanudar y cancelar el grafo van con el
    # plan a propósito: cancelar es lo mismo que ya puede hacer con
    # `POST /experts/cancel/{chat_id}`, y reanudar no le da nada que no
    # consiga lanzando otro `POST /experts/run`.
    ("GET", "/conversations/{id}/plan"),
    ("POST", "/graphs/{id}/resume"),
    ("POST", "/graphs/{id}/cancel"),
    # Contestar las preguntas del modo nocturno. No es "abrir night al
    # member": arrancarlo, configurarlo y ver los runs siguen siendo del
    # owner. Es que si el único despierto es un member, un run se queda
    # colgado esperando una respuesta que nadie puede dar.
    ("GET", "/admin/api/night/questions"),
    ("POST", "/admin/api/night/questions/{q_id}/answer"),
    ("POST", "/admin/api/night/questions/{q_id}/skip"),
    # ver lo que salió
    ("GET", "/chats"), ("GET", "/chats/{id}"),
    ("GET", "/chats/{id}/md"), ("GET", "/chats/{id}/status"),
    ("GET", "/conversations"), ("GET", "/conversations/{id}"),
    ("GET", "/conversations/{id}/messages"),
    ("GET", "/conversations/{id}/branch"),
    ("GET", "/conversations/{id}/diff"),
    ("GET", "/conversations/{id}/pr"),
    # adjuntar una imagen al prompt y volver a verla
    ("POST", "/attachments"), ("GET", "/attachments/{attach_id}"),
})


def _allowed_for_member(request: web.Request) -> bool:
    route = request.match_info.route
    resource = route.resource if route is not None else None
    if resource is None:
        # No matcheó ninguna ruta: es la SystemRoute del 404. Dejarla
        # pasar para que conteste 404 y no 403 — si no, un member pide
        # /favicon.ico y el log dice que lo rechazamos.
        return True
    # HEAD lo registra aiohttp solo por cada GET; sigue al GET.
    method = "GET" if request.method == "HEAD" else request.method
    return (method, resource.canonical) in MEMBER_ALLOWED


@web.middleware
async def require_role(request: web.Request, handler):
    """Deny by default para todo el que no sea owner.

    Va acá y no en el menú a propósito: si la validación vive en la UI
    no es control, es decoración — basta un fetch a mano para saltearla.
    """
    if role_of(request) == OWNER_ROLE or _allowed_for_member(request):
        return await handler(request)
    # WARNING solo para lo que intenta HACER algo. Un GET rechazado es la
    # UI poll-eando tabs que no le tocan (night/questions cada 5s) y a
    # ese ritmo entierra el log; lo interesante es el POST/DELETE.
    nivel = logging.DEBUG if request.method in ("GET", "HEAD") else logging.WARNING
    logger.log(
        nivel, "member %s rechazado en %s %s", requester(request),
        request.method, request.path)
    return web.json_response(
        {"error": "forbidden",
         "message": "Esta acción es del owner del relay."},
        status=403)


def requester(request: web.Request) -> str:
    """Quién pidió esto, para persistir/loguear. Default OWNER.

    El default NO es laxitud: los handlers también se llaman derecho
    desde los tests con requests mockeados que nunca pasaron por el
    middleware, y ahí la respuesta correcta es la de siempre (owner).
    """
    return request.get(IDENTITY_KEY, OWNER)


@web.middleware
async def access_identity(request: web.Request, handler):
    request[IDENTITY_KEY] = await resolve(request)
    from .execution_policy import request_role
    token = request_role.set(role_of(request))
    try:
        return await handler(request)
    finally:
        request_role.reset(token)
