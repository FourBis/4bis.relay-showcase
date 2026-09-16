"""Attachments genéricos para el relay (iter Discord-attachment).

Stores bytes subidos por el bot C# (bot-demo) en
`<state_dir>/attachments/<sha256[:16]><ext>` y devuelve un id estable.
El id es el prefijo del SHA256 — si el bot sube el mismo archivo dos
veces (típico cuando Discord tiene el mismo URL en cache), el relay
devuelve el mismo id y path. Sin doble storage.

Ponytail: piggyback sobre el patrón de voice.py (mismo dir base
state/, misma env var para cap, mismas convenciones). Un solo
módulo que solo sabe: guardar bytes, mapear id→path.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("relay.attachments")

# Cap defensivo compartido con voice (mismo default 50MB, mismo env var
# vía ATTACHMENT_MAX_BYTES si está o voice como fallback). Evita abusar
# del disco sin configurar.
_MAX_FALLBACK = 50 * 1024 * 1024

# Extensiones bloqueadas: ejecutables y scripts que pueden ejecutarse
# si algo downstream (wrapper MCP, agente, humano con shell) los abre
# por error. El bot C# también podría recibir URLs maliciosas de
# Discord (links disfrazados de PDF/imagen). Bloquear por extensión
# es la línea base; el chequeo de mimetype real lo hace el antivirus
# o el OS — acá solo frenamos lo obvio.
# Bug fix 2026-07-18: el comentario del módulo decía que estos se
# rechazaban pero el código nunca lo hizo. Si un user Discord subió
# un .exe disfrazado, quedaba en state/attachments/ y se le pasaba
# al LLM como path.
_BLOCKED_EXTS = frozenset({
    ".exe", ".dll", ".com", ".scr", ".cpl", ".msi", ".msp", ".msc",
    ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".hta",
    ".sh", ".bash", ".zsh", ".ksh",
    ".py", ".pyc", ".pyo", ".pl", ".rb",
    ".jar", ".war",
    ".lnk", ".inf", ".reg",
})
# Mimetypes peligrosos: aunque la extensión venga limpia, el
# Content-Type que el bot declara puede mentir.
_BLOCKED_MIMES = frozenset({
    "application/x-msdownload",
    "application/x-msdos-program",
    "application/x-executable",
    "application/x-sh",
    "application/x-bat",
    "application/x-shellscript",
    "text/x-shellscript",
    "application/javascript",
    "application/x-javascript",
    "application/x-vbs",
})


# Raíz del repo (mcp-server/src/relay/attachments.py → cuatro arriba).
# Mismo cálculo que `config._REPO_ROOT`; se repite acá para no importar
# config y meter un ciclo (config no importa attachments, pero experts
# importa los dos y el orden no está garantizado).
_REPO_ROOT = Path(__file__).resolve().parents[3]


def attachments_dir() -> Path:
    """Directorio base de attachments. Absoluto SIEMPRE.

    2026-08-26: antes devolvía `Path("state") / "attachments"`, relativo,
    y eso quedaba anclado al CWD del proceso. Mientras el relay arranque
    desde la raíz del repo da lo mismo, pero desde que este directorio es
    una raíz extra del sandbox del experto (`Permisos.para`, ver
    `run_expert`) la ruta dejó de ser solo un lugar donde guardar: es un
    permiso. Un permiso que se mueve con el CWD es un permiso que no se
    puede razonar, así que se ancla al repo — que es donde ya resolvía.
    """
    raw = os.environ.get("FOURBIS_ATTACHMENTS_DIR", "").strip()
    if raw:
        # `expanduser` para que `~/...` funcione igual que en config.py;
        # `resolve` para que un relativo del env también quede absoluto.
        return Path(raw).expanduser().resolve()
    return _REPO_ROOT / "state" / "attachments"


# Un scope es un id de conversación o un slug de proyecto; los dos son
# nuestros, pero igual se sanea: termina siendo un nombre de directorio.
#
# El punto NO está en la lista blanca, y es deliberado: con `.` adentro,
# un scope de `..` sobrevive entero y `conv/..` es el store completo —
# o sea que la vista se abre justo a lo que existe para tapar. Ni los
# ids de conversación ni los slugs llevan puntos, así que no se pierde
# nada. Lo encontró el test de traversal, no el diseño.
_SCOPE_RE = re.compile(r"[^A-Za-z0-9_-]")


def scope_for(conversation_id: str = "", project_slug: str = "") -> str:
    """El scope de un run: su conversación, o su proyecto si no tiene.

    Una sola función porque la usan dos lados —el handler, que escribe
    las rutas en el prompt, y `run_expert`, que habilita la raíz del
    sandbox— y si divergen el experto termina con un path que no puede
    abrir. Los dos aíslan entre clientes, que es lo que importa; la
    conversación además hace que un id citado tres mensajes atrás siga
    resolviendo, porque su hardlink quedó en la vista desde entonces.
    """
    conv = str(conversation_id or "").strip()
    return conv if conv else f"proj-{str(project_slug or '').strip()}"


def scope_dir(scope: str) -> Path:
    """Vista por conversación del store (2026-08-26).

    El store es content-addressed y PLANO: `state/attachments/<sha256>`.
    Eso es lo correcto para guardar —dedup gratis entre todo el mundo—
    pero desde que el directorio es raíz del sandbox del experto pasó a
    ser también una superficie de lectura, y ahí lo plano deja de servir:
    un run del cliente A tiene delante los adjuntos del cliente B.

    La vista arregla eso sin tocar el store. Los blobs siguen donde
    están (un solo archivo por contenido); acá abajo cuelga un
    directorio por conversación con hardlinks a los que esa conversación
    citó. El experto ve solo su vista; el dedup no se pierde porque el
    hardlink no copia bytes.
    """
    safe = _SCOPE_RE.sub("-", str(scope or "").strip())[:64] or "sin-scope"
    return attachments_dir() / "conv" / safe


def materializar(scope: str, blob: Path) -> Path:
    """Deja `blob` visible en la vista de `scope` y devuelve esa ruta.

    Hardlink si el filesystem lo permite (NTFS sí, y cuesta cero bytes);
    copia si no. Si las dos fallan, devuelve el blob original: perder la
    aislación es mejor que perder el adjunto, y el warning queda en el
    log para que se note.
    """
    d = scope_dir(scope)
    destino = d / blob.name
    if destino.exists():
        return destino
    try:
        d.mkdir(parents=True, exist_ok=True)
        os.link(blob, destino)
    except OSError:
        try:
            import shutil
            shutil.copy2(blob, destino)
        except OSError as e:
            logger.warning(
                "attachments: no pude materializar %s en la vista de %r "
                "(%r); el experto lo va a ver en el store plano",
                blob.name, scope, e)
            return blob
    return destino


def max_attachment_bytes() -> int:
    """Cap por archivo subido. Default 50MB (igual que voice)."""
    raw = os.environ.get("ATTACHMENT_MAX_BYTES", "")
    if raw.strip().isdigit():
        return int(raw.strip())
    return _MAX_FALLBACK


# id generado por store(): prefijo del SHA256 (16 hex). Lo que el bot
# guarda en metadata para reconstruir el path después. Corto y
# comparable contra el archivo.
_ID_LEN = 16
_ID_RE = re.compile(r"^att_[0-9a-f]{16}$")


def _sha256_id(buf: bytes) -> str:
    return "att_" + hashlib.sha256(buf).hexdigest()[:_ID_LEN]


def _safe_ext(filename: str, mimetype: Optional[str]) -> str:
    """Extensión normalizada en lowercase. Devuelve '' si no es derivable.

    El bot se autentica por Discord CDN y la URL no siempre trae la ext.
    Si no hay ext ni mimetype utilizable, guardamos SIN extensión — el
    bot lo trata como bytes opacos.

    Bloquea extensiones y mimetypes peligrosos (ejecutables, scripts).
    Devuelve None cuando hay que rechazar el upload entero — el handler
    HTTP traduce eso a 400.
    """
    # Chequeo de mimetype ANTES de la extensión: si Discord/CDN nos
    # miente con la ext pero el mime real dice "msdownload", frenamos.
    if mimetype:
        mt = mimetype.split(";")[0].strip().lower()
        if mt in _BLOCKED_MIMES:
            return None  # señal de rechazo
    if filename:
        ext = Path(filename).suffix.lower()
        if ext and len(ext) <= 8 and ext.startswith("."):
            # Sanear: solo [a-z0-9.], por si filename viene con mierda.
            if not re.match(r"^\.[a-z0-9.]+$", ext):
                return None
            if ext in _BLOCKED_EXTS:
                return None
            return ext
    if mimetype:
        # Mapeo chico — solo lo que realmente nos importa. El resto
        # queda sin ext y el LLM decide.
        mt = mimetype.split(";")[0].strip().lower()
        common = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "application/pdf": ".pdf",
            "text/plain": ".txt",
            "text/csv": ".csv",
            "text/markdown": ".md",
            "application/json": ".json",
            "application/zip": ".zip",
            "application/octet-stream": "",
        }
        return common.get(mt, "")
    return ""


def store(buf: bytes, *, filename: str = "",
          mimetype: Optional[str] = None) -> Tuple[str, Path, int]:
    """Guarda bytes en disco y devuelve (id, path, bytes).

    Idempotente: si el mismo buf ya está, devuelve el path existente
    (no duplica). NO valida el contenido — el bot es quien debe
    pasar MIME real. El cap lo aplica el caller (handler HTTP).

    Levanta ValueError si la extensión o mimetype es peligroso (.exe,
    .ps1, application/x-msdownload, etc). El handler HTTP traduce
    eso a 400.
    """
    if not buf:
        raise ValueError("buf vacío")
    ext = _safe_ext(filename, mimetype)
    if ext is None:
        # Mensaje seguro: NO le decimos al cliente qué categoría exacta
        # bloqueamos (le daría info al attacker sobre qué mimetype usar
        # para bypassear). Mensaje genérico.
        raise ValueError(
            f"tipo de archivo rechazado (filename={filename!r}, "
            f"mimetype={mimetype!r})")
    attach_id = _sha256_id(buf)
    adir = attachments_dir()
    target = adir / f"{attach_id}{ext}"
    if target.exists():
        logger.info("attachments: hit de cache %s (%d bytes)", attach_id, len(buf))
        return attach_id, target, len(buf)
    adir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(buf)
    logger.info("attachments: escrito %s (%d bytes, %s)", attach_id, len(buf), filename or mimetype or "?")
    return attach_id, target, len(buf)


def resolve(attach_id: str) -> Optional[Path]:
    """Devuelve la ruta absoluta si el id existe en disco. None si no."""
    if not attach_id or not _ID_RE.match(attach_id):
        return None
    adir = attachments_dir()
    # Probamos con cualquier ext (los archivos pueden estar con o sin).
    if adir.exists():
        for p in adir.glob(f"{attach_id}.*"):
            if p.is_file():
                return p
        # Sin ext.
        candidate = adir / attach_id
        if candidate.exists():
            return candidate
    return None


def generated_markdown(images: dict) -> str:
    """Capturas recibidas por MCP, entregadas aunque el modelo no cite su id."""
    return "\n\n".join(
        f"![Captura {i}](/attachments/{aid})\n[{path.name}](/attachments/{aid})"
        for i, aid in enumerate(images, 1) if (path := resolve(aid)) is not None)


# Extensiones cuyo contenido se inyecta INLINE al prompt. Todo lo
# demás (pdf, binarios) se lista con metadata honesta; las imágenes
# tienen su propio camino (_IMAGE_MIMES).
_TEXT_EXTS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".json", ".csv", ".tsv",
    ".log", ".xml", ".yaml", ".yml", ".html", ".htm", ".ini",
    ".toml", ".sql", ".tex",
})

# Imágenes que viajan al modelo como parte binaria del prompt
# (2026-07-31). Antes se listaban con "no puedes ver su contenido":
# era cierto para el experto de texto, pero MiniMax-M3 SÍ ve — medido
# con un PNG de bandas roja/verde, lo describió bien (228 tokens de
# entrada para 64x64). El formato lo decide la extensión guardada, que
# `store()` ya normaliza desde el mimetype real.
_IMAGE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _image_max() -> int:
    """Cap por imagen en bytes (env-tunable).

    No es cosmético: la imagen viaja en CADA request del run (igual que
    el resto del prompt), así que una foto de 8MB se paga en todas las
    vueltas. 5MB cubre cualquier screenshot y frena un video mal
    etiquetado como .gif.
    """
    raw = os.environ.get("FOURBIS_ATTACH_IMAGE_MAX", "")
    return int(raw) if raw.strip().isdigit() else 5 * 1024 * 1024


def image_mime(path: Path) -> Optional[str]:
    """media_type si el archivo es una imagen que el modelo puede ver."""
    return _IMAGE_MIMES.get(path.suffix.lower())


def load_images(attach_ids: list[str]) -> list[Tuple[bytes, str]]:
    """Ids → [(bytes, media_type)] de las imágenes que van al modelo.

    Best-effort y en el mismo orden que `format_user_block` los nombra:
    lo que no resuelve, no es imagen o excede el cap se omite acá y el
    bloque de texto lo explica. Nunca levanta: un adjunto roto no puede
    tumbar el run entero.
    """
    out: list[Tuple[bytes, str]] = []
    cap = _image_max()
    for aid in attach_ids or []:
        p = resolve(aid)
        if p is None:
            continue
        mime = image_mime(p)
        if mime is None:
            continue
        try:
            if p.stat().st_size > cap:
                logger.warning(
                    "load_images: %s pesa %d bytes (cap %d); no va al modelo",
                    p.name, p.stat().st_size, cap)
                continue
            out.append((p.read_bytes(), mime))
        except OSError as e:
            logger.warning("load_images: no pude leer %s (%r)", p, e)
    return out


def _inline_max() -> int:
    """Cap de chars inyectados por archivo (env-tunable)."""
    raw = os.environ.get("FOURBIS_ATTACH_INLINE_MAX", "")
    return int(raw) if raw.strip().isdigit() else 30000


def is_inlineable(filename_or_path: str) -> bool:
    """¿El contenido de este archivo se le puede mostrar al experto?

    True  → va INLINE al prompt (el experto lee el contenido real).
    False → binario: el experto solo ve nombre y tamaño, NO el contenido.

    El relay es la única autoridad sobre esto (`_TEXT_EXTS`). Lo
    exponemos para que el bot de Discord pueda avisarle al usuario en
    el momento del upload en vez de hacerlo esperar el run entero para
    que el LLM conteste "no puedo verlo" (iter 2026-07-22). Si el bot
    duplicara la lista, las dos se irían de sync al primer cambio.
    """
    return Path(filename_or_path).suffix.lower() in _TEXT_EXTS


def format_user_block(attach_ids: list[str], scope: str = "") -> str:
    """Bloque que se inyecta al `user` que se le pasa al LLM.

    Bug fix 2026-07-20 (reporte real: "los adjuntos no funcionan"):
    la versión anterior inyectaba solo el PATH del archivo esperando
    que el LLM lo leyera con read_file del wrapper — imposible por
    partida doble: (a) el path era relativo al cwd del RELAY (el LLM
    lo resolvía contra su repo y no existía), y (b) aunque fuera
    absoluto, el wrapper rechaza paths fuera de FOURBIS_WORKSPACE y
    los adjuntos viven en state/ del relay. Los mocks pasaban porque
    solo asserteaban el formato del bloque, nunca una lectura real
    (transcript un transcript de ejemplo: respuesta VACÍA).

    Ahora: los adjuntos de texto (.txt/.md/.json/...) van INLINE
    (contenido real, cap FOURBIS_ATTACH_INLINE_MAX=30000 chars por
    archivo — el caso "prompt largo que Discord sugiere mandar como
    .txt" queda idéntico a pegarlo). Los binarios (imágenes, pdf) se
    listan con nombre/tamaño y una nota honesta de que el experto de
    texto no puede verlos — mejor que silencio o alucinación.

    Si un id no resuelve a un archivo en disco, se loggea warning y
    se omite del bloque (mejor silencioso-omitido que "(no encontrado)"
    que el LLM podría tratar como contenido válido — bug fix 2026-07-18).
    """
    if not attach_ids:
        return ""
    lines = ["", "## Adjuntos"]
    resolved_count = 0
    cap = _inline_max()
    for aid in attach_ids:
        p = resolve(aid)
        if p is None:
            logger.warning(
                "format_user_block: attachment %r no resuelve en disco "
                "(FOURBIS_ATTACHMENTS_DIR=%s); omitiendo del bloque",
                aid, attachments_dir())
            continue
        resolved_count += 1
        p = p.resolve()
        if p.suffix.lower() in _TEXT_EXTS:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                logger.warning(
                    "format_user_block: no pude leer %s (%r); "
                    "omitiendo contenido", p, e)
                resolved_count -= 1
                continue
            if len(text) > cap:
                text = (text[:cap]
                        + f"\n…[TRUNCADO: el adjunto completo tiene "
                        f"{len(text)} chars]")
            # Fence de 4 backticks: aguanta ``` adentro del contenido.
            lines.append(f"### {p.name}")
            lines.append("````")
            lines.append(text)
            lines.append("````")
        elif image_mime(p) and p.stat().st_size <= _image_max():
            # La imagen va como parte binaria de ESTE mismo mensaje
            # (ver load_images); acá solo la nombramos para que el
            # modelo sepa cuál es cuál cuando hay varias.
            lines.append(
                f"- {p.name} ({p.stat().st_size} bytes) — imagen adjunta "
                "en este mensaje: puedes verla.")
        else:
            # 2026-08-26: el path, no una disculpa. La versión anterior
            # decía "no puedes ver su contenido; pídele al usuario que lo
            # mande como texto" — cierto cuando el experto no llegaba al
            # archivo, falso desde que el directorio de adjuntos entra
            # como raíz extra del sandbox (ver `Permisos.para` en
            # experts.py). Un .xlsx, un .pdf o un .zip los abre el
            # experto con las tools que ya tiene; lo único que le
            # faltaba era saber dónde está.
            # La ruta que se le nombra al experto es la de SU vista, no
            # la del store plano: es la única que su sandbox habilita.
            if scope:
                p = materializar(scope, p)
            size = p.stat().st_size
            extra = ""
            if image_mime(p):
                extra = (f" Es una imagen y excede el cap de "
                         f"{_image_max()} bytes, así que no viaja como "
                         "parte visual del mensaje, pero el archivo está "
                         "igual en esa ruta.")
            lines.append(
                f"- {p.name} ({size} bytes, {p.suffix.lower() or 'sin ext'})"
                f" — no viene inline, pero está en disco y tus tools "
                f"llegan: `{p}`. Ábrelo con `read_file` si resulta ser "
                "texto, o con `shell` si necesita una librería "
                "(Python con openpyxl para .xlsx, con pypdf para .pdf, "
                "`tar -xf` o Python con zipfile para .zip). Si vas a "
                "modificarlo, copia primero dentro del repo: el "
                "directorio de adjuntos no es tu área de trabajo."
                + extra)
    if resolved_count == 0:
        # Nada resolvió: devolver string vacío → el handler no inyecta
        # nada (back-compat: si los adjuntos estaban todos mal, NO
        # sumamos un bloque "## Adjuntos" vacío que el LLM malinterprete).
        logger.warning(
            "format_user_block: 0/%d attachments resolvieron; bloque omitido",
            len(attach_ids))
        return ""
    return "\n".join(lines)
