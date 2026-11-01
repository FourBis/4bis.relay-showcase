"""Manual GitHub image evidence; no upload, comment creation, agent or repo execution.

Only Relay-owned PNG copies live under the configured DB parent's separate
external-evidence-tmp directory. Drafts survive cancellation/failure indefinitely.
The human downloads, uploads through GitHub's normal UI (upload is immediate),
posts the supplied marker, then supplies the comment permalink. Confirmation
uses their existing actor-bound GitHub connection and exact remote byte digest.
Private assets which cannot be downloaded safely remain pending, never cleaned.
Cleanup removes only confirmed, unchanged, non-linked Relay copies; it makes no
claim about deleting GitHub assets. Images never satisfy test declarations or
human review, and are never sent to a model. GitHub upload remains a manual browser action.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import uuid
import zlib
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from . import external_pr, github, identity, user_accounts
from .task_pr import TaskPRError, now

MAX_BYTES = 5 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
_ASSET = re.compile(r"https://github\.com/user-attachments/assets/(" + _UUID.pattern + r")(?![A-Za-z0-9_/?#-])")
_IMAGE = re.compile(r"!\[[^\]\n]{0,300}\]\((https://github\.com/user-attachments/assets/" + _UUID.pattern + r")\)")
_TABLE = "external_evidence_images"


def _palette_row(encoded, previous, filtering, depth, pixels, palette):
    row = bytearray(encoded)
    for index, value in enumerate(row):
        left, up = row[index - 1] if index else 0, previous[index]
        corner = previous[index - 1] if index else 0
        if filtering == 1:
            value += left
        elif filtering == 2:
            value += up
        elif filtering == 3:
            value += (left + up) // 2
        elif filtering == 4:
            prediction = left + up - corner
            a, b, c = abs(prediction - left), abs(prediction - up), abs(prediction - corner)
            value += left if a <= b and a <= c else up if b <= c else corner
        row[index] = value & 255
    for pixel in range(pixels):
        offset = pixel * depth
        if ((row[offset // 8] >> (8 - depth - offset % 8)) & (2 ** depth - 1)) >= palette:
            raise ValueError("Índice de paleta PNG inválido")
    return row


def validate_png(data):
    """Check CRC/order, dimensions and bounded decoded PNG scanlines, not just MIME."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        raise ValueError("PNG debe contener entre 1 byte y 5 MB")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Sólo se admiten imágenes PNG reales; JPEG, SVG y HTML no están habilitados")
    pos, kinds, compressed, palette = 8, [], bytearray(), None
    geometry = None
    while pos < len(data):
        if len(data) - pos < 12:
            raise ValueError("PNG truncado")
        size = struct.unpack_from(">I", data, pos)[0]
        kind = data[pos + 4:pos + 8]
        end = pos + size + 12
        if end > len(data) or not re.fullmatch(b"[A-Za-z]{4}", kind) or kind[2] & 32:
            raise ValueError("Chunk PNG inválido")
        value = data[pos + 8:pos + 8 + size]
        if zlib.crc32(kind + value) & 0xffffffff != struct.unpack_from(">I", data, end - 4)[0]:
            raise ValueError("CRC PNG inválido")
        if (not kinds and kind != b"IHDR") or (kind == b"IHDR" and kinds):
            raise ValueError("IHDR PNG inválido")
        if kind in {b"acTL", b"fcTL", b"fdAT"}:
            raise ValueError("PNG animado no habilitado")
        if kind == b"IHDR":
            if size != 13:
                raise ValueError("IHDR PNG inválido")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", value)
            depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if (not 1 <= width <= 4096 or not 1 <= height <= 4096
                    or width * height > 4194304 or depth not in depths.get(color, set())
                    or compression or filtering or interlace not in {0, 1}):
                raise ValueError("Geometría o formato PNG no habilitado (máximo 4096 por lado y 4 megapíxeles)")
            geometry = width, height, depth, color, interlace
        elif kind == b"PLTE":
            if palette is not None or b"IDAT" in kinds or not size or size % 3 or size > 768:
                raise ValueError("Paleta PNG inválida")
            palette = size // 3
        elif kind == b"IDAT":
            if b"IDAT" in kinds and kinds[-1] != b"IDAT":
                raise ValueError("IDAT PNG no contiguo")
            compressed.extend(value)
        elif kind == b"IEND":
            if size or b"IDAT" not in kinds or end != len(data):
                raise ValueError("IEND PNG inválido")
            kinds.append(kind)
            break
        elif not kind[0] & 32:
            raise ValueError("Chunk crítico PNG no reconocido")
        kinds.append(kind)
        pos = end
    if not geometry or not kinds or kinds[-1] != b"IEND":
        raise ValueError("PNG incompleto")
    width, height, depth, color, interlace = geometry
    if (color == 3 and (palette is None or palette > 2 ** depth)) or (color in {0, 4} and palette):
        raise ValueError("Paleta PNG incompatible")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    passes = [(0, 0, 1, 1)] if not interlace else [
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
    scanlines = []
    for x, y, dx, dy in passes:
        pw, ph = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
        if pw and ph:
            scanlines.extend((1 + (pw * channels * depth + 7) // 8, pw, row == 0) for row in range(ph))
    expected = sum(size for size, _, _ in scanlines)
    if expected > 40 * 1024 * 1024:
        raise ValueError("PNG decodificado demasiado grande")
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, expected + 1)
        if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("Datos PNG inválidos")
        offset = 0
        previous = bytearray()
        for size, pixels, first in scanlines:
            if raw[offset] > 4:
                raise ValueError("Filtro PNG inválido")
            if color == 3:
                if first:
                    previous = bytearray(size - 1)
                previous = _palette_row(raw[offset + 1:offset + size], previous, raw[offset], depth, pixels, palette)
            offset += size
    except zlib.error:
        raise ValueError("Compresión PNG inválida") from None
    return {"width": width, "height": height}


async def _schema(db):
    await db.run(f"CREATE TABLE IF NOT EXISTS {_TABLE} (id TEXT PRIMARY KEY, "
                 "conversation_id TEXT NOT NULL, project_slug TEXT NOT NULL, actor TEXT NOT NULL, "
                 "state TEXT NOT NULL, metadata TEXT NOT NULL)")


async def _scope(db, project, cid, actor):
    who = actor.strip().lower() if isinstance(actor, str) else ""
    current = await db.get_project((project or {}).get("slug", ""))
    users = await db.list_users()
    user = next((r for r in users if r["email"].lower() == who), {})
    if (not who or who == identity.OWNER or not current or not current.get("enabled", True)
            or (current.get("defaults_json") or {}).get("read_only")
            or current.get("repo_path") != project.get("repo_path")
            or not identity.user_can_write_project(user, current["slug"])):
        raise PermissionError("Proyecto no autorizado para tu cuenta")
    conv = await db.get_conversation(cid)
    task = await db.get_conversation_task(cid)
    if not conv or conv["project_slug"] != current["slug"] or task.get("mode") != "external_pr":
        raise PermissionError("Registro externo no autorizado")
    return who, current, task


async def _mapping_scope(db, project, cid, actor):
    """Refresh only local origin metadata, then recheck current ACL/policy after IO."""
    _, current, _ = await _scope(db, project, cid, actor)
    origin = await github.repo_slug(current["repo_path"], refresh=True)
    who, current, task = await _scope(db, current, cid, actor)
    expected = (task.get("external_pr") or {}).get("repo")
    if not isinstance(origin, str) or not isinstance(expected, str) or origin.casefold() != expected.casefold():
        raise ValueError("El origin local cambió respecto al registro; se conserva cualquier temporal pendiente")
    return who, current, task


def _target(task, kind):
    if kind not in {"pr", "issue"}:
        raise ValueError("Elige un único destino: pr o issue")
    pr = task.get("external_pr") or {}
    ref = pr if kind == "pr" else task.get("issue_link") or {}
    repo, number = ref.get("repo"), ref.get("number")
    if (not isinstance(repo, str) or not _REPO.fullmatch(repo) or repo.rsplit("/", 1)[1] in {".", ".."}
            or type(number) is not int or not 0 < number <= 2147483647
            or type(ref.get("repo_id")) is not int or ref["repo_id"] <= 0 or ref["repo_id"] != pr.get("repo_id")
            or repo.casefold() != str(pr.get("repo", "")).casefold()):
        raise ValueError("Falta un destino GitHub verificado del proyecto")
    return {"kind": kind, "repo": repo, "repo_id": ref["repo_id"], "number": number,
            "url": f"https://github.com/{repo}/{'pull' if kind == 'pr' else 'issues'}/{number}"}


def _linked(path):
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def _root(db, create=False):
    base = Path(db.path).absolute().parent.resolve(strict=True)
    root = base / "external-evidence-tmp"
    if _linked(root) or (root.exists() and not root.is_dir()):
        raise ValueError("Directorio temporal inseguro")
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
    if root.exists() and (_linked(root) or root.resolve() != root):
        raise ValueError("Directorio temporal inseguro")
    return root


def _path(db, draftid):
    if not isinstance(draftid, str) or not _UUID.fullmatch(draftid):
        raise ValueError("Identificador de imagen inválido")
    path = _root(db) / (draftid + ".png")
    if _linked(path) or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise ValueError("Archivo temporal inseguro")
    return path


def _public(row):
    return {**json.loads(row["metadata"]), "state": row["state"]}


def _check_copy(path, draft):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != draft["size"]:
            raise ValueError("La copia temporal cambió")
        data = stream.read(MAX_BYTES + 1)
    if len(data) != draft["size"] or hashlib.sha256(data).hexdigest() != draft["sha256"]:
        raise ValueError("La copia temporal cambió")


async def prepare(db, project, cid, actor, *, head_sha, target, filename, content_type, data, request_id=None):
    who, project, task = await _scope(db, project, cid, actor)
    if not isinstance(head_sha, str) or not _SHA.fullmatch(head_sha) or head_sha != task["external_pr"].get("head_sha"):
        raise ValueError("El commit cambió; actualiza el registro antes de preparar la imagen")
    if (content_type != "image/png" or not isinstance(filename, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,119}\.png", filename, re.I)):
        raise ValueError("Selecciona un archivo PNG con nombre simple y MIME image/png")
    geometry = await db._in_thread(validate_png, data)
    who, project, task = await _mapping_scope(db, project, cid, actor)
    if head_sha != task["external_pr"].get("head_sha"):
        raise ValueError("El commit cambió durante la preparación")
    target = _target(task, target)
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", request_id)):
        raise ValueError("request_id debe ser un identificador simple de hasta 100 caracteres")
    draftid = (str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["relay:image", cid, who, request_id])))
               if request_id is not None else str(uuid.uuid4()))
    request_id = request_id or draftid
    digest = hashlib.sha256(data).hexdigest()
    marker = f"<!-- relay-image:{draftid} request:{request_id} sha:{head_sha} target:{target['kind']}:{target['repo']}#{target['number']} sha256:{digest} -->"
    metadata = {"id": draftid, "request_id": request_id, "head_sha": head_sha, "target": target,
                "filename": filename, "mime": "image/png", "size": len(data), "sha256": digest,
                **geometry, "created_at": now(), "marker": marker, "error": "",
                "comment_body": f"Evidencia visual del commit `{head_sha}`.\n\n{marker}\n",
                "github_target_url": target["url"], "github_upload_supported": False,
                "manual_ui_required": True, "cleanup_policy": "confirmed_only"}
    await _schema(db)
    def store():
        conn = db._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(f"SELECT * FROM {_TABLE} WHERE id=?", (draftid,)).fetchone()
            if existing:
                saved = _public(existing)
                if ((existing["actor"], existing["conversation_id"], existing["project_slug"]) != (who, cid, project["slug"])
                        or any(saved.get(key) != metadata[key] for key in ("head_sha", "target", "sha256", "size", "mime", "filename"))):
                    raise ValueError("request_id ya usado para otra imagen, commit o destino")
                if existing["state"] != "cleaned":
                    _check_copy(_path(db, draftid), saved)
                return saved
            _root(db, create=True)
            path = _path(db, draftid)
            if path.exists():
                # Recover an identical owned copy after a failed local DB commit.
                _check_copy(path, metadata)
            else:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
            conn.execute(f"INSERT INTO {_TABLE} VALUES (?,?,?,?,?,?)",
                         (draftid, cid, project["slug"], who, "pending", json.dumps(metadata)))
            conn.commit()
            return {**metadata, "state": "pending"}
        finally:
            conn.close()
    # _in_thread drains on cancellation: the completed owned copy remains listed.
    return await db._in_thread(store)


