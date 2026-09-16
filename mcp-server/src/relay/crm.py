"""Cliente del CRM local (trycompai/crm).

El CRM es su propio stack (Next.js :3000 + NestJS :3001 + Postgres en
Docker) y es la fuente de verdad. El relay lo lee en **read-only** y deja
un snapshot en `crm_clients` para que la Admin UI liste sin depender de
que el CRM esté arriba, y para que `projects.client_id` siga apuntando a
una fila local.

ponytail: se lee Postgres directo en vez de la API del CRM porque toda su
API va por sesión de Better Auth (cookie emitida por un IdP); no hay token
de servicio. Es el mismo camino que usa `apps/agent` del CRM, que es otro
deployment compartiendo `DATABASE_URL`. Techo: acopla a los nombres de
tabla/columna del schema Prisma (`company`, `contact`, `deal`). Si el CRM
llega a exponer un token de servicio, cambiar `_fetch_all` por httpx y el
resto queda igual.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import asyncpg

from .config import log_dir, repos_root

log = logging.getLogger(__name__)

# Digest de salud de clientes (idea "silencio" + "resumen"): sin API de
# ventas, esto es lo que un dev-shop necesita — ¿a qué cliente con
# proyectos activos no le hablamos hace rato? Config por env, mismo
# patrón que CRM_DATABASE_URL/CRM_APP_URL: no amerita entrada en la UI
# de system_config para dos números.
DEFAULT_STALE_DAYS = int(os.environ.get("CRM_STALE_DAYS", "14"))
DEFAULT_DIGEST_CHANNEL = os.environ.get("CRM_DIGEST_CHANNEL", "#equipo-demo")


class CrmError(Exception):
    """Error hablando con el Postgres del CRM (caído, DSN mal, schema viejo).

    Hereda de Exception; los handlers la distinguen de bugs del relay.
    `status_code` existe para que la Admin UI pinte 503 vs 500.
    """

    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# El compose del CRM publica en 5434 (el 5432 lo ocupa Postgres compartido),
# y solo en IPv4: con "localhost" un resolver que prefiera ::1 no llega.
DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:5434/crm"


def crm_dsn(dsn: Optional[str] = None) -> str:
    """DSN del Postgres del CRM: kwarg > env > default del compose."""
    return dsn or os.environ.get("CRM_DATABASE_URL") or DEFAULT_DSN


# La app del CRM (Next.js). La UI del relay la usa para linkear empresas y
# deals: sin esto habría que hardcodear el host en el JS.
DEFAULT_APP_URL = "http://localhost:3000"
CRM_WORKSPACE = "crm"  # el slug del workspace en las rutas del CRM


def crm_app_url() -> str:
    return (os.environ.get("CRM_APP_URL") or DEFAULT_APP_URL).rstrip("/")


# `?schema=public` es sintaxis de Prisma, no de libpq: asyncpg la rechaza.
def _clean(dsn: str) -> str:
    return dsn.split("?", 1)[0]


async def _connect(dsn: Optional[str] = None) -> asyncpg.Connection:
    try:
        return await asyncpg.connect(_clean(crm_dsn(dsn)), timeout=10)
    except Exception as e:  # noqa: BLE001
        raise CrmError(
            f"no se pudo conectar al Postgres del CRM: {type(e).__name__}: {e}"
        ) from e


# Columnas mínimas para la vista de clientes del relay. Si hace falta más
# (logo, linkedin, brandColor), se agrega acá — el schema del CRM las tiene.
# `lastActivityAt` la mantiene `ActivityStampService` del CRM (se actualiza
# solo con cada email/reunión/nota); el digest de silencio se apoya en
# ella en vez de recalcular actividad del lado del relay.
_COMPANY_SQL = """
SELECT id, name, domain, website, industry, city, country,
       "lastActivityAt"
FROM company
ORDER BY name
"""

_CONTACT_SQL = """
SELECT id, "firstName", "lastName", email, phone, title, "companyId"
FROM contact
WHERE "companyId" IS NOT NULL
"""

_DEAL_SQL = """
SELECT id, name, stage::text AS stage, amount, currency,
       "expectedCloseDate", "companyId"
