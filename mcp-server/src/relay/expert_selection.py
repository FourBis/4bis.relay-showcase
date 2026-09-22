"""Selección explícita de MCP, skills y permisos del experto."""
from __future__ import annotations
import logging
import re
from pydantic_ai import Tool
from typing import Any, Optional
from . import expert_toolsets, mcp_pool as mcp_pool_mod

logger = logging.getLogger("relay.experts")


_MCP_FLAG_RE = re.compile(r"(?:^|\s)--(?:con|with)[ =]([\w,\-]+)", re.IGNORECASE)


def parse_mcp_flags(text: str) -> tuple[str, list[str]]:
    """Extrae `--con db` / `--with db,docs` del mensaje del usuario.

    Devuelve (texto limpio, selección). La selección son términos que el
    selector F0 matchea contra capability O name (case-insensitive).
    Varios flags se acumulan.
    """
    selection: list[str] = []
    def _grab(m: re.Match) -> str:
        selection.extend(
            t.strip() for t in m.group(1).split(",") if t.strip())
        return ""
    clean = _MCP_FLAG_RE.sub(_grab, text).strip()
    return clean, selection


# Selección explícita de skills: `--skill pdf` / `--skills pdf,docx`.
# Paralelo a `--con` para MCPs: fuerza una skill al run aunque tenga
# `when: manual` (que si no NO se auto-inyecta). El bloque lo arma
# skills.render_requested_block; el `!ayuda` lista las disponibles.
_SKILL_FLAG_RE = re.compile(r"(?:^|\s)--skills?[ =]([\w,\-]+)", re.IGNORECASE)


def parse_skill_flags(text: str) -> tuple[str, list[str]]:
    """Extrae `--skill pdf` / `--skills pdf,docx` del mensaje del usuario.

    Devuelve (texto limpio, nombres pedidos). Mismos alias-y-coma que
    parse_mcp_flags; los nombres se resuelven contra el dir o el `name`
    del frontmatter (case-insensitive) en skills.render_requested_block.
    """
    names: list[str] = []
    def _grab(m: re.Match) -> str:
        names.extend(t.strip() for t in m.group(1).split(",") if t.strip())
        return ""
    clean = _SKILL_FLAG_RE.sub(_grab, text).strip()
    return clean, names


# Working set scope (2026-07-20d): `--solo src/auth/*` / `--scope a,b`
# acota la atención del experto a esos paths. El valor es un token
# sin espacios (glob(s) separados por coma). Sin espacios porque el
# resto del mensaje es prosa — un glob con espacios sería ambiguo.
_SCOPE_FLAG_RE = re.compile(
    r"(?:^|\s)--(?:solo|scope|only)[ =](\S+)", re.IGNORECASE)


def parse_scope_flags(text: str) -> tuple[str, list[str]]:
    """Extrae `--solo <glob[,glob]>` del mensaje. Devuelve (texto
    limpio, lista de paths/globs). Varios flags se acumulan."""
    scopes: list[str] = []
    def _grab(m: re.Match) -> str:
        scopes.extend(
            t.strip() for t in m.group(1).split(",") if t.strip())
        return ""
    clean = _SCOPE_FLAG_RE.sub(_grab, text).strip()
    return clean, scopes


def scope_block(scopes: list[str]) -> str:
    """Bloque de instrucciones para un run acotado a `scopes`."""
    paths = ", ".join(f"`{s}`" for s in scopes)
    return (
        "## Working set acotado (pedido EXPLÍCITO del usuario)\n"
        f"Limita tu atención a estos paths: {paths}. Ignora el resto del "
        "repo salvo que sea imprescindible para entender esos archivos. "
        "NO edites archivos fuera de ese alcance; si crees que hace falta, "
        "explica por qué en vez de hacerlo."
    )