async def _owned(db, project, cid, actor, draftid):
    who, project, task = await _scope(db, project, cid, actor)
    if not isinstance(draftid, str) or not _UUID.fullmatch(draftid):
        raise ValueError("Identificador de imagen inválido")
    await _schema(db)
    rows = await db.run(f"SELECT * FROM {_TABLE} WHERE id=? AND conversation_id=? AND project_slug=? AND actor=?",
                        (draftid, cid, project["slug"], who))
    if not rows:
        raise PermissionError("Imagen no autorizada para tu cuenta")
    return rows[0], task, who


async def get_owned(db, project, cid, actor, draftid):
    row, _, _ = await _owned(db, project, cid, actor, draftid)
    path = _path(db, draftid)
    if row["state"] == "cleaned" or not path.is_file():
        raise ValueError("La copia temporal ya no está disponible")
    await db._in_thread(_check_copy, path, _public(row))
    return {**_public(row), "_path": path}


async def list_owned(db, project, cid, actor):
    who, project, _ = await _scope(db, project, cid, actor)
    await _schema(db)
    rows = await db.run(f"SELECT * FROM {_TABLE} WHERE conversation_id=? AND project_slug=? AND actor=? ORDER BY rowid",
                        (cid, project["slug"], who))
    return [_public(row) for row in rows]