FROM deal
"""


async def fetch_companies(limit: Optional[int] = None,
                          *, dsn: Optional[str] = None) -> list[dict]:
    """Companies del CRM. `limit` recorta (sirve para validar conexión)."""
    conn = await _connect(dsn)
    try:
        sql = _COMPANY_SQL + (f"LIMIT {int(limit)}" if limit else "")
        rows = await conn.fetch(sql)
    except asyncpg.PostgresError as e:
        raise CrmError(f"query de companies falló: {e}") from e
    finally:
        await conn.close()
    return [dict(r) for r in rows]


def _contact_name(r: Any) -> str:
    return " ".join(filter(None, [r["firstName"], r["lastName"]])) or ""


async def check(dsn: Optional[str] = None) -> dict[str, Any]:
    """Ping + conteos. La UI lo usa para el semáforo del tab CRM."""
    conn = await _connect(dsn)
    try:
        counts = await conn.fetchrow(
            "SELECT (SELECT COUNT(*) FROM company)  AS companies,"
            "       (SELECT COUNT(*) FROM contact)  AS contacts,"
            "       (SELECT COUNT(*) FROM deal)     AS deals")
        sample = await conn.fetchval("SELECT name FROM company ORDER BY name LIMIT 1")
    except asyncpg.PostgresError as e:
        # Tabla inexistente = el CRM está arriba pero sin migrar.
        raise CrmError(
            f"el Postgres responde pero el schema del CRM no está "
            f"(¿falta `bun run db:deploy`?): {e}", 503) from e
    finally:
        await conn.close()
    return {"ok": True, "sample_company": sample, **dict(counts)}


# ---- levantar el stack del CRM (botón del tab) ----
# El CRM es otro stack y hay que arrancarlo a mano después de cada
# reinicio (docs/CRM_LOCAL.md). Esto es ese runbook en un botón:
# contenedor de Postgres + `bun run dev` (turbo: app 3000 + api 3001).

CRM_PG_CONTAINER = os.environ.get("CRM_PG_CONTAINER", "crm-postgres")


def crm_repo_path() -> str:
    """Repo del CRM: env > <repos_root>/crm."""
    return os.environ.get("CRM_REPO_PATH") or str(
        Path(repos_root()) / "crm")


def crm_dev_log() -> Path:
    """Salida de `bun run dev`. El proceso queda detached: este archivo es
    el único rastro cuando algo no arranca."""
    return log_dir() / "crm-dev.log"


async def app_up(timeout: float = 1.5) -> bool:
    """¿Contesta el puerto de la app del CRM?

    `check()` mira el Postgres, que sigue arriba con el dev server apagado
    (el contenedor es `restart: unless-stopped`), así que no sirve para
    decidir si hay que levantar algo. Un connect TCP alcanza y no pide
    cliente HTTP.
    """
    u = urlparse(crm_app_url())
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(u.hostname or "127.0.0.1", port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


# El dev server que largamos nosotros: evita apilar dos con dos clicks.
# Uno arrancado en otra terminal no se ve acá — para eso está `app_up()`.
_dev_proc: Optional[Any] = None

# Margen para que un arranque fallido (bun ausente, deps sin instalar) ya
# haya muerto cuando lo miramos. Los tests lo ponen en 0.
_SETTLE_S = 2.0


# `--ui=stream`: el `dev` del CRM es `turbo run dev` y turbo.json pide
# `"ui": "tui"`; sin consola turbo aborta con "Cannot run interactive task
# without Terminal UI". En stream los logs van derecho al archivo.
# `--filter`: solo app y api — el `agent` no se levanta a propósito
# (necesita un modelo por Vercel AI Gateway, ver docs/CRM_LOCAL.md).
_DEV_ARGS = ["run", "dev", "--ui=stream", "--filter=app", "--filter=api"]


def _dev_cmd() -> list[str]:
    # En Windows bun se instala como shim .cmd y CreateProcess no ejecuta
    # .cmd: hay que pasar por cmd.exe.
    return (["cmd", "/c", "bun", *_DEV_ARGS] if os.name == "nt"
            else ["bun", *_DEV_ARGS])


def _detach_kwargs() -> dict[str, Any]:
    """Sin ventana y en su propio grupo (no se lo lleva un Ctrl-C al relay).

    CREATE_NO_WINDOW y no DETACHED_PROCESS: probado en esta máquina, con
    DETACHED_PROCESS el `cmd` arranca, devuelve 0 y **no escribe una sola
    línea** al log — sin consola no ejecuta nada útil. El árbol igual
    sobrevive a que muera el relay: Windows no mata hijos al salir.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _log_tail(path: Path, n: int = 400) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:].strip()
    except OSError:
        return "(sin log)"


