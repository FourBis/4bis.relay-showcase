"""POST al bot de Discord cuando un agente quiere hablar.

Diseño:
- Progreso coalescido en segundo plano: un bot caído no frena al agente.
- Mensajes críticos esperan aceptación, con reintentos (tenacity).
- Si todo falla, NO abortamos el agente: lo loggeamos y seguimos.
  El usuario puede consultar /agents/{id}/state para ver el mensaje pendiente.
- Header X-Relay-Source: mcp-server para que el bot C# distinga de otros clientes.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import logctx

logger = logging.getLogger("relay.notify")

NOTIFY_KINDS = ("input_needed", "response", "done", "error", "cancelled",
                "progress", "question")
_PROGRESS_PENDING_MAX = 64
_PROGRESS_RETRY_AFTER_S = 30.0


class NotifyClient:
    def __init__(self, base_url: str, source: str = "mcp-server") -> None:
        # normalizo: sin slash al final, agregar /notify
        base = base_url.rstrip("/")
        if not base.endswith("/notify"):
            self.url = base + "/notify"
        else:
            self.url = base
        self.source = source
        # read=15s: el bot postea a Discord ANTES de responder el notify
        # y la API de Discord puede tardar >5s (rate limits) — con 5s el
        # notify moría en ReadTimeout y la respuesta nunca llegaba al
        # canal (visto 2026-07-19: "notify falló ... err=" vacío).
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=2.0),
            headers={
                "Content-Type": "application/json",
                "X-Relay-Source": source,
            },
        )
        self._progress_pending: dict[str, dict] = {}
        self._progress_task: asyncio.Task | None = None
        self._progress_agent: str | None = None
        self._progress_retry_at = 0.0
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True
        await self._discard_progress()
        await self._client.aclose()

    async def queue_progress(self, agent_id: str, kind: str, message: str,
                             metadata: dict | None = None) -> None:
        """Retiene el último progreso por agente; no confirma entrega al bot."""
        if kind != "progress":
            raise ValueError("queue_progress solo acepta kind='progress'")
        if (self._closed
                or asyncio.get_running_loop().time() < self._progress_retry_at):
            return
        # ponytail: un worker, último estado de hasta 64 agentes. El panel
        # conserva el timeline completo; usar cola durable si el bot lo requiere.
        if (agent_id not in self._progress_pending
                and len(self._progress_pending) >= _PROGRESS_PENDING_MAX):
            self._progress_pending.pop(next(iter(self._progress_pending)))
        self._progress_pending[agent_id] = {
            "agent_id": agent_id, "kind": kind, "message": message,
            "metadata": metadata or {},
        }
        if self._progress_task is None:
            self._progress_task = asyncio.create_task(
                self._drain_progress(), name="relay-notify-progress")

    async def _drain_progress(self) -> None:
        try:
            while self._progress_pending and not self._closed:
                self._progress_agent = next(iter(self._progress_pending))
                payload = self._progress_pending.pop(self._progress_agent)
                metadata = payload["metadata"]
                # El worker sirve varios runs: no heredar para todos el
                # contexto del primero. bind queda dentro de esta task.
                logctx.bind(
                    metadata.get("chat_id") or self._progress_agent.removeprefix("chat:"),
                    metadata.get("target") or "")
                try:
                    accepted = await self.send(**payload)
                except Exception:  # observabilidad: nunca interrumpe el run
                    logger.exception("no pude enviar progreso al bot")
                    accepted = False
                if not accepted:
                    self._progress_retry_at = (
                        asyncio.get_running_loop().time() + _PROGRESS_RETRY_AFTER_S)
                    self._progress_pending.clear()
                    break
        finally:
            self._progress_agent = None
            self._progress_task = None

    async def _discard_progress(self, agent_id: str | None = None) -> None:
        """Descarta progreso antes del cierre/pregunta; evita envíos tardíos."""
        if agent_id is None:
            self._progress_pending.clear()
        else:
            self._progress_pending.pop(agent_id, None)
        task = self._progress_task
        if task is not None and (agent_id is None or self._progress_agent == agent_id):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Una task cancelada antes de arrancar no ejecuta su finally.
            if self._progress_task is task:
                self._progress_task = None
            if (self._progress_pending and not self._closed
                    and self._progress_task is None):
                self._progress_task = asyncio.create_task(
                    self._drain_progress(), name="relay-notify-progress")

    def _acepto(self, r: httpx.Response, agent_id: str, kind: str) -> bool:
        """¿El bot lo posteó, o lo tiró y contestó 200 igual?

        2026-09-04, medido: el bot rechaza en el CUERPO y con status 200.
        `NotifyController` responde `Ok(new { discarded = true, reason })`
        en dos caminos que el relay usa de verdad:

          - `bad_agent_id_prefix`: solo reconoce `chat:`, `prompt:` y
            `night:`. El digest de CRM manda `crm-digest:<fecha>`
            (admin.py) y se descarta SIEMPRE.
          - `missing_qid_runid`: el handler de night exige `q_id` en la
            metadata sea cual sea el `kind`, y el `done` de fin de run
            (night.py) manda `run_id` sin `q_id`. También siempre.

        Mirando solo `raise_for_status()` los dos devolvían True, así que
        `admin.py` le contestaba `sent: true` a quien pidió el digest y
        en Discord no aparecía nada. Un fallo silencioso que además
        miente es peor que uno ruidoso: no hay forma de enterarse.

        Esto NO arregla el descarte —eso es un desajuste de contrato con
        el bot, y de qué lado se arregla es una decisión aparte— pero lo
        vuelve visible en el log y en el valor de retorno.
        """
        try:
            cuerpo = r.json()
        except Exception:  # noqa: BLE001 — leer el cuerpo es best-effort
            # Ancho a propósito: esto es observabilidad, no el envío. Si
            # el cuerpo no se puede leer —no es JSON, viene vacío, o el
            # objeto de respuesta no es el que esperábamos— la respuesta
            # correcta es "no sé si lo descartó", y ante la duda el 2xx
            # manda. Que `send` NUNCA reviente por esto es el contrato
            # del módulo entero: un notify roto no puede voltear al
            # agente que lo llamó.
            return True
        if not isinstance(cuerpo, dict) or not cuerpo.get("discarded"):
            return True
        logger.warning(
            "el bot DESCARTÓ el notify agent=%s kind=%s motivo=%s "
            "(contestó 200, no lo posteó)",
            agent_id, kind, cuerpo.get("reason") or "sin motivo")
        return False

    async def send(
        self,
        agent_id: str,
        kind: str,
        message: str,
        metadata: dict | None = None,
    ) -> bool:
        """Espera aceptación del bot; True nunca significa solo encolado.

        Para telemetría sin bloquear al agente, usar queue_progress.
        """
        if kind not in NOTIFY_KINDS:
            raise ValueError(f"kind inválido: {kind!r} (esperado: {NOTIFY_KINDS})")
        if self._closed:
            return False
        if kind != "progress":
            await self._discard_progress(agent_id)

        payload = {
            "agent_id": agent_id,
            "kind": kind,
            "message": message,
            "metadata": metadata or {},
        }

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
                retry=retry_if_exception_type((httpx.HTTPError,)),
                reraise=True,
            ):
                with attempt:
                    r = await self._client.post(self.url, json=payload)
                    r.raise_for_status()
                    return self._acepto(r, agent_id, kind)
        except httpx.HTTPStatusError as e:
            logger.warning(
                "notify falló agent=%s kind=%s status=%s body=%s",
                agent_id, kind, e.response.status_code, e.response.text[:200],
            )
        except httpx.HTTPError as e:
            # repr, no str: httpx.ReadTimeout tiene str() vacío y el log
            # quedaba "err=" sin decir NADA del porqué.
            logger.warning(
                "notify falló agent=%s kind=%s err=%r", agent_id, kind, e)
        return False


def default_bot_url() -> str:
    """Lee BOT_NOTIFY_URL del entorno, con fallback sensato para dev local.

    El relay y el bot corren nativos en la misma máquina (ADR-001, la
    era Docker terminó): loopback. Si el bot corre en otro host o en
    un container, configura BOT_NOTIFY_URL.
    """
    return os.environ.get("BOT_NOTIFY_URL", "http://127.0.0.1:8297/notify")
