"""Repositorios de proyectos, comandos y catálogo MCP."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .db_support import _COMMAND_COLS, _MCP_COLS, _PROJECT_COLS

class DatabaseProjectsMixin:

    async def list_projects(self, enabled_only: bool = True) -> list[dict]:
        where = "WHERE enabled=1" if enabled_only else ""
        rows = await self.run(f"SELECT * FROM projects {where} ORDER BY slug")
        return [self._parse_project(r) for r in rows]

    async def get_project(self, slug: str) -> Optional[dict]:
        # NOCASE: los targets de VS Code llegan como "INVENTORYDEMO" y los slugs
        # se guardan "inventorydemo" — el lookup no debe depender del caso.
        rows = await self.run(
            "SELECT * FROM projects WHERE slug=? COLLATE NOCASE", (slug,))
        return self._parse_project(rows[0]) if rows else None

    @staticmethod
    def _parse_project(row: dict) -> dict:
        row = dict(row)
        # Expandir `~` en repo_path: el seed de `notes` guarda
        # "~/.4bis/notes" y los consumidores (git_flow.is_git_repo,
        # subprocess cwd=, filesystem) NO expanden el tilde → cwd
        # inexistente → OSError 500 en /conversations.
        # Solo tocamos paths con `~`: los repos reales ya son absolutos
        # y se guardan con forward-slash (C:/Users/...). Usamos as_posix()
        # para MANTENER esa convención (str(Path(...)) los pasaría a
        # backslash, cambiando la representación de un path que otros
        # comparan por string). Ver _cbm_project_name (slash-agnóstico).
        rp = row.get("repo_path")
        if rp and rp.startswith("~"):
            row["repo_path"] = Path(rp).expanduser().as_posix()
        for col in ("mcp_servers", "defaults_json", "native_tools"):
            try:
                default = "[]" if col in ("mcp_servers", "native_tools") else "{}"
                row[col] = json.loads(row[col] or default)
            except (json.JSONDecodeError, TypeError):
                row[col] = [] if col in ("mcp_servers", "native_tools") else {}
        row["enabled"] = bool(row["enabled"])
        row["include_in_index"] = bool(row.get("include_in_index", 1))
        # ADR-028: modo nocturno (opt-in) + config JSON con defaults en código.
        row["night_mode_enabled"] = bool(row.get("night_mode_enabled", 0))
        try:
            row["night_config"] = json.loads(row.get("night_config") or "{}")
        except (json.JSONDecodeError, TypeError):
            row["night_config"] = {}
        return row

    async def upsert_project(self, p: dict) -> dict:
        """Crea o actualiza por slug. Solo toma columnas conocidas."""
        data = {k: p[k] for k in _PROJECT_COLS if k in p}
        if "slug" not in data:
            raise ValueError("slug requerido")
        for col in ("mcp_servers", "defaults_json", "native_tools", "night_config"):
            if col in data and not isinstance(data[col], str):
                data[col] = json.dumps(data[col], ensure_ascii=False)
        for col in ("enabled", "include_in_index", "night_mode_enabled"):
            if col in data:
                data[col] = 1 if data[col] else 0
        await self._upsert("projects", "slug", data)
        return (await self.get_project(data["slug"]))  # type: ignore[return-value]

    async def _upsert(self, table: str, key: str, data: dict) -> None:
        """INSERT o UPDATE parcial por clave. No usamos ON CONFLICT
        porque valida el INSERT completo (NOT NULL) antes de resolver
        el conflicto — rompería updates parciales."""
        async with self._upsert_lock:
            existing = await self.run(
                f"SELECT 1 FROM {table} WHERE {key}=?", (data[key],))
            if existing:
                sets = {c: v for c, v in data.items() if c != key}
                if not sets:
                    return
                clause = ", ".join(f"{c}=?" for c in sets)
                await self.run(
                    f"UPDATE {table} SET {clause}, updated_at=datetime('now') "
                    f"WHERE {key}=?", (*sets.values(), data[key]))
            else:
                cols = ", ".join(data)
                marks = ", ".join("?" for _ in data)
                await self.run(
                    f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                    tuple(data.values()))

    async def disable_project(self, slug: str) -> bool:
        """Soft delete: enabled=0, no rompe historial."""
        rows = await self.run(
            "UPDATE projects SET enabled=0, updated_at=datetime('now') "
            "WHERE slug=? RETURNING id", (slug,))
        return bool(rows)

    # ---- notes workspace (iter 9.8 — single-pair con UI chats) ----

    NOTES_SLUG = "notes"   # slug del proyecto auto-creado para notas/bitácora
    NOTES_ROOT_DEFAULT = "~/.4bis/notes"  # carpeta por default

    async def ensure_notes_project(
        self, *, root_dir: Optional[str] = None,
    ) -> Optional[dict]:
        """Auto-seed del proyecto `notes`. Idempotente.

        Si ya existe (incluso disabled) lo devuelve. Si no, lo crea
        con system_prompt dedicado y `enabled=1`. La carpeta en
        disco se crea afuera (DB no toca filesystem por ahora —
        ver server.py:_on_startup).

        Devuelve el dict del proyecto o None si el slug está
        ocupado por OTRO proyecto no-notes (defensiva: no pisar
        proyectos reales del usuario).
        """
        existing = await self.get_project(self.NOTES_SLUG)
        if existing is not None:
            return existing
        # Defensive check: si alguien ya tiene un proyecto en ese
        # slug (no debería pasar), no pisa.
        check = await self.run(
            "SELECT slug FROM projects WHERE slug=?",
            (self.NOTES_SLUG,))
        if check:
            return None
        path = root_dir or self.NOTES_ROOT_DEFAULT
        await self.upsert_project({
            "slug": self.NOTES_SLUG,
            "name": "Notas / bitácora",
            "repo_path": path,
            "system_prompt": (
                "Eres el asistente de notas/bitácora del usuario. "
                "Tu trabajo es ayudar a fijar ideas, decisiones, tareas "
                "sueltas y borradores. NO necesitas aprobar nada ni pedir "
                "permiso: si el usuario tira una idea, la guardas en el "
                "formato que pida. Si pide procesar/expandir/resumir, lo "
                "haces. Si te pide crear archivos en el workspace, usás "
                "las tools de filesystem disponibles. Responde siempre en "
                "español, sin emojis salvo que el usuario "
                "los pida, y trata los archivos del workspace como de él: "
                "leelos cuando haga falta, escribe solo cuando lo pida o "
                "lo implique el contexto."
            ),
            "description": "Workspace default para notas, ideas y decisiones sin repo",
            "mcp_servers": [],
            "defaults_json": {"timeout": 180},  # 3min es suficiente para notas
            "native_tools": [],  # sin cbm en este workspace
            "enabled": 1,
            "include_in_index": 0,
        })
        return await self.get_project(self.NOTES_SLUG)

    # ---- commands ----

    async def list_commands(self, enabled_only: bool = True) -> list[dict]:
        where = "WHERE enabled=1" if enabled_only else ""
        rows = await self.run(f"SELECT * FROM commands {where} ORDER BY name")
        return [self._parse_command(r) for r in rows]

    async def get_command(self, name: str) -> Optional[dict]:
        rows = await self.run("SELECT * FROM commands WHERE name=?", (name,))
        return self._parse_command(rows[0]) if rows else None

    async def delete_command(self, name: str) -> bool:
        """Borra un command por nombre. Devuelve True si existía y borró."""
        existing = await self.get_command(name)
        if not existing:
            return False
        await self.run("DELETE FROM commands WHERE name=?", (name,))
        return True

    async def delete_project(self, slug: str) -> bool:
        """Borra un project por slug. Hard delete (no soft)."""
        existing = await self.get_project(slug)
        if not existing:
            return False
        await self.run("DELETE FROM projects WHERE slug=?", (slug,))
        return True

    # ---- ignored_orphans ----

    async def list_ignored_orphans(self) -> list[str]:
        """Devuelve los cbm_name que el usuario eligió ignorar en /cbm/orphans."""
        rows = await self.run("SELECT cbm_name FROM ignored_orphans")
        return [r["cbm_name"] for r in rows]

    async def ignore_orphan(self, cbm_name: str, reason: str = "") -> bool:
        """Marca un cbm_name como ignorado. Devuelve True si se insertó."""
        try:
            await self.run(
                "INSERT INTO ignored_orphans (cbm_name, reason) VALUES (?, ?)",
                (cbm_name, reason),
            )
            return True
        except Exception:  # noqa: BLE001
            # PRIMARY KEY colisión = ya estaba.
            return False

    async def unignore_orphan(self, cbm_name: str) -> bool:
        existing = await self.run(
            "SELECT 1 FROM ignored_orphans WHERE cbm_name=?", (cbm_name,),
        )
        if not existing:
            return False
        await self.run(
            "DELETE FROM ignored_orphans WHERE cbm_name=?", (cbm_name,),
        )
        return True

    @staticmethod
    def _parse_command(row: dict) -> dict:
        row = dict(row)
        if row.get("args_schema"):
            try:
                row["args_schema"] = json.loads(row["args_schema"])
            except json.JSONDecodeError:
                row["args_schema"] = None
        row["enabled"] = bool(row["enabled"])
        return row

    async def upsert_command(self, c: dict) -> dict:
        data = {k: c[k] for k in _COMMAND_COLS if k in c}
        if "name" not in data:
            raise ValueError("name requerido")
        if "args_schema" in data and data["args_schema"] is not None \
                and not isinstance(data["args_schema"], str):
            data["args_schema"] = json.dumps(data["args_schema"], ensure_ascii=False)
        if "enabled" in data:
            data["enabled"] = 1 if data["enabled"] else 0
        await self._upsert("commands", "name", data)
        return (await self.get_command(data["name"]))  # type: ignore[return-value]

    async def disable_command(self, name: str) -> bool:
        rows = await self.run(
            "UPDATE commands SET enabled=0, updated_at=datetime('now') "
            "WHERE name=? RETURNING id", (name,))
        return bool(rows)

    # ---- mcp_servers (plan MCP_REGISTRY F0: catálogo, espejo de commands) ----

    @staticmethod
    def _parse_mcp(row: dict) -> dict:
        row = dict(row)
        for col, default in (("args", "[]"), ("env", "{}")):
            try:
                row[col] = json.loads(row[col] or default)
            except (json.JSONDecodeError, TypeError):
                row[col] = json.loads(default)
        for col in ("read_only", "on_demand", "enabled"):
            row[col] = bool(row[col])
        return row

    async def list_mcp_servers(self, enabled_only: bool = False) -> list[dict]:
        where = "WHERE enabled=1" if enabled_only else ""
        rows = await self.run(f"SELECT * FROM mcp_servers {where} ORDER BY name")
        return [self._parse_mcp(r) for r in rows]

    async def get_mcp_server(self, name: str) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM mcp_servers WHERE name=? COLLATE NOCASE", (name,))
        return self._parse_mcp(rows[0]) if rows else None

    async def upsert_mcp_server(self, m: dict) -> dict:
        data = {k: m[k] for k in _MCP_COLS if k in m}
        if "name" not in data:
            raise ValueError("name requerido")
        for col in ("args", "env"):
            if col in data and not isinstance(data[col], str):
                data[col] = json.dumps(data[col], ensure_ascii=False)
        for col in ("read_only", "on_demand", "enabled"):
            if col in data:
                data[col] = 1 if data[col] else 0
        await self._upsert("mcp_servers", "name", data)
        return (await self.get_mcp_server(data["name"]))  # type: ignore[return-value]

    async def delete_mcp_server(self, name: str) -> bool:
        """Hard delete (los links caen por ON DELETE CASCADE)."""
        existing = await self.get_mcp_server(name)
        if not existing:
            return False
        await self.run("DELETE FROM mcp_servers WHERE id=?", (existing["id"],))
        return True

    async def link_mcp(self, project_id: int, mcp_id: int) -> None:
        await self.run(
            "INSERT OR IGNORE INTO project_mcp_servers (project_id, mcp_id) "
            "VALUES (?, ?)", (project_id, mcp_id))

    async def unlink_mcp(self, project_id: int, mcp_id: int) -> None:
        await self.run(
            "DELETE FROM project_mcp_servers WHERE project_id=? AND mcp_id=?",
            (project_id, mcp_id))

    async def mcp_project_ids(self, mcp_id: int) -> list[int]:
        rows = await self.run(
            "SELECT project_id FROM project_mcp_servers WHERE mcp_id=?",
            (mcp_id,))
        return [r["project_id"] for r in rows]

    async def mcp_servers_for_project(
        self, project_id: int, names: Optional[list[str]] = None,
        capabilities: Optional[list[str]] = None,
        all_visible: bool = False,
    ) -> list[dict]:
        """Selector F1: MCPs habilitados visibles para un proyecto.

        Visible = sin links (global, caso wrapper) o linkeado al proyecto.
        `names` / `capabilities` filtran además por selección explícita
        (`use_capability("postgres-x")` → names; `--con db` → capabilities).
        Los filtros NO afectan a los always-on (on_demand=0): esos entran
        siempre. `all_visible=True` ignora los filtros y devuelve todo lo
        visible (para armar el menú de use_capability).
        """
        sql = (
            "SELECT m.* FROM mcp_servers m WHERE m.enabled=1 "
            "AND (NOT EXISTS (SELECT 1 FROM project_mcp_servers l "
            "                 WHERE l.mcp_id=m.id) "
            "     OR EXISTS (SELECT 1 FROM project_mcp_servers l "
            "                WHERE l.mcp_id=m.id AND l.project_id=?))")
        params: list[Any] = [project_id]
        if not all_visible:
            clauses = ["m.on_demand=0"]
            for col, values in (("name", names), ("capability", capabilities)):
                if values:
                    marks = ", ".join("?" for _ in values)
                    clauses.append(f"m.{col} COLLATE NOCASE IN ({marks})")
                    params.extend(values)
            sql += f" AND ({' OR '.join(clauses)})"
        sql += " ORDER BY m.name"
        rows = await self.run(sql, tuple(params))
        return [self._parse_mcp(r) for r in rows]

    # ---- crm_clients (snapshot read-only del CRM local) ----
    # ponytail: read-only en el sentido "el relay NO escribe contra el
    # CRM"; localmente sí se persiste (snapshot) para que la Admin UI
    # liste sin depender de que el CRM esté arriba. Upsert por
    # ext_id = `company.id` (cuid) del CRM.