class _CapabilityRequested(Exception):
    """El LLM pidió una capacidad vía use_capability → re-run con ella."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _one_per_capability(
    rows: list[dict], selection: set[str] | None = None,
) -> list[dict]:
    """Deja UN MCP por capacidad. Devuelve las filas que se adjuntan.

    Bug fix 2026-08-01: una capacidad puede resolver a varios MCPs
    (entonces `browser` = obscura + playwright-mcp) y `--con browser` los
    adjuntaba a los dos. Como ambos declaraban las mismas tools,
    `CombinedToolset.get_tools` aborta el run entero con
    `UserError: ... defines a tool whose name conflicts with existing
    tool ...: 'browser_close'` — el experto muere antes de escribir una
    línea. Y aunque los nombres no chocaran, dos implementaciones de lo
    mismo pagan dos veces el catálogo de tools en tokens.

    Criterio de desempate, en orden:

    1. El always-on (on_demand=0): el sistema lo da por adjunto siempre,
       no lo puede desplazar un on-demand.
    2. El pedido por NOMBRE exacto en `selection`. Importa en el re-run
       de use_capability: si el humano arrancó con `--con playwright-mcp`
       y después el modelo pide `use_capability("browser")`, la selección
       pasa a ser {playwright-mcp, browser} y sin esto el desempate
       alfabético le cambiaba el MCP a mitad del run.
    3. El primero alfabético.

    2026-08-16: `browser` volvió a resolver a UN solo MCP
    (`playwright-mcp`); obscura salió del catálogo. La función se queda
    igual: el desempate sigue haciendo falta para cualquier capacidad que
    algún día vuelva a tener dos implementaciones, y es lo que evita que
    el run muera con `UserError: ... name conflicts`.
    """
    sel = {s.lower() for s in (selection or set())}
    best: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: (
            r.get("on_demand", 1),
            0 if (r.get("name") or "").lower() in sel else 1,
            r.get("name") or "")):
        cap = row.get("capability") or row.get("name") or ""
        if cap in best:
            logger.info(
                "mcp %r: no se adjunta, %r ya cubre la capacidad %r "
                "(pídelo por nombre exacto para usar este)",
                row.get("name"), best[cap].get("name"), cap)
            continue
        best[cap] = row
    return list(best.values())


async def _catalog_toolsets(
    db: Any, project: dict, selection: set[str], pool: Any,
    tool_call_timeout: Optional[float] = None,
    inflight: Optional[dict] = None,
    hide_tools: frozenset = frozenset(),
    image_artifacts: Optional[dict] = None,
    vision: bool = True,
) -> tuple[list[Any], list[dict], list[dict]]:
    """Arma los toolsets del run desde el catálogo F0.

    Devuelve (toolsets, adjuntados, visibles): adjuntados = filas cuyos
    toolsets efectivamente entraron; visibles = TODOS los MCPs
    habilitados del proyecto (para el menú de use_capability). TODOS los
    stdio van por el pool (spin-up con probe + reaper); http/sse se arman
    por run. Un MCP que no levanta se saltea (degradación limpia) y su
    health queda en la DB para la Admin UI.

    2026-07-26: el pool era solo para los `on_demand`. Como en la práctica
    todas las filas son always-on (on_demand=0), NINGUNA pasaba por el
    probe: el primer contacto con el subprocess era el `__aenter__` del
    agente, y un `npx -y`/`uvx` que no completa el handshake mataba el run
    (RuntimeError de fastmcp). Poolearlos también les da proceso vivo
    entre runs, que es justo lo que necesitan los que tardan en arrancar.
    """
    repo_path = project["repo_path"]
    sel = list(selection) or None
    rows = _one_per_capability(await db.mcp_servers_for_project(
        project["id"], names=sel, capabilities=sel), selection)
    visible = await db.mcp_servers_for_project(project["id"], all_visible=True)

    toolsets: list[Any] = []
    attached: list[dict] = []
    for row in rows:
        try:
            runtime_row = {**row, "_tool_call_timeout_s": tool_call_timeout}
            source_repo = project.get("_task_source_repo")
            if source_repo:
                # Los stdio que reciben la raíz por args se pueden reubicar.
                # Un servicio HTTP repo-aware no tiene canal por corrida para
                # cambiarla: se omite antes que exponer el checkout original.
                repo_cap = (row.get("capability") or "").lower() in {
                    "code", "filesystem", "files", "git", "repo",
                }
                if row.get("transport") in ("http", "sse") and repo_cap:
                    logger.warning(
                        "mcp %r omitido: no puede aislar root por tarea",
                        row.get("name"))
                    continue
                source_forms = {str(source_repo), str(source_repo).replace("\\", "/")}

                def _retarget(value):
                    if not isinstance(value, str):
                        return value
                    for old in source_forms:
                        value = value.replace(old, repo_path)
                    return value

                runtime_row["args"] = [_retarget(a) for a in (row.get("args") or [])]
                runtime_row["env"] = {
                    key: _retarget(value)
                    for key, value in (row.get("env") or {}).items()
                }
            if pool is not None and row["transport"] == "stdio":
                toolset = await pool.acquire(runtime_row, repo_path)
                if toolset is None:  # no levantó — queda visible en la UI
                    if row.get("health") != "handshake_failed":
                        await db.upsert_mcp_server(
                            {"name": row["name"],
                             "health": "handshake_failed"})
                    continue
                if row.get("health") != "ok":
                    await db.upsert_mcp_server(
                        {"name": row["name"], "health": "ok"})
            else:
                toolset, _ = mcp_pool_mod.make_toolset(
                    runtime_row, repo_path, keep_alive=False)
            # Cap/elisión (ahorro de tokens) + guard de arranque. Se
            # envuelve ACÁ y no en run_expert porque el re-run de
            # use_capability reasigna la lista desde esta misma función:
            # envolver allá dejaba los toolsets del segundo round crudos.
            capped = expert_toolsets.CappedToolset(
                wrapped=toolset, timeout=tool_call_timeout,
                image_artifacts=image_artifacts, vision=vision,
                inflight=inflight if inflight is not None else {})
            if hide_tools:
                capped = expert_toolsets.CappedToolset(
                    wrapped=expert_toolsets.HideToolsToolset(
                        wrapped=toolset, hidden=hide_tools),
                    image_artifacts=image_artifacts, vision=vision,
                    timeout=tool_call_timeout,
                    inflight=inflight if inflight is not None else {})
            toolsets.append(expert_toolsets.OptionalToolset(wrapped=capped))
            attached.append(row)
        except mcp_pool_mod.McpConfigBusy:
            logger.warning("mcp %r: configuración pendiente, no es un fallo de health", row["name"])
        except Exception as e:  # noqa: BLE001
            logger.warning("mcp %r: no se pudo armar (%r), salteando",
                           row["name"], e)
    return toolsets, attached, visible


def make_use_capability(attached_mcps, visible_mcps) -> Optional[Tool]:
    """Meta-tool F1: el LLM pide una capacidad on-demand y el run se
    re-corre con ese toolset adjunto (plan §4.1). Solo existe si hay
    MCPs on-demand visibles que aún no están adjuntos."""
    attached_terms = set()
    for m in attached_mcps:
        attached_terms.add(m["name"].lower())
        attached_terms.add(m["capability"].lower())
    pending = [
        m for m in visible_mcps
        if m["on_demand"] and m["name"].lower() not in attached_terms]
    if not pending:
        return None
    by_term = set()
    for m in pending:
        by_term.add(m["name"].lower())
        by_term.add(m["capability"].lower())
    menu = ", ".join(sorted(
        f"{m['name']} (capability: {m['capability']})" for m in pending))

    async def use_capability(name: str) -> str:
        term = (name or "").strip().lower()
        if term in attached_terms:
            return f"{name!r} ya está activa en este run."
        if term in by_term:
            raise _CapabilityRequested(term)
        return (f"No existe la capacidad {name!r}. "
                f"Disponibles: {menu}")

    use_capability.__doc__ = (
        "Activa una capacidad externa (MCP on-demand) para esta "
        "conversación. El run se reinicia con las tools de esa "
        f"capacidad disponibles. Disponibles: {menu}.\n\n"
        "Args:\n"
        "    name: nombre del MCP o de la capability (ej. 'db').")
    return Tool(use_capability, takes_ctx=False)
