"""Authorized review inbox and temporary, manually uploaded GitHub PNG evidence."""
from __future__ import annotations

from aiohttp import web

from . import external_evidence_images as images, external_pr_access
from . import external_review_notify, identity
from .app_state import DB_KEY
from .server_common import _require_auth
from .task_pr import TaskPRError


async def _scope(request):
    db, cid = request.app[DB_KEY], request.match_info["id"]
    try:
        project, _, _ = await external_pr_access.require_record(db, cid, identity.requester(request))
    except LookupError:
        raise web.HTTPNotFound()
    except PermissionError:
        raise web.HTTPForbidden(text='{"error":"Proyecto no autorizado"}', content_type="application/json")
    except ValueError:
        raise web.HTTPBadRequest(text='{"error":"La tarea no es un PR externo"}', content_type="application/json")
    return db, project, cid, identity.requester(request)


def _draft(value, cid):
    return {key: val for key, val in value.items() if not key.startswith("_")} | {
        "download_url": f"/conversations/{cid}/external-pr/images/{value['id']}"}


@_require_auth
async def image_list(request):
    db, project, cid, actor = await _scope(request)
    try:
        rows = await images.list_owned(db, project, cid, actor)
        return web.json_response({"images": [_draft(row, cid) for row in rows]}, headers={"Cache-Control": "no-store"})
    except PermissionError:
        return web.json_response({"error": "Imagen no autorizada"}, status=403)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=422)


@_require_auth
async def image_prepare(request):
    db, project, cid, actor = await _scope(request)
    try:
        reader = await request.multipart()
        fields, image = {}, None
        async for part in reader:
            if part.name == "image":
                if image is not None:
                    raise ValueError("Selecciona una sola imagen PNG")
                content = bytearray()
                while chunk := await part.read_chunk(size=65536):
                    content.extend(chunk)
                    if len(content) > images.MAX_BYTES:
                        raise web.HTTPRequestEntityTooLarge(max_size=images.MAX_BYTES, actual_size=len(content))
                image = {"filename": part.filename or "", "content_type": part.headers.get("Content-Type", ""),
                         "data": bytes(content)}
            elif part.name in {"head_sha", "target", "consent", "request_id"}:
                if part.name in fields:
                    raise ValueError("Campo duplicado")
                raw = await part.read_chunk(size=1024)
                if len(raw) > 512 or not part.at_eof():
                    raise ValueError("Campo de imagen demasiado largo")
                fields[part.name] = raw.decode("utf-8")
            else:
                raise ValueError("Campo de imagen desconocido")
        if fields.get("consent") != "true":
            raise ValueError("Confirma que revisaste la captura y su destino GitHub antes de prepararla")
        if image is None:
            raise ValueError("Selecciona un archivo PNG")
        result = await images.prepare(db, project, cid, actor, head_sha=fields.get("head_sha"),
                                      target=fields.get("target"), request_id=fields.get("request_id"), **image)
        return web.json_response(_draft(result, cid), status=201, headers={"Cache-Control": "no-store"})
    except PermissionError:
        return web.json_response({"error": "Imagen no autorizada"}, status=403)
    except (ValueError, UnicodeError) as exc:
        return web.json_response({"error": str(exc)}, status=422)
    except OSError:
        return web.json_response({"error": "No se pudo preparar la copia temporal; conserva el original"}, status=503)


@_require_auth
async def image_download(request):
    db, project, cid, actor = await _scope(request)
    try:
        draft = await images.get_owned(db, project, cid, actor, request.match_info["draft_id"])
        return web.FileResponse(draft["_path"], headers={"Content-Type": "image/png", "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "Content-Disposition": f'attachment; filename="{draft["id"]}.png"'})
    except PermissionError:
        return web.json_response({"error": "Imagen no autorizada"}, status=403)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=422)
    except OSError:
        return web.json_response({"error": "La copia temporal no está disponible"}, status=503)


@_require_auth
async def image_confirm(request):
    db, project, cid, actor = await _scope(request)
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"comment_url"}:
            raise ValueError("Indica el enlace del comentario GitHub")
        result = await images.confirm(db, project, cid, actor, request.match_info["draft_id"], body["comment_url"])
        await images.cleanup_confirmed(db)
        current = await images.list_owned(db, project, cid, actor)
        result = next((row for row in current if row["id"] == result["id"]), result)
        return web.json_response(_draft(result, cid), headers={"Cache-Control": "no-store"})
    except PermissionError:
        return web.json_response({"error": "Imagen no autorizada"}, status=403)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=422)
    except (TaskPRError, OSError):
        return web.json_response({"error": "No se confirmó el adjunto; se conserva la copia temporal para recuperar"}, status=502)


@_require_auth
async def inbox(request):
    try:
        rows = await external_review_notify.list_for(request.app[DB_KEY], identity.requester(request))
        return web.json_response({"notifications": rows}, headers={"Cache-Control": "no-store"})
    except ValueError:
        return web.json_response({"error": "Usa tu sesión personal de Relay para leer tus avisos"}, status=403)


@_require_auth
async def inbox_read(request):
    try:
        row = await external_review_notify.mark_read(request.app[DB_KEY], request.match_info["notification_id"],
                                                   identity.requester(request))
        return web.json_response({"notification": row}, headers={"Cache-Control": "no-store"}) if row else web.json_response(
            {"error": "Aviso no encontrado para tu cuenta"}, status=404)
    except ValueError:
        return web.json_response({"error": "Usa tu sesión personal de Relay para leer tus avisos"}, status=403)