def _asset_url(url, *, initial=False):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}
            or parsed.fragment or len(url) > 4096):
        raise ValueError("URL de adjunto GitHub no permitida")
    if parsed.hostname == "github.com":
        if parsed.query or not _ASSET.fullmatch(url):
            raise ValueError("Se requiere un adjunto oficial de GitHub")
    elif initial or parsed.hostname not in {"private-user-images.githubusercontent.com", "user-images.githubusercontent.com"}:
        raise ValueError("Host de adjunto no permitido")
    elif not re.fullmatch(r"/[A-Za-z0-9_./-]+", parsed.path) or not parsed.path.lower().endswith(".png"):
        raise ValueError("Ruta de adjunto no permitida")
    return parsed.hostname


async def _download_asset(url):
    try:
        return await _fetch_asset(url)
    except httpx.HTTPError:
        raise TaskPRError("No se pudo leer el adjunto de GitHub; se conserva el temporal") from None


async def _fetch_asset(url):
    """GET only; no cookies/proxies. Never forward actor credentials to a redirect host."""
    _asset_url(url, initial=True)
    account = await user_accounts.require_account("github")
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        for _ in range(4):
            host = _asset_url(url)
            client.cookies.clear()
            headers = {"Accept": "image/png"}
            if host == "github.com":
                headers["Authorization"] = f"Bearer {account['access_token']}"
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("location", ""))
                    _asset_url(url)
                    continue
                if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0].lower() != "image/png":
                    raise TaskPRError("No se pudo verificar el PNG adjunto con la conexión existente; se conserva el temporal")
                result = bytearray()
                async for chunk in response.aiter_bytes():
                    result.extend(chunk)
                    if len(result) > MAX_BYTES:
                        raise ValueError("Adjunto remoto supera 5 MB")
                return bytes(result)
    raise TaskPRError("Demasiadas redirecciones del adjunto; se conserva el temporal")


