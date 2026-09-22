"""Consultas de uso y métricas de ejecución."""
from __future__ import annotations

import calendar
import json
import time
from datetime import datetime, timedelta
from typing import Optional

from .db_support import MODEL_PRICES_KEY, cost_usd, match_model_price, prices_from_models
from .reporting import previous_window, sqlite_utc_timestamp, utc_window

class DatabaseMetricsMixin:

    async def report_usage(self, days: int = 30,
                           project_slug: Optional[str] = None) -> dict:
        """Agregados de `chats` para el tab Informe de la Admin UI.

        2026-07-20: /chats capea en 200 filas — para un informe
        histórico el GROUP BY va en SQL, no en el browser. Read-only.
        """
        since = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
        where = "WHERE started_at>=?"
        params: tuple = (since,)
        if project_slug:
            where += " AND project_slug=? COLLATE NOCASE"
            params = (since, project_slug)
        agg = ("COUNT(*) AS runs, "
               "COALESCE(SUM(tokens_in),0) AS tokens_in, "
               # 2026-08-31: la parte cacheada de `tokens_in`, no un
               # sumando aparte. Sin esto el Informe mostraba el bruto y
               # parecía 3-4x del gasto real (82,5% medido en MiniMax).
               "COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens, "
               "COALESCE(SUM(tokens_out),0) AS tokens_out, "
               "COALESCE(SUM(tool_calls),0) AS tool_calls, "
               "CAST(AVG(duration_ms) AS INTEGER) AS avg_duration_ms, "
               "SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) AS not_ok")
        daily = await self.run(
            f"SELECT substr(started_at,1,10) AS day, {agg} "
            f"FROM chats {where} GROUP BY day ORDER BY day", params)
        by_project = await self.run(
            f"SELECT COALESCE(project_slug,'—') AS project, {agg}, "
            "MAX(started_at) AS last_run "
            f"FROM chats {where} GROUP BY project "
            "ORDER BY tokens_in DESC", params)
        by_author = await self.run(
            f"SELECT COALESCE(author,'—') AS author, "
            f"COALESCE(source,'—') AS source, {agg} "
            f"FROM chats {where} GROUP BY author, source "
            "ORDER BY tokens_in DESC", params)
        return {"days": days, "since": since, "daily": daily,
                "by_project": by_project, "by_author": by_author}

    # Sprint 1 (Item 3): Dashboard de métricas

    async def metrics_summary(  # noqa: C901
        self, days: int = 7, *, project: str = "", status: str = "",
        provider: str = "", role: str = "",
        from_date: str = "", to_date: str = "",
    ) -> dict:
        """KPIs agregados para el dashboard. Una query por sección.

        Los filtros operan en DOS niveles distintos, y mezclarlos daría
        totales incocherentes:

        * `project` y `status` filtran RUNS. Afectan todo: totales,
          desglose por modelo, por proyecto, horario y errores.
        * `provider` y `role` filtran TURNOS, que es una dimensión del
          desglose y no del run. Solo afectan `by_model`/`by_provider`;
          los totales siguen siendo los del run completo, porque un run
          no "pertenece" a un proveedor —usa varios—.

        Ventana temporal: si vienen `from_date` y `to_date` (YYYY-MM-DD,
        validados por `admin._parse_metrics_window`) se usa ese rango
        absoluto; si no, se cae al comportamiento viejo con `days`
        contando desde ahora. En ambos casos el "período anterior" del
        que sacamos los deltas es la ventana de igual largo
        inmediatamente anterior.

        `filters_applied` en la respuesta deja explícito qué se aplicó,
        para que la UI pueda decirlo en vez de mostrar números filtrados
        que parecen totales.
        """
        since, until, span_days = self._resolve_metrics_window(
            from_date, to_date, days)
        where, run_params = self._build_run_where(
            since, until, project, status)

        # Totales
        tot = await self.run(
            "SELECT COUNT(*) AS runs, "
            "COALESCE(SUM(tokens_in),0) AS tokens_in, "
            "COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens, "
            "COALESCE(SUM(tokens_out),0) AS tokens_out, "
            "COALESCE(SUM(tool_calls),0) AS tool_calls, "
            "CAST(COALESCE(AVG(duration_ms),0) AS INTEGER) AS duration_ms_avg, "
            "SUM(CASE WHEN status='error' AND COALESCE(phase_at_end,'')!='budget_split' THEN 1 ELSE 0 END) AS errors, "
            "SUM(status='ok' AND COALESCE(phase_at_end,'')!='budget_split') AS ok, "
            "SUM(status='running' AND COALESCE(phase_at_end,'')!='budget_split') AS running, "
            "SUM(status='cancelled' AND COALESCE(phase_at_end,'')!='budget_split') AS cancelled, "
            "SUM(COALESCE(phase_at_end,'')='budget_split') AS split "
            f"FROM chats {where}", tuple(run_params))
        totals = dict(tot[0]) if tot else {}

        # Período ANTERIOR de igual largo, para el delta de los KPIs. Un
        # número solo no es una señal: 229 runs no dice si el sistema se
        # está usando más o menos que la semana pasada. El rango previo
        # arranca donde terminaba el actual y tiene la misma amplitud
        # (`span_days`); sin `until` el WHERE es abierto abajo y sin
        # `since` no entramos acá.
        prev_since, prev_until = self._shift_window_back(
            since, until, span_days)
        prev_where, prev_params = self._build_run_where(
            prev_since, prev_until, project, status)
        prev = await self.run(
            "SELECT COUNT(*) AS runs, "
            "COALESCE(SUM(tokens_in),0) AS tokens_in, "
            "COALESCE(SUM(tokens_out),0) AS tokens_out, "
            "COALESCE(SUM(tool_calls),0) AS tool_calls, "
            "SUM(CASE WHEN status='error' AND COALESCE(phase_at_end,'')!='budget_split' THEN 1 ELSE 0 END) AS errors "
            f"FROM chats {prev_where}", tuple(prev_params))
        previous = dict(prev[0]) if prev else {}

        # Por modelo y ROL. Antes esto agrupaba solo por `chats.model`,
        # que guarda el spec del EJECUTOR: desde que las etapas corren en
        # proveedores distintos, eso le atribuía al modelo pagado todo lo
        # que consumieron el planificador, el verificador y el
        # documentador. Ahora cada etapa aporta su propia fila desde
        # `stages_json` (una UNION por etapa, sin tabla nueva).
        #
        # Los runs viejos no tienen `stages_json`: quedan con su fila de
        # ejecutor y ninguna de etapa, que es la verdad — no se midieron.
        _stage_union = " UNION ALL ".join(
            # Sin COALESCE a propósito: si la etapa corrió pero no se
            # midió (runs entre que se agregó stages_json y que se
            # instrumentaron los tokens), json_extract da NULL, SUM lo
            # ignora y la fila queda en NULL = "sin dato". Con COALESCE
            # a 0 se leería como "corrió y no consumió", que es mentira.
            # `cache_read_tokens` va en NULL para las etapas: no se mide
            # (planner/verifier/documenter son ~1% del gasto y corren en
            # otros proveedores). NULL = sin dato, y `cost_usd` entonces
            # les cobra la entrada entera, que es lo correcto acá.
            f"""SELECT json_extract(stages_json,'$.{st}_model') AS model,
                       '{st}' AS role, 1 AS runs,
                       json_extract(stages_json,'$.{st}_tokens_in') AS tokens_in,
                       NULL AS cache_read_tokens,
                       json_extract(stages_json,'$.{st}_tokens_out') AS tokens_out
                FROM chats {where} AND stages_json IS NOT NULL
                  AND json_extract(stages_json,'$.{st}_model') IS NOT NULL
                  AND json_extract(stages_json,'$.{st}_model') != ''"""
            for st in ("planner", "verifier", "documenter"))
        by_model = await self.run(
            "SELECT model, role, SUM(runs) AS runs, "
            "SUM(tokens_in) AS tokens_in, "
            "SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(tokens_out) AS tokens_out FROM ("
            "  SELECT COALESCE(model,'?') AS model, 'executor' AS role, "
            "         1 AS runs, COALESCE(tokens_in,0) AS tokens_in, "
            "         cache_read_tokens, "
            "         COALESCE(tokens_out,0) AS tokens_out "
            f"  FROM chats {where}"
            f"  UNION ALL {_stage_union}"
            ") GROUP BY model, role ORDER BY runs DESC",
            tuple(run_params) * 4)

        # Por proveedor (el prefijo antes de ':'). Responde la pregunta
        # que el desglose por modelo no contesta de un vistazo: cuánto
        # corre en el proveedor pagado y cuánto en los endpoints gratis.
        # `tokens_*` arranca en None y solo se vuelve número si alguna
        # fila trajo dato, para no reportar 0 tokens de un proveedor cuyas
        # etapas todavía no estaban instrumentadas.
        # Tarifas (fase 2). Sin `MODEL_PRICES` cargada, todos los costos
        # quedan en None y la UI los muestra como "sin tarifa" — nunca
        # como 0, que se leería como "gratis".
        # 2026-08-31: la tabla `models` es la base y `MODEL_PRICES` la
        # pisa. Antes se leía SOLO la clave de system_config, que estaba
        # vacía: `priced=false` y todos los costos en null con la tarifa
        # cargada en la pantalla Modelos. `MODEL_PRICES` sigue mandando
        # porque es donde viven los patrones `nvidia:*` y los `ref_*`.
        prices = prices_from_models(
            await self.run("SELECT spec, cost_in, cost_out, cost_cache_in "
                           "FROM models"))
        try:
            prices.update(json.loads(
                await self.get_config(MODEL_PRICES_KEY, "") or "{}"))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        # Filtros de TURNO. Se aplican acá y no en el SQL porque `role`
        # y `provider` son dimensiones del desglose: recortarlos en la
        # query dejaría los totales del run descuadrados contra la tabla.
        if role:
            by_model = [r for r in by_model if r["role"] == role]
        if provider:
            by_model = [r for r in by_model
                        if (r["model"] or "?").split(":", 1)[0] == provider]

        by_provider: dict[str, dict] = {}
        cost_total: Optional[float] = None
        saved_total: Optional[float] = None
        for row in by_model:
            spec = row["model"] or "?"
            price = match_model_price(prices, spec)
            row["cost_usd"] = cost_usd(
                price, row["tokens_in"], row["tokens_out"],
                cache_read=row.get("cache_read_tokens"))
            # Ahorro: lo que ESTE turno habría costado a precio de
            # mercado, menos lo que costó de verdad. Solo tiene sentido
            # donde hay `ref_*` cargado (los endpoints gratis).
            ref = cost_usd(price, row["tokens_in"], row["tokens_out"],
                           reference=True)
            row["saved_usd"] = (
                None if ref is None else ref - (row["cost_usd"] or 0.0))

            prov = spec.split(":", 1)[0] or "?"
            acc = by_provider.setdefault(
                prov, {"provider": prov, "runs": 0, "tokens_in": None,
                       "cache_read_tokens": None,
                       "tokens_out": None, "cost_usd": None,
                       "saved_usd": None})
            acc["runs"] += row["runs"] or 0
            for k in ("tokens_in", "cache_read_tokens", "tokens_out",
                      "cost_usd", "saved_usd"):
                if row[k] is not None:
                    acc[k] = (acc[k] or 0) + row[k]
            if row["cost_usd"] is not None:
                cost_total = (cost_total or 0.0) + row["cost_usd"]
            if row["saved_usd"] is not None:
                saved_total = (saved_total or 0.0) + row["saved_usd"]

        totals["cost_usd"] = cost_total
        totals["saved_usd"] = saved_total
        totals["priced"] = bool(prices)

        # Por proyecto (top 10)
        by_project = await self.run(
            "SELECT COALESCE(project_slug,'—') AS slug, COUNT(*) AS runs, "
            "COALESCE(SUM(tokens_in),0) AS tokens_in "
            f"FROM chats {where} GROUP BY project_slug "
            "ORDER BY runs DESC LIMIT 10", tuple(run_params))

        # Distribución horaria
        hourly = await self.run(
            "SELECT CAST(strftime('%H',started_at) AS INTEGER) AS hour, "
            "COUNT(*) AS runs "
            f"FROM chats {where} GROUP BY hour ORDER BY hour", tuple(run_params))

        # Top errores
        errors_breakdown = await self.run(
            "SELECT COALESCE(error,'(unknown)') AS error_type, COUNT(*) AS count "
            f"FROM chats {where} AND status='error' "
            "AND COALESCE(phase_at_end,'')!='budget_split' AND error IS NOT NULL "
            "GROUP BY error_type ORDER BY count DESC LIMIT 10", tuple(run_params))

        return {
            # `span_days` es la amplitud real del rango pedido: cuando el
            # dashboard mandó `from`+`to` puede no coincidir con `days`
            # (que queda en None). La UI ya sabe que 30 es 30 días.
            "period_days": span_days,
            "since": since,
            "until": until,
            "from_date": from_date,
            "to_date": to_date,
            # Solo las claves con valor: la UI pregunta "¿hay filtros?"
            # con un truthiness y no tiene que descartar strings vacíos.
            "filters_applied": {k: v for k, v in (
                ("project", project), ("status", status),
                ("provider", provider), ("role", role)) if v},
            "totals": totals,
            # Crudo, sin porcentajes calculados: el delta contra 0 no es
            # "+100%", es "no hay con qué comparar", y esa distinción la
            # decide quien lo muestra.
            "previous": previous,
            "by_model": by_model,
            "by_provider": sorted(by_provider.values(),
                                  key=lambda r: -r["runs"]),
            "by_project": by_project,
            "hourly_distribution": hourly,
            "error_breakdown": errors_breakdown,
        }

    async def metrics_trends(
        self, days: int = 7, *, project: str = "", status: str = "",
        from_date: str = "", to_date: str = "",
    ) -> list[dict]:
        """Slice diario para gráfica de tendencia.

        Toma los MISMOS filtros de nivel run que `metrics_summary`
        (`project` y `status`, con errores separados de cancelación,
        actividad y subdivisión). Sin esto la serie es global mientras los
        KPIs de arriba están filtrados, y el gráfico contradice a los
        números que tiene al lado.

        Misma ventana que summary: `from_date`+`to_date` absoluto o
        `days` fallback.

        `provider` y `role` no viajan a propósito: filtran TURNOS, no
        runs, así que no recortan ni los totales ni esta serie —igual
        que en `metrics_summary`.
        """
        since, until, _span = self._resolve_metrics_window(
            from_date, to_date, days)
        where, params = self._build_run_where(
            since, until, project, status)
        return await self.run(
            "SELECT substr(started_at,1,10) AS date, "
            "COUNT(*) AS runs, "
            "COALESCE(SUM(tokens_in),0) AS tokens_in, "
            "SUM(CASE WHEN status='error' AND COALESCE(phase_at_end,'')!='budget_split' THEN 1 ELSE 0 END) AS errors, "
            "SUM(status='ok' AND COALESCE(phase_at_end,'')!='budget_split') AS ok, "
            "SUM(status='running' AND COALESCE(phase_at_end,'')!='budget_split') AS running, "
            "SUM(status='cancelled' AND COALESCE(phase_at_end,'')!='budget_split') AS cancelled, "
            "SUM(COALESCE(phase_at_end,'')='budget_split') AS split "
            f"FROM chats {where} "
            "GROUP BY date ORDER BY date",
            tuple(params))

    # ---- helpers internos de métricas (Sprint 1: rango de fechas) ----

    @staticmethod
    def _resolve_metrics_window(
        from_date: str, to_date: str, days: int,
    ) -> tuple[str, str, int]:
        """Resuelve la ventana de tiempo en formato SQL.

        Devuelve `(since, until, span_days)`. `since` siempre se incluye
        en el WHERE; `until` es "" si no hay cota superior (modo
        `days`). `span_days` es la amplitud que va al JSON para que la UI
        sepa cuántos días cubre.

        El caller (admin._parse_metrics_window) ya validó que
        `from_date`/`to_date` parsean y que el rango no supera 366 días;
        acá solo se traduce a ISO-8601 con `T00:00:00Z` para que el
        `WHERE started_at>=?` los pueda comparar contra el
        `started_at` UTC que guarda SQLite.
        """
        if from_date and to_date:
            # 23:59:59 del día final para que un run que arrancó a las
            # 23:55 entre en "to=2026-02-01". Sin esto, to=inicio del
            # día y la query excluiría el mismo día del borde derecho.
            since = f"{from_date}T00:00:00Z"
            until = f"{to_date}T23:59:59Z"
            f = datetime.strptime(from_date, "%Y-%m-%d").date()
            t = datetime.strptime(to_date, "%Y-%m-%d").date()
            span = (t - f).days + 1
            return since, until, span
        # Fallback legacy: `days` contando desde ahora. Arriba, el admin
        # ya clampeó a 1..90; acá solo computamos el timestamp.
        since = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
        return since, "", days

    @staticmethod
    def _shift_window_back(
        since: str, until: str, span_days: int,
    ) -> tuple[str, str]:
        """Desplaza la ventana `span_days` hacia atrás, sin pisarla.

        Si la ventana actual tiene `until` (modo absoluto), la anterior
        va desde `until` (exclusivo) hasta `until - span_days`. Si es
        abierta (modo `days`), la anterior es `[since-span, since)`.
        Devuelve los dos extremos ya en formato ISO con hora fija
        (00:00:00 / 23:59:59) para que los WHERE comparen parejo con
        `started_at` UTC.
        """
        if until:
            # `until` está fijo en 23:59:59 del `to`; para la ventana
            # anterior, la "abajo" tiene que empezar exactamente al día
            # siguiente en 00:00:00. Calcularlo sobre el timestamp crudo
            # (sin hora) evita derivas por zona horaria del servidor.
            since_date = datetime.strptime(since[:10], "%Y-%m-%d").date()
            new_to_date = since_date - timedelta(days=1)
            new_since_date = new_to_date - timedelta(days=span_days - 1)
            return (f"{new_since_date}T00:00:00Z",
                    f"{new_to_date}T23:59:59Z")
        # Modo `days`: ventana abierta abajo. Cortamos en `since` (la
        # nueva va desde `since - span_days` hasta `since`, exclusivo).
        since_ts = calendar.timegm(time.strptime(since, "%Y-%m-%dT%H:%M:%SZ"))
        new_since_ts = since_ts - span_days * 86400
        return (time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(new_since_ts)),
            since)

    @staticmethod
    def _build_run_where(
        since: str, until: str, project: str, status: str,
    ) -> tuple[str, list]:
        """Arma el `WHERE started_at ...` de summary/trends con filtros run.

        Centralizado para que el `prev_where` del summary no tenga que
        reescribir el `replace("WHERE started_at>=?", ...)` original:
        ahora los dos lados (período actual y anterior) salen del mismo
        helper y agregan filtros en el mismo orden.
        """
        where = "WHERE started_at>=?"
        params: list = [since]
        if until:
            where += " AND started_at<?"
            params.append(until)
        if project:
            where += " AND project_slug=?"
            params.append(project)
        if status == "error":
            where += " AND status='error' AND COALESCE(phase_at_end,'')!='budget_split'"
        elif status == "split":
            where += " AND phase_at_end='budget_split'"
        elif status:
            where += " AND status=?"
            params.append(status)
            if status in ("ok", "running", "cancelled"):
                where += " AND COALESCE(phase_at_end,'')!='budget_split'"
        return where, params