async def start_stack() -> dict[str, Any]:
    """Arranca Postgres (docker) y el dev server del CRM.

    No espera a que la app conteste: turbo + Next tardan ~40s en frío, más
    que cualquier timeout de fetch razonable. Quien llama poll-ea `app_up()`.
    """
    global _dev_proc
    repo = Path(crm_repo_path())
    if not (repo / "package.json").exists():
        raise CrmError(f"no encuentro el repo del CRM en {repo} "
                       f"(se configura con CRM_REPO_PATH)", 400)

    notes: list[str] = []
    # Postgres primero: sin base el dev server levanta igual y el error
    # recién aparece 40s después, en el semáforo.
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "start", CRM_PG_CONTAINER,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        msg = out.decode("utf-8", errors="replace").strip()[:200]
        notes.append(f"docker start {CRM_PG_CONTAINER}: {msg or 'ok'}")
    except (OSError, asyncio.TimeoutError) as e:
        notes.append(f"no se pudo arrancar {CRM_PG_CONTAINER}: {e!r} "
                     f"(¿Docker Desktop apagado?)")

    if _dev_proc is not None and _dev_proc.returncode is None:
        notes.append("ya había un `bun run dev` arrancado desde acá")
        return {"started": False, "notes": notes, "log": str(crm_dev_log())}

    log_path = crm_dev_log()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    with open(log_path, "ab") as fh:
        fh.write(f"\n=== bun run dev — {stamp} ===\n".encode("utf-8"))
        fh.flush()
        try:
            _dev_proc = await asyncio.create_subprocess_exec(
                *_dev_cmd(), cwd=str(repo),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=fh, stderr=asyncio.subprocess.STDOUT,
                **_detach_kwargs())
        except OSError as e:
            raise CrmError(f"no se pudo lanzar `bun run dev` en {repo}: {e}",
                           500) from e

    # Si murió al toque (bun no instalado, deps sin instalar) el motivo está
    # en el log: mejor devolverlo ahora que dejar a la UI poll-eando 3 min.
    await asyncio.sleep(_SETTLE_S)
    if _dev_proc.returncode is not None:
        raise CrmError(
            f"`bun run dev` murió al arrancar (exit {_dev_proc.returncode}): "
            f"{_log_tail(log_path)}", 500)
    notes.append(f"`bun run dev` corriendo (pid {_dev_proc.pid})")
    return {"started": True, "pid": _dev_proc.pid, "notes": notes,
            "log": str(log_path)}


async def sync_once(db: Any, *, dsn: Optional[str] = None) -> dict[str, int]:
    """Copia companies + contacts + deals del CRM al snapshot del relay.

    3 queries fijas (no N+1): se traen las tres tablas enteras y se agrupan
    en memoria. Con miles de filas sigue siendo más barato que una query
    por company. `db` es un relay.db.Database.
    """
    conn = await _connect(dsn)
    try:
        companies = await conn.fetch(_COMPANY_SQL)
        contacts = await conn.fetch(_CONTACT_SQL)
        deals = await conn.fetch(_DEAL_SQL)
    except asyncpg.PostgresError as e:
        raise CrmError(f"sync falló leyendo el CRM: {e}", 503) from e
    finally:
        await conn.close()

    by_company_contacts: dict[str, list[dict]] = {}
    for r in contacts:
        by_company_contacts.setdefault(r["companyId"], []).append({
            "id": r["id"],
            "name": _contact_name(r),
            "email": r["email"],
            "title": r["title"],
            "phone": r["phone"],
        })

    by_company_deals: dict[str, list[dict]] = {}
    for r in deals:
        by_company_deals.setdefault(r["companyId"], []).append({
            "id": r["id"],
            "name": r["name"],
            "stage": r["stage"],
            "amount": r["amount"],
            "currency": r["currency"],
            "closedate": r["expectedCloseDate"],
        })

    stats = {"companies": 0, "deals": 0, "contacts": 0, "errors": 0,
             "removed": 0, "gone": 0}
    for c in companies:
        try:
            cs = by_company_contacts.get(c["id"], [])
            ds = by_company_deals.get(c["id"], [])
            await db.upsert_crm_client(
                ext_id=c["id"],
                name=c["name"] or f"#{c['id']}",
                domain=c["domain"],
                contacts_json=json_dumps(cs),
                deals_json=json_dumps(ds),
                last_activity_at=(c["lastActivityAt"].isoformat()
                                  if c["lastActivityAt"] else None),
            )
            stats["companies"] += 1
            stats["contacts"] += len(cs)
            stats["deals"] += len(ds)
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            log.warning("CRM sync falló en company %s: %s", c["id"], e)

    # Reconciliar bajas: el upsert de arriba nunca saca nada, así que sin
    # esto lo borrado en el CRM sobrevive para siempre en el snapshot.
    # Solo si el sync fue limpio: con errores no sabemos si una company
    # "falta" o si su fila falló, y borraríamos por un error transitorio.
    if not stats["errors"]:
        stats |= await db.prune_crm_clients([c["id"] for c in companies])
    return stats


