"""Repositorios de CRM, modelos y usuarios."""
from __future__ import annotations

import json
from typing import Optional

from .db_support import now_iso

class DatabaseCrmModelsMixin:

    async def upsert_crm_client(
        self, *, ext_id: str, name: str, domain: Optional[str],
        contacts_json: str, deals_json: str,
        last_sync_status: str = "ok",
        last_activity_at: Optional[str] = None,
    ) -> int:
        """Snapshot de una empresa del CRM. Devuelve el id local."""
        now = now_iso()
        async with self._upsert_lock:
            row = await self.run(
                "SELECT id FROM crm_clients WHERE ext_id=?",
                (ext_id,))
            if row:
                cid = row[0]["id"]
                await self.run(
                    "UPDATE crm_clients SET name=?, domain=?, "
                    "contacts_json=?, deals_json=?, last_sync_at=?, "
                    "last_sync_status=?, last_activity_at=? WHERE id=?",
                    (name, domain, contacts_json, deals_json, now,
                     last_sync_status, last_activity_at, cid))
                return cid
            rows = await self.run(
                "INSERT INTO crm_clients (ext_id, name, domain, "
                "contacts_json, deals_json, last_sync_at, last_sync_status, "
                "last_activity_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (ext_id, name, domain, contacts_json, deals_json,
                 now, last_sync_status, last_activity_at))
            return rows[0]["id"]

    async def list_crm_clients(self) -> list[dict]:
        rows = await self.run(
            "SELECT c.*, "
            "       (SELECT COUNT(*) FROM projects p "
            "        WHERE p.client_id=c.id) AS project_count "
            "FROM crm_clients c ORDER BY c.name")
        return [
            {
                "id": r["id"],
                "ext_id": r["ext_id"],
                "name": r["name"],
                "domain": r["domain"],
                "contacts": json.loads(r["contacts_json"] or "[]"),
                "deals": json.loads(r["deals_json"] or "[]"),
                "last_sync_at": r["last_sync_at"],
                "last_sync_status": r["last_sync_status"],
                "last_activity_at": r["last_activity_at"],
                "project_count": r["project_count"],
            }
            for r in rows
        ]

    async def get_crm_client(self, cid: int) -> Optional[dict]:
        rows = await self.run(
            "SELECT * FROM crm_clients WHERE id=?", (cid,))
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["id"],
            "ext_id": r["ext_id"],
            "name": r["name"],
            "domain": r["domain"],
            "contacts": json.loads(r["contacts_json"] or "[]"),
            "deals": json.loads(r["deals_json"] or "[]"),
            "last_sync_at": r["last_sync_at"],
            "last_sync_status": r["last_sync_status"],
            "last_activity_at": r["last_activity_at"],
        }

    async def mark_crm_sync_error(self, message: str) -> None:
        """Marca TODOS los clientes con last_sync_status='error'.

        Usado cuando el sync completo falla (ej: token expirado) para
        que la UI muestre el badge rojo sin perder los datos viejos."""
        await self.run(
            "UPDATE crm_clients SET last_sync_at=datetime('now'), "
            "last_sync_status='error'")

    async def prune_crm_clients(self, seen_ext_ids: list[str]) -> dict[str, int]:
        """Saca del snapshot lo que ya no está en el CRM.

        Sin esto el sync solo hace upsert y los clientes borrados en el CRM
        quedan para siempre en la tabla del relay (se veían 16 clientes
        contra 6 empresas reales).

        Regla: si nadie lo apuntaba, se borra; si tiene proyectos
        vinculados, NO se borra — la FK es ON DELETE SET NULL y borrarlo
        desvincularía los proyectos en silencio. Ese queda marcado 'gone'
        para que la UI lo muestre y vos decidas.
        """
        if not seen_ext_ids:
            # Un CRM vacío es indistinguible de un sync roto: no tocar nada.
            return {"removed": 0, "gone": 0}

        marks = ",".join("?" * len(seen_ext_ids))
        linked = (
            "EXISTS (SELECT 1 FROM projects p WHERE p.client_id = crm_clients.id)"
        )
        gone = await self.run(
            f"UPDATE crm_clients SET last_sync_status='gone' "
            f"WHERE ext_id NOT IN ({marks}) AND {linked} "
            f"AND last_sync_status <> 'gone' RETURNING id",
            tuple(seen_ext_ids))
        removed = await self.run(
            f"DELETE FROM crm_clients "
            f"WHERE ext_id NOT IN ({marks}) AND NOT {linked} RETURNING id",
            tuple(seen_ext_ids))
        return {"removed": len(removed), "gone": len(gone)}

    async def set_project_client(
        self, *, project_slug: str, client_id: Optional[int],
    ) -> None:
        """Vincula un project a un crm_client (F2: Convertir en proyecto)."""
        await self.run(
            "UPDATE projects SET client_id=?, updated_at=? WHERE slug=?",
            (client_id, now_iso(), project_slug))

    async def find_crm_client_by_deal(self, deal_id: str) -> Optional[dict]:
        """El cliente cuyo `deals_json` contiene ese deal.

        ponytail: escaneo lineal sobre el snapshot (decenas de clientes,
        pocos deals cada uno). Si algún día duele, los deals pasan a tabla
        propia con índice por id — ver la nota del schema de crm_clients.
        """
        for client in await self.list_crm_clients():
            for deal in client.get("deals") or []:
                if str(deal.get("id")) == deal_id:
                    return client
        return None

    async def set_project_deal(
        self, *, project_slug: str, deal_id: Optional[str],
        client_id: Optional[int],
    ) -> None:
        """Vincula un project a un deal del CRM.

        Escribe `client_id` en la misma sentencia porque un deal pertenece
        a una company: dejarlos desincronizados haría que el proyecto
        cuelgue de un cliente que no es el dueño de su deal.
        """
        await self.run(
            "UPDATE projects SET deal_id=?, client_id=?, updated_at=? "
            "WHERE slug=?",
            (deal_id, client_id, now_iso(), project_slug))

    async def list_projects_for_client(self, client_id: int) -> list[dict]:
        """Proyectos vinculados a un cliente CRM (eslabón cliente→proyecto).

        Devuelve el project parseado completo: quien arma la cadena
        necesita `repo_path` (git) y `defaults_json.github_project`
        (kanban) del mismo row, sin una segunda consulta por proyecto.
        """
        rows = await self.run(
            "SELECT * FROM projects WHERE client_id=? ORDER BY slug",
            (client_id,))
        return [self._parse_project(r) for r in rows]

    async def client_name_by_id(self) -> dict[int, str]:
        """`{crm_clients.id: name}` para resolver nombres en lote.

        El listado de proyectos muestra el cliente de cada fila; con 52
        proyectos, resolverlo de a uno serían 52 consultas.
        """
        rows = await self.run("SELECT id, name FROM crm_clients")
        return {r["id"]: r["name"] for r in rows}

    async def last_crm_sync_at(self) -> Optional[str]:
        rows = await self.run(
            "SELECT MAX(last_sync_at) AS s FROM crm_clients")
        return rows[0]["s"] if rows and rows[0]["s"] else None

    # ---- catálogo de modelos (2026-08-18) ----

    async def list_models(self, *, only_enabled: bool = False) -> list[dict]:
        sql = "SELECT * FROM models"
        if only_enabled:
            sql += " WHERE enabled=1"
        # Los prendidos primero y los medidos-que-ven arriba: el selector
        # los muestra en este orden y lo primero tiene que ser lo usable.
        sql += " ORDER BY enabled DESC, vision IS NULL, vision DESC, spec"
        return [dict(r) for r in await self.run(sql)]

    @staticmethod
    def mask_key(row: dict) -> dict:
        """Fila lista para viajar por la API: la key sale enmascarada.

        La tabla la guarda porque así se pidió, pero eso no es motivo
        para mandarla en cada poll del panel. `api_key_set` le dice a la
        UI si hay una cargada sin decir cuál.
        """
        out = dict(row)
        key = (out.pop("api_key", None) or "").strip()
        out["api_key_set"] = bool(key)
        out["api_key_hint"] = f"••••{key[-4:]}" if len(key) >= 4 else ""
        return out

    async def get_model(self, spec: str) -> Optional[dict]:
        rows = await self.run("SELECT * FROM models WHERE spec=?", (spec,))
        return dict(rows[0]) if rows else None

    async def model_context(self, model_name: str) -> tuple:
        """`(ventana, umbral_de_aviso)` del modelo, o `(None, None)`.

        2026-08-31. `model_name` es lo que el provider devuelve en cada
        respuesta (`MiniMax-M3`), sin el prefijo del spec — por eso el
        match es exacto O por sufijo `:modelo`. Devolver None y no un
        default es a propósito: quien llama (`experts.context_usage`)
        decide el fallback, y así el medidor puede decir contra qué
        número está midiendo en vez de mostrar un porcentaje de una
        ventana que no es la del modelo que corrió.
        """
        nombre = (model_name or "").strip()
        if not nombre:
            return (None, None)
        rows = await self.run(
            "SELECT context_tokens, context_warn_tokens FROM models "
            "WHERE spec=? OR spec LIKE ? COLLATE NOCASE LIMIT 1",
            (nombre, f"%:{nombre}"))
        if not rows:
            return (None, None)
        r = dict(rows[0])
        return (r.get("context_tokens"), r.get("context_warn_tokens"))

    async def upsert_model(self, spec: str, **campos) -> None:
        """Alta o edición. Solo toca las columnas que le pasan."""
        permitidas = ("label", "provider", "base_url", "api_key_env",
                      "api_key", "vision", "enabled", "cost_in", "cost_out",
                      "cost_cache_in", "context_tokens",
                      "context_warn_tokens", "notes", "verified_at")
        malas = set(campos) - set(permitidas)
        if malas:
            raise ValueError(f"columnas desconocidas: {sorted(malas)}")
        await self.run(
            "INSERT INTO models (spec, created_at) VALUES (?, ?) "
            "ON CONFLICT(spec) DO NOTHING", (spec, now_iso()))
        if campos:
            sets = ", ".join(f"{k}=?" for k in campos)
            await self.run(f"UPDATE models SET {sets} WHERE spec=?",
                           (*campos.values(), spec))

    async def delete_model(self, spec: str) -> None:
        await self.run("DELETE FROM models WHERE spec=?", (spec,))

    # ---- users / roles (ADR-037 fase 2) ----

    async def list_users(self) -> list[dict]:
        rows = await self.run(
            "SELECT email, role, created_at FROM users ORDER BY email")
        return [dict(r) for r in rows]

    async def set_user_role(self, email: str, role: str) -> None:
        """Alta o cambio de rol (2026-08-21: ya tiene UI y endpoints).

        El email se normaliza a minúsculas acá y no en el caller porque
        `identity.role_of` busca por email en minúsculas: una fila con
        mayúsculas sería un owner que nunca resuelve como owner.
        """
        if role not in ("owner", "member"):
            raise ValueError(f"rol desconocido: {role!r}")
        email = (email or "").strip().lower()
        if not email:
            raise ValueError("email vacío")
        await self.run(
            "INSERT INTO users (email, role, created_at) VALUES (?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET role=excluded.role",
            (email, role, now_iso()))

    async def delete_user(self, email: str) -> bool:
        """Saca la fila. NO es "sacarle el acceso": quien no está en la
        tabla es `member`, y el alta/baja de verdad la hace la policy de
        Access (ver `identity`). Esto solo lo devuelve a member."""
        rows = await self.run(
            "DELETE FROM users WHERE email=? COLLATE NOCASE RETURNING email",
            ((email or "").strip().lower(),))
        return bool(rows)

    # ---- chats (índice; el .md es la verdad) ----