def _comment_id(url, target):
    match = (re.fullmatch(re.escape(target["url"]) + r"#issuecomment-([1-9][0-9]{0,18})", url)
             if isinstance(url, str) and len(url) <= 500 else None)
    if not match:
        raise ValueError("Pega el enlace canónico del comentario en el destino seleccionado")
    return int(match[1])


async def confirm(db, project, cid, actor, draftid, comment_url):
    row, task, who = await _owned(db, project, cid, actor, draftid)
    draft = _public(row)
    commentid = _comment_id(comment_url, draft["target"])
    _, _, task = await _mapping_scope(db, project, cid, actor)
    if row["state"] in {"confirmed", "cleaned"}:
        if draft.get("comment_url") != comment_url:
            raise ValueError("La imagen ya fue confirmada con otro comentario")
        return draft
    if _target(task, draft["target"]["kind"]) != draft["target"]:
        raise ValueError("El destino cambió; se conserva el temporal")
    repo, number = draft["target"]["repo"], draft["target"]["number"]
    with user_accounts.bind_actor(db, who):
        human = await external_pr.actor_identity(who)
        endpoint = f"repos/{repo}/issues/comments/{commentid}"
        comment = await external_pr._api(endpoint)
        if not isinstance(comment, dict):
            raise ValueError("GitHub no confirmó el comentario")
        body = comment.get("body", "")
        if (not isinstance(body, str) or comment.get("id") != commentid or comment.get("html_url") != comment_url
                or comment.get("issue_url") != f"https://api.github.com/repos/{repo}/issues/{number}"
                or not isinstance(comment.get("user"), dict) or comment["user"].get("id") != human["github_id"]
                or comment["user"].get("type") != "User" or draft["marker"] not in body):
            raise ValueError("GitHub no confirma autor, destino y marcador SHA del comentario")
        assets = set(_IMAGE.findall(body))
        if not assets:
            raise ValueError("El comentario no contiene un adjunto oficial GitHub verificable")
        if len(assets) > 5:
            raise ValueError("El comentario contiene demasiados adjuntos para verificar")
        asset_url = None
        for candidate in sorted(assets):
            content = await _download_asset(candidate)
            await db._in_thread(validate_png, content)
            if len(content) == draft["size"] and hashlib.sha256(content).hexdigest() == draft["sha256"]:
                asset_url = candidate
                break
        if asset_url is None:
            raise ValueError("Los bytes del adjunto no coinciden con el PNG preparado")
        latest = await external_pr._api(endpoint)
        pr = await external_pr._api(f"repos/{repo}/pulls/{task['external_pr']['number']}")
        if latest != comment:
            raise ValueError("El comentario cambió durante la verificación; se conserva el temporal")
    _, _, current = await _mapping_scope(db, project, cid, actor)
    if _target(current, draft["target"]["kind"]) != draft["target"]:
        raise ValueError("El destino cambió durante la verificación")
    if not isinstance(pr, dict):
        raise ValueError("GitHub no confirmó la PR")
    if not isinstance(pr.get("head"), dict) or not isinstance(pr.get("base"), dict) or not isinstance(pr["base"].get("repo"), dict):
        raise ValueError("GitHub no confirmó la identidad actual de la PR")
    remote_sha = pr["head"].get("sha")
    if (pr.get("number") != task["external_pr"]["number"] or not isinstance(remote_sha, str)
            or not _SHA.fullmatch(remote_sha) or pr["base"]["repo"].get("id") != draft["target"]["repo_id"]):
        raise ValueError("GitHub no confirmó la identidad actual de la PR")
    draft.update(comment_url=comment_url, asset_url=asset_url, confirmed_at=now(),
                 historical=remote_sha != draft["head_sha"] or current["external_pr"]["head_sha"] != draft["head_sha"],
                 verified_image=True, verified_by=human)
    rows = await db.run(f"UPDATE {_TABLE} SET state='confirmed',metadata=? WHERE id=? AND actor=? AND state='pending' RETURNING *",
                        (json.dumps(draft), draftid, who))
    if not rows:
        existing, _, _ = await _owned(db, project, cid, actor, draftid)
        if _public(existing).get("comment_url") != comment_url:
            raise ValueError("Confirmación concurrente diferente; se conserva el temporal")
        return _public(existing)
    return _public(rows[0])


async def cleanup_confirmed(db):
    """No TTL/cancellation deletion; only unchanged owned copies with verified metadata."""
    await _schema(db)
    rows = await db.run(f"SELECT * FROM {_TABLE} WHERE state='confirmed'")
    cleaned = 0
    for row in rows:
        draft = _public(row)
        if draft.get("verified_image") is not True or not draft.get("comment_url") or not draft.get("asset_url"):
            continue
        try:
            path = _path(db, row["id"])
            if path.is_file():
                await db._in_thread(_check_copy, path, draft)
                path.unlink()
            changed = await db.run(f"UPDATE {_TABLE} SET state='cleaned' WHERE id=? AND state='confirmed' RETURNING id", (row["id"],))
            cleaned += len(changed)
        except (OSError, ValueError):
            continue
    return {"cleaned": cleaned}
