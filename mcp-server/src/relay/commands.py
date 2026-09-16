"""Comandos dinámicos — ADR-013.

Un comando = fila en la tabla `commands` (name → handler). El handler
es un string "modulo.funcion" que se resuelve con importlib. El bot
Discord lee GET /commands al arrancar y despacha con
POST /commands/{name}/run.

Ponytail: todos los handlers built-in viven en ESTE módulo (funciones
chicas), no un archivo por handler. Un handler devuelve un string (el
texto que el bot publica). Si algún handler necesita algo más rico,
se cambia el shape ahí — no antes.

Validación de args: si la fila trae args_schema (JSON Schema), se
valida con `jsonschema` (ya instalado como dep transitiva de mcp).
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("relay.commands")

BUILD_TIMEOUT_S = 600


@dataclass
class CommandContext:
    """Lo que un handler puede necesitar. Se arma en el server."""
    db: Any                      # relay.db.Database
    sessions: Any                # relay.sessions.SessionRegistry
    running: dict[str, asyncio.Task] = field(default_factory=dict)
    source: str = "api"
    author: str = ""


Handler = Callable[[dict, CommandContext], Awaitable[str]]


class UnknownCommand(KeyError):
    pass


class CommandRegistry:
    """Carga comandos desde la BD y despacha por nombre."""

    def __init__(self, db) -> None:
        self._db = db
        self._handlers: dict[str, tuple[Handler, dict]] = {}

    async def load_from_db(self) -> int:
        cmds = await self._db.list_commands(enabled_only=True)
        handlers: dict[str, tuple[Handler, dict]] = {}
        for cmd in cmds:
            try:
                module_path, func_name = cmd["handler"].rsplit(".", 1)
                mod = importlib.import_module(module_path)
                handlers[cmd["name"]] = (getattr(mod, func_name), cmd)
            except (ImportError, AttributeError, ValueError) as e:
                logger.warning("comando %r: handler %r inválido (%r), salteado",
                               cmd["name"], cmd["handler"], e)
        self._handlers = handlers
        return len(handlers)

    def names(self) -> list[str]:
        return sorted(self._handlers)

    async def dispatch(self, name: str, args: dict, ctx: CommandContext) -> str:
        if name not in self._handlers:
            # reload lazy: quizás lo agregaron por HTTP hace un momento
            await self.load_from_db()
        if name not in self._handlers:
            raise UnknownCommand(name)
        handler, cmd = self._handlers[name]

        from .execution_policy import ExecutionPolicy
        target = args.get("project") or args.get("target")
        project = await ctx.db.get_project(target) if isinstance(target, str) else None
        policy = ExecutionPolicy.for_run((project or {}).get("defaults_json") or {})
        if not policy.unrestricted_tools and handler not in {
                list_sessions, list_projects, show_logs, memoria, contexto, ayuda}:
            raise RuntimeError("este comando no está habilitado con los permisos del run")

        schema = cmd.get("args_schema")
        if schema:
            import jsonschema
            try:
                jsonschema.validate(args, schema)
            except jsonschema.ValidationError as e:
                raise ValueError(f"args inválidos: {e.message}") from e

        t0 = time.monotonic()
        try:
            result = await handler(args, ctx)
            status = "ok"
        except Exception as e:
            result = f"error: {e}"
            status = "error"
        duration_ms = int((time.monotonic() - t0) * 1000)
        await ctx.db.log_command(
            name=name, source=ctx.source, author=ctx.author, args=args,
            response=result, duration_ms=duration_ms, status=status,
        )
        if status == "error":
            raise RuntimeError(result)
        return result


# ======================================================================
# Handlers built-in
# ======================================================================


async def list_sessions(args: dict, ctx: CommandContext) -> str:
    """Lista sesiones VS Code handshakeadas (registro en memoria, ADR-009).

    El push por SSE ya no se ejerce (2026-08-10): la extensión solo
    handshakea, así que "activa" = handshake vivo en este proceso.
    Cuando el relay reinicia, todas caen (no hay persistencia).
    """
    items = await ctx.sessions.list_sessions()
    if not items:
        return "Sin sesiones VS Code handshakeadas en este proceso."
    lines = []
    for s in items:
        target = s.get("last_target") or "(sin workspace)"
        ts = s.get("last_handshake_ts") or "?"
        lines.append(f"• {s['name']} → {target} — handshake {ts}")
    return "\n".join(lines)


async def list_projects(args: dict, ctx: CommandContext) -> str:
    """Lista proyectos registrados en SQLite con sus rutas."""
    projects = await ctx.db.list_projects(enabled_only=False)
    if not projects:
        return ("Sin proyectos. Creá uno desde la Admin UI "
                "(/admin/, tab Proyectos) o con POST /projects.")
    return "\n".join(
        f"• {p['slug']} — {p['name']} ({p['repo_path']})"
        f"{'' if p['enabled'] else ' [deshabilitado]'}"
        for p in projects
    )


async def show_logs(args: dict, ctx: CommandContext) -> str:
    """Últimos chats de un target/proyecto (índice SQLite)."""
    name = args.get("name")
    limit = int(args.get("limit", 10))
    chats = await ctx.db.list_chats(project_slug=name, limit=limit)
    if not chats:
        return f"Sin chats registrados{f' para {name!r}' if name else ''}."
    lines = []
    for c in chats:
        dur = ""
        if c.get("finished_at"):
            dur = f" [{c['status']}]"
        else:
            dur = " [running]"
        lines.append(f"• {c['started_at']} {c['target'] or c['project_slug']}"
                     f" ({c['source']}){dur} — {c['id'][:8]}")
    return "\n".join(lines)


async def cancel_chat(args: dict, ctx: CommandContext) -> str:
    """Cancela un chat de experto en curso (mata el asyncio.Task)."""
    chat_id = args["chat"]
    # match por prefijo: el usuario ve ids cortos en `logs`
    matches = [cid for cid in ctx.running if cid.startswith(chat_id)]
    if not matches:
        return f"No hay chat en curso que empiece con {chat_id!r}."
    for cid in matches:
        ctx.running[cid].cancel()
        await ctx.db.finish_chat(cid, status="cancelled")
    return f"Cancelado(s): {', '.join(c[:8] for c in matches)}."


# ---- memoria de largo plazo (ADR-027: retrieval MANUAL) ----


async def memoria(args: dict, ctx: CommandContext) -> str:
    """Busca resúmenes de conversaciones previas de un proyecto (FTS5).

    Sin query: devuelve los resúmenes más recientes. Scopeado por
    proyecto — la memoria de INVENTORYDEMO no se filtra a SampleApp.
    """
    target = args["target"]
    query = args.get("query", "") or ""
    project = await ctx.db.get_project(target)
    if project is None or not project["enabled"]:
        return f"proyecto {target!r} no existe o está deshabilitado"
    hits = await ctx.db.search_memories(project["slug"], query, limit=5)
    from .memory import format_memory_hits
    text = format_memory_hits(hits)
    if not text:
        suffix = f" para {query!r}" if query else ""
        return f"Sin memoria previa de {project['slug']}{suffix}."
    return f"Memoria de {project['slug']}:\n{text}"


async def fact(args: dict, ctx: CommandContext) -> str:
    """Lista los hechos atómicos destilados de un proyecto (ADR-026)."""
    target = args["target"]
    project = await ctx.db.get_project(target)
    if project is None or not project["enabled"]:
        return f"proyecto {target!r} no existe o está deshabilitado"
    facts = await ctx.db.list_facts(project["slug"], limit=50)
    if not facts:
        return f"Sin hechos registrados para {project['slug']}."
    lines = [f"• {f['fact']}" for f in facts]
    return f"Hechos de {project['slug']}:\n" + "\n".join(lines)


# ---- contexto del hilo (2026-07-22) ----
# Un hilo largo se arrastra hasta el timeout aunque no esté colgado: el
# historial replayado se come la ventana. `contexto` lo mide y
# `compactar` lo baja sin cerrar el hilo (misma rama, mismo thread).


async def _open_conversation(target: str, ctx: CommandContext) -> dict:
    project = await ctx.db.get_project(target)
    if project is None or not project["enabled"]:
        raise ValueError(f"proyecto {target!r} no existe o está deshabilitado")
    conv = await ctx.db.get_open_conversation_for_project(project["slug"])
    if conv is None:
        raise ValueError(f"no hay conversación abierta en {project['slug']} "
                         "(abre una con /nuevo)")
    return conv


async def contexto(args: dict, ctx: CommandContext) -> str:
    """Cuánto de la ventana de contexto ocupa el hilo abierto del proyecto."""
    from .experts import context_usage_db
    conv = await _open_conversation(args["target"], ctx)
    usage = await context_usage_db(ctx.db, conv.get("messages_json") or "")
    if usage is None:
        return (f"Hilo {conv['id'][:8]}… todavía sin contexto medible "
                "(sin runs, o recién compactado).")
    k = lambda n: f"{n / 1000:.0f}k"  # noqa: E731
    tip = ("\n⚠️ Poco margen para trabajar: `compactar` baja el contexto "
           "sin cerrar el hilo." if usage["hot"] else "")
    # El pico primero: es el número que decide si hay que compactar. La
    # base es lo que pesaba el hilo ANTES del último run (ver el medidor
    # en tab-chats.js).
    aprox = "" if usage.get("limit_medido") else "~"
    return (f"Hilo {conv['id'][:8]}… — pico del último run "
            f"{k(usage['peak_tokens'])}/{k(usage['limit'])} "
            f"({aprox}{usage['peak_pct']}%), el hilo arranca en "
            f"{k(usage['base_tokens'])} ({aprox}{usage['pct']}%).{tip}")


async def compactar(args: dict, ctx: CommandContext) -> str:
    """Destila el hilo abierto a un resumen y lo usa como historial.

    El hilo sigue vivo: misma conversación, misma rama, mismo thread.
    Solo se libera la ventana de contexto.
    """
    from .server import compact_live_conversation
    conv = await _open_conversation(args["target"], ctx)
    if not (conv.get("messages_json") or "").strip():
        return f"Hilo {conv['id'][:8]}… sin historial que compactar."
    runs = await ctx.db.list_chats(status="running", limit=50)
    if any(c.get("conversation_id") == conv["id"] for c in runs):
        return ("Hay un run en curso en este hilo — espera a que termine "
                "(o `cancel`) y compacta después.")
    out = await compact_live_conversation(ctx.db, conv)
    if not out["ok"]:
        return f"No pude compactar: {out['error']}"
    before = out.get("before")
    antes = (f"{before['base_tokens'] / 1000:.0f}k tokens ({before['pct']}%)"
             if before else f"{out['chars_before']} chars")
    return (f"Hilo {conv['id'][:8]}… compactado ✓ — venía de {antes}, "
            f"ahora arranca de un resumen ({out['chars_after']} chars, "
            f"{out['facts']} hechos guardados). Sigue en el mismo hilo.")


# ---- atajos 4bis (skill 4bis-shortcuts): dotnet build/test ----
# Ponytail: dotnet build/test es determinístico — subprocess directo,
# sin LLM en el medio. El experto LLM queda para preguntas de verdad.


_ERROR_LINE_RE = re.compile(r"error [A-Z]+\d+|error MSB\d+|: error ", re.IGNORECASE)
_TEST_SUMMARY_RE = re.compile(
    r"(Passed!|Failed!|con error|Passed:|Failed:|Skipped:|Total:|total:|failed:|succeeded:)",
)


async def _run_dotnet(verb: str, repo_path: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "dotnet", verb, cwd=repo_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=BUILD_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"dotnet {verb} superó {BUILD_TIMEOUT_S}s y fue matado."
    return proc.returncode or 0, out_b.decode("utf-8", errors="replace")


async def _project_path(args: dict, ctx: CommandContext) -> tuple[str, str]:
    slug = args["project"]
    project = await ctx.db.get_project(slug)
    if project is None or not project["enabled"]:
        raise ValueError(f"proyecto {slug!r} no existe o está deshabilitado")
    return slug, project["repo_path"]


async def build(args: dict, ctx: CommandContext) -> str:
    """dotnet build en el repo del proyecto. Reporta SOLO errores o 'build OK'."""
    slug, repo_path = await _project_path(args, ctx)
    code, out = await _run_dotnet("build", repo_path)
    if code == 0:
        return f"build OK ({slug})"
    errors = sorted({l.strip() for l in out.splitlines() if _ERROR_LINE_RE.search(l)})
    body = "\n".join(errors[:20]) or out[-2000:]
    return f"build FALLÓ ({slug}, exit {code}):\n{body}"


async def test(args: dict, ctx: CommandContext) -> str:
    """dotnet test en el repo del proyecto. Resume pasados/fallados."""
    slug, repo_path = await _project_path(args, ctx)
    code, out = await _run_dotnet("test", repo_path)
    summary = [l.strip() for l in out.splitlines() if _TEST_SUMMARY_RE.search(l)]
    body = "\n".join(summary[-15:]) or out[-2000:]
    prefix = "tests OK" if code == 0 else f"tests FALLARON (exit {code})"
    return f"{prefix} ({slug}):\n{body}"


async def build_test(args: dict, ctx: CommandContext) -> str:
    """build y después test. Si build falla, NO corre tests (regla del skill)."""
    result = await build(args, ctx)
    if "FALLÓ" in result:
        return result
    return result + "\n" + await test(args, ctx)


async def ayuda(args: dict, ctx: CommandContext) -> str:
    """Comandos disponibles + flags que entienden los mensajes al experto.

    Todo sale de la DB (commands + mcp_servers): si mañana pasás un MCP
    a on-demand o agregás un comando, este texto se actualiza solo. Un
    help hardcodeado miente a la semana.
    """
    cmds = await ctx.db.list_commands(enabled_only=True)
    bloques = ["**Comandos** (prefijo `!`)"]
    bloques += [f"• `!{c['name']}` — {c['description']}"
                for c in cmds if c["name"] != "ayuda"]

    mcps = await ctx.db.list_mcp_servers(enabled_only=True)
    pedibles = [m for m in mcps if m["on_demand"]]
    adjuntos = [m for m in mcps if not m["on_demand"]]

    bloques.append("")
    bloques.append("**Flags del mensaje** (en cualquier parte del texto)")
    if pedibles:
        # El selector matchea capability O name. Una capability con dos
        # MCPs adjunta UNO solo (ver _one_per_capability en experts.py);
        # marcamos cuál es el default para que se vea antes de escribir
        # el flag. Hoy ninguna capacidad tiene dos (el browser quedó en
        # `playwright-mcp` solo), pero el caso sigue contemplado.
        por_cap: dict[str, list[str]] = {}
        for m in pedibles:
            por_cap.setdefault(m["capability"], []).append(m["name"])
        # Mismo criterio de desempate que _one_per_capability.
        terminos = " · ".join(
            f"`{cap}` → {', '.join(sorted(nombres))}"
            + (" (default: el primero)" if len(nombres) > 1 else "")
            for cap, nombres in sorted(por_cap.items()))
        bloques.append(
            "• `--con <capacidad>` — adjunta un MCP on-demand desde el "
            "primer turno. Alias: `--with`. Varias con coma: "
            "`--con browser,github`.")
        bloques.append(f"    Disponibles: {terminos}.")
        if any(len(v) > 1 for v in por_cap.values()):
            bloques.append(
                "    Ojo: una capacidad con varios MCPs adjunta solo uno "
                "(declaran las mismas tools). Para elegir otro, usa el "
                "nombre exacto (ej. `--con playwright-mcp`).")
        bloques.append(
            "    Sin el flag el experto igual puede pedirlas solo con "
            "`use_capability`, pero pierde una vuelta del run.")
    if adjuntos:
        bloques.append(
            "    Ya adjuntas siempre (no hace falta pedirlas): "
            + ", ".join(f"{m['name']} ({m['capability']})" for m in adjuntos)
            + ".")

    # Skills: las `manual` (frontmatter `when: manual`) NO se auto-inyectan
    # — se activan con `--skill`. Las `auto` ya están siempre. Mismo patrón
    # que --con para MCPs; sale de disco (no de la DB) igual que el run.
    from . import skills as _skills_mod
    installed = await asyncio.to_thread(
        _skills_mod.list_skills_sync, _skills_mod.resolve_skills_dir())
    manual = [s for s in installed if s["enabled"] and s["manual"]]
    auto = [s for s in installed if s["enabled"] and not s["manual"]]
    if manual:
        nombres = " · ".join(f"`{s['name']}`" for s in manual)
        bloques.append(
            "• `--skill <nombre>` — activa una skill `manual` en el run "
            "(no se auto-inyectan). Alias: `--skills`. Varias con coma: "
            "`--skill pdf,docx`.")
        bloques.append(f"    Disponibles: {nombres}.")
    if auto:
        bloques.append(
            "    Ya activas siempre (auto, no hace falta pedirlas): "
            + ", ".join(s["name"] for s in auto) + ".")

    bloques.append(
        "• `--solo <glob>` — acota la atención del experto a esos paths. "
        "Alias: `--scope`, `--only`. Ej: `--solo src/auth/*`.")
    return "\n".join(bloques)