def json_dumps(obj: Any) -> str:
    """default=str para Decimal (amount) y datetime (closedate)."""
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)


# -------- digest de salud de clientes (silencio + resumen) --------
# Cálculo puro: sin DB ni `gh` acá adentro, para poder testearlo sin
# mocks. `admin.py` junta los datos (proyectos, issues/PRs de GitHub) y
# llama a estas dos funciones.

def days_since(iso: Optional[str], *, now: Optional[datetime] = None) -> Optional[int]:
    """Días desde `iso` (ISO-8601, con o sin tz). None si `iso` es None.

    `None` es "nunca tuvo actividad", que es una señal distinta —y peor—
    que "hace muchos días"; quien llama lo distingue explícitamente en
    vez de que un default silencioso los mezcle.
    """
    if not iso:
        return None
    when = datetime.fromisoformat(iso)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    return max(0, (ref - when).days)


def render_digest(rows: list[dict], *, stale_days: int) -> str:
    """Arma el texto del digest a partir de filas ya calculadas.

    Cada `row` trae: name, domain, days_silent (int|None), project_count,
    deals (list de {name, stage}), open_issues (int|None), open_prs
    (int|None). None en issues/PRs = no se pudo leer GitHub (sin `gh` o
    repo sin remote), y se marca distinto de "0 abiertos".

    Orden: primero los que llevan más silencio — es lo que hay que leer
    primero, no una lista alfabética.
    """
    if not rows:
        return "CRM: sin clientes con proyectos vinculados todavía."

    # None (nunca) es lo peor: va primero. Entre los que sí tienen fecha,
    # el que lleva más días sin contacto va antes.
    ordered = sorted(
        rows,
        key=lambda r: (r["days_silent"] is not None, -(r["days_silent"] or 0)))
    stale = [r for r in ordered if r["days_silent"] is None
             or r["days_silent"] >= stale_days]

    lines = [f"**Estado de clientes** ({len(rows)} con proyectos activos, "
             f"{len(stale)} sin contacto hace {stale_days}+ días)", ""]
    for r in ordered:
        d = r["days_silent"]
        if d is None:
            silence = "🔴 sin actividad registrada"
        elif d >= stale_days:
            silence = f"🔴 {d}d sin contacto"
        else:
            silence = f"🟢 {d}d"
        gh = ("" if r.get("open_issues") is None else
              f" · {r['open_issues']} issues, {r.get('open_prs') or 0} PR")
        deals = ", ".join(dl["name"] for dl in r.get("deals") or []) or "—"
        lines.append(
            f"**{r['name']}** — {silence}{gh}\n"
            f"    {r['project_count']} proyecto(s): {deals}")
    return "\n".join(lines)


# -------- check de smoke (ejecutable con `python -m relay.crm`) --------

if __name__ == "__main__":
    import asyncio
    import sys

    async def _main() -> int:
        try:
            r = await check(sys.argv[1] if len(sys.argv) > 1 else None)
        except CrmError as e:
            print(f"CRM NO disponible: {e.message}", file=sys.stderr)
            return 1
        print(f"CRM ok en {crm_dsn()}")
        print(f"  companies: {r['companies']}")
        print(f"  contacts:  {r['contacts']}")
        print(f"  deals:     {r['deals']}")
        return 0

    sys.exit(asyncio.run(_main()))
