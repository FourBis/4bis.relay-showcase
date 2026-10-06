"""Deriva una tarea de codificación menor al experto del 4bis.relay.

Usa el modelo configurado en Relay para trabajar sobre un repositorio. Flujo: POST /experts/run (202) -> poll status hasta
finished -> lee el .md del chat y devuelve la sección `## Respuesta`.

Uso:
    python relay_delegate.py <target-slug> "<tarea>" [--model p:m] [--timeout 600]
    python relay_delegate.py --list          # lista los slugs de proyectos
    python relay_delegate.py --selftest       # check runnable, sin red
    python relay_delegate.py --resume <chat_id>  # recupera sin lanzar otro run
    python relay_delegate.py <slug> "<tarea>" --max-tools 8  # corta trabajo menor que se expande

El relay tiene que estar corriendo en http://127.0.0.1:8413.
Autenticación explícita por entorno: RELAY_SESSION_TOKEN (sesión nativa)
y RELAY_CLIENT_API_KEY (X-Relay-Key, si el servidor lo requiere).

Fuente versionada: 4bis.relay/scripts/relay_delegate.py.
ponytail: sin deps (urllib stdlib), sin cache ni reenvios de POST.
Ceiling: poll fijo cada 3s; para runs >timeout devuelve el chat_id para
que lo recuperes a mano con GET /chats/{id}. Upgrade: SSE en vez de poll.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://127.0.0.1:8413"
POLL_S = 3


def _req(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    session = os.environ.get("RELAY_SESSION_TOKEN", "")
    api_key = os.environ.get("RELAY_CLIENT_API_KEY", "")
    if session and not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        raise ValueError("RELAY_SESSION_TOKEN debe contener solo el token de sesión válido")
    if api_key and any(not 32 <= ord(char) <= 126 for char in api_key):
        raise ValueError("RELAY_CLIENT_API_KEY debe contener solo caracteres ASCII imprimibles")
    # Las credenciales pertenecen a esta petición, nunca a sus redirecciones.
    if session:
        req.add_unredirected_header("Cookie", "relay-session=" + session)
    if api_key:
        req.add_unredirected_header("X-Relay-Key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


# Los ÚNICOS headings que abren sección en un .md de chat. Set cerrado,
# no heurística: es el mismo literal que escribe el relay
# (`relay.persist._MD_SECTIONS`, fuente de verdad). Cualquier otro `## `
# es CONTENIDO — el experto escribe markdown y sus títulos entran ahí.
_MD_SECTIONS_RE = r"^##[ \t]+(?:Usuario|Respuesta|Error)[ \t]*$"


def _extract_respuesta(md_text):
    """Saca el cuerpo de la sección `## Respuesta` de un .md de chat.

    Corta en el próximo heading DE SECCIÓN o fin de archivo. Devuelve ""
    si no hay sección (run que no dejó respuesta).
    """
    m = re.search(r"^##[ \t]+Respuesta[ \t]*$(.*?)(?=" + _MD_SECTIONS_RE +
                  r"|\Z)", md_text, re.MULTILINE | re.DOTALL)
    if not m:
        return ""
    answer = m.group(1).strip()
    before_journal = answer.split("\n\n## Bitácora de la corrida\n", 1)[0].strip()
    return "" if before_journal == "(sin contenido)" else answer


def _selftest():
    # Los títulos del experto forman parte de la respuesta.
    md = ("---\nid: x\n---\n\n## Usuario\n\nhola\n\n"
          "## Respuesta\n\nlisto, hecho\n\n## Meta\n\nignora")
    assert _extract_respuesta(md) == "listo, hecho\n\n## Meta\n\nignora", \
        _extract_respuesta(md)
    md_err = "## Respuesta\n\nlisto\n\n## Error\n\nboom"
    assert _extract_respuesta(md_err) == "listo", _extract_respuesta(md_err)
    assert _extract_respuesta("## Usuario\n\nsolo user") == ""
    md = "## Usuario\n\nx\n\n## Respuesta\n\n## Resultado\n\n271 tests OK\n"
    assert "271 tests OK" in _extract_respuesta(md), repr(_extract_respuesta(md))
    # …pero un `## Usuario` de un turno posterior SÍ tiene que cortar.
    md2 = "## Respuesta\n\nprimera\n\n## Usuario\n\nsegunda pregunta\n"
    assert _extract_respuesta(md2) == "primera", repr(_extract_respuesta(md2))
    # La salida debe admitir Unicode incluso desde una consola cp1252.
    print("encoding: ✅ → ≠ │ … áéíóú")
    print("selftest OK")


def delegate(target=None, task=None, model=None, timeout=600, *,
             chat_id=None, source="cli", author="", max_tools=None):
    if max_tools is not None and max_tools <= 0:
        raise ValueError("max_tools debe ser positivo")
    body = {"target": target, "user": task, "source": source, "author": author}
    if model:
        body["model"] = model
    try:
        run = {"id": chat_id} if chat_id else _req("POST", "/experts/run", body)
    except urllib.error.HTTPError as e:
        print(f"error HTTP {e.code} al lanzar el run", file=sys.stderr)
        if e.code in (401, 403):
            print("revisa la sesión y los permisos del usuario", file=sys.stderr)
        if e.code == 404:
            print("  (usa --list para ver los slugs válidos)", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError):
        print("error: el relay no responde en " + BASE +
              " (¿está corriendo start.ps1?)", file=sys.stderr)
        return 2

    if isinstance(run, dict) and run.get("command") and isinstance(run.get("text"), str):
        print(run["text"])
        if run.get("ok") is True:
            return 0
        if run.get("ok") is False:
            return 1
        print("estado no verificable; no se reintenta el comando", file=sys.stderr)
        return 2
    if not isinstance(run, dict) or not isinstance(run.get("id"), str) or not run["id"]:
        print("respuesta de Relay sin identificador válido; no se reintenta el envío", file=sys.stderr)
        return 2
    chat_id = run["id"]
    resume = f"--resume {chat_id}" + (f" --max-tools {max_tools}" if max_tools is not None else "")
    print(f"[delegado a relay] chat_id={chat_id} target={target} "
          f"model={model or 'default'}", file=sys.stderr)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(min(POLL_S, max(0, deadline - time.monotonic())))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            st = _req("GET", f"/experts/status/{chat_id}", timeout=min(10, remaining))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break  # Puede estar persistido aunque ya no quede en memoria.
            if e.code < 500:
                print(f"error HTTP {e.code}; recuperar con {resume}", file=sys.stderr)
                return 2
            continue
        except (urllib.error.URLError, TimeoutError):
            print(f"estado temporalmente inaccesible; conservo chat_id={chat_id}", file=sys.stderr)
            continue
        phase = st.get("phase")
        tool = st.get("last_tool")
        print(f"  … {st.get('elapsed_s','?')}s phase={phase} tool={tool} "
              f"tools={st.get('tool_calls',0)}", file=sys.stderr)
        if st.get("finished"):
            break
        # ponytail: umbral observado cada 3s, no un tope estricto de gasto.
        # Para impedir incluso una llamada extra, el límite debe vivir en el runner.
        if max_tools is not None and st.get("tool_calls", 0) >= max_tools:
            print(f"umbral de {max_tools} tools; solicito cancelar solo chat_id={chat_id}. "
                  "Conservar y revisar el diff parcial.", file=sys.stderr)
            try:
                _req("POST", f"/experts/cancel/{chat_id}")
            except (urllib.error.URLError, TimeoutError):
                print(f"cancelación no confirmada; revisar {resume}", file=sys.stderr)
                return 2
            break

    try:
        chat = _req("GET", f"/chats/{chat_id}")
    except (urllib.error.URLError, TimeoutError):
        print(f"no pude recuperar el resultado; usar {resume}", file=sys.stderr)
        return 2
    if chat.get("status") in ("queued", "running"):
        print(f"run sigue activo; usar {resume}. No se relanzó.", file=sys.stderr)
        return 2
    if chat.get("status") == "error":
        print(f"error del experto: {chat.get('error')}", file=sys.stderr)
        return 1
    md_path = chat.get("md_path")
    answer = ""
    try:
        if md_path:
            answer = _extract_respuesta(Path(md_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        print(f"respuesta no disponible en disco; revisar chat_id={chat_id}", file=sys.stderr)
    print(answer or "(sin respuesta; revisa el chat)")
    print(f"\n---\n[relay] tokens_in={chat.get('tokens_in')} "
          f"tokens_out={chat.get('tokens_out')} "
          f"tool_calls={chat.get('tool_calls')} "
          f"model={chat.get('model')} status={chat.get('status')} "
          f"chat_id={chat_id}", file=sys.stderr)
    stages = chat.get("stages") or {}
    incomplete = stages.get("verifier_verdict") in ("needs_more", "needs_human", "off_plan") \
        or chat.get("phase_at_end") == "no_final_text"
    return 0 if chat.get("status") == "ok" and answer and not incomplete else 1


def main():
    # Configurar ambos streams cubre respuesta, progreso y listado.
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", help="slug del proyecto (--list)")
    ap.add_argument("task", nargs="?", help="la tarea para el experto")
    ap.add_argument("--model", help="override provider:modelo")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--max-tools", type=int, help="solicita cancelar este run al observar el umbral")
    ap.add_argument("--resume", help="chat_id existente; no crea otro run")
    ap.add_argument("--source", default="cli", help="origen real del pedido, ej codex")
    ap.add_argument("--author", default="", help="autor real, si se conoce")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        _selftest()
        return 0
    if a.list:
        projs = _req("GET", "/admin/api/projects")
        projs = projs if isinstance(projs, list) else projs.get("projects", [])
        for p in projs:
            print(f"{p['slug']:40s} {p.get('name','')}")
        return 0
    if a.timeout <= 0:
        ap.error("--timeout debe ser positivo")
    if a.max_tools is not None and a.max_tools <= 0:
        ap.error("--max-tools debe ser positivo")
    if a.resume and (a.target or a.task or a.model):
        ap.error("--resume no admite target, task ni --model")
    if not a.resume and (not a.target or not a.task):
        ap.error("faltan <target> y <tarea> (o usa --list / --selftest)")
    return delegate(a.target, a.task, a.model, a.timeout, chat_id=a.resume,
                    source=a.source, author=a.author, max_tools=a.max_tools)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
    except (urllib.error.URLError, TimeoutError):
        print("error: no se pudo completar la petición a Relay; revisa la conexión y la sesión", file=sys.stderr)
        sys.exit(2)
