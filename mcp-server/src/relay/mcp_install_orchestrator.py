"""Orquestación de jobs MCP desde clone hasta confirmación."""
from __future__ import annotations
import asyncio
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("relay.mcp_installer")

from .mcp_install_models import InstallJob, InstallerState, _now_iso, mcp_installs_root
from . import mcp_install_scan as scan

def _new_job(url: str) -> InstallJob:
    # file:// se permite solo si está activado por env var (tests/E2E).
    # Producción espera http/https/git@ (la UI copia el ejemplo así).
    if url.startswith("file://"):
        if not os.environ.get("FOURBIS_MCP_ALLOW_FILE_URL"):
            raise ValueError(
                "file:// no permitido; set FOURBIS_MCP_ALLOW_FILE_URL=1 "
                "para E2E/tests")
    elif not (url.startswith("http://") or url.startswith("https://")
              or url.startswith("git@")):
        raise ValueError("URL debe ser http(s) o git@")
    slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
    slug = re.sub(r"[^a-z0-9_\-]", "-", slug).strip("-") or "mcp"
    job_id = uuid.uuid4().hex[:12]
    install_dir = str(mcp_installs_root() / f"{slug}-{job_id}")
    now = _now_iso()
    return InstallJob(
        id=job_id, url=url, slug=slug, install_dir=install_dir,
        created_at=now, updated_at=now)

class McpInstaller:
    """Maneja jobs transitorios de install desde GitHub."""

    def __init__(self) -> None:
        self._jobs: dict[str, InstallJob] = {}
        self._lock = asyncio.Lock()

    async def start(self, url: str) -> InstallJob:
        """Crea un job, dispara clone + scan + vet en background."""
        job = _new_job(url)
        async with self._lock:
            self._jobs[job.id] = job
        # Background task — la respuesta es inmediata con state=PENDING
        # y se va actualizando. La UI hace polling.
        asyncio.create_task(self._run_pipeline(job))
        return job

    def get(self, job_id: str) -> Optional[InstallJob]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[InstallJob]:
        return list(self._jobs.values())

    async def _set_state(
        self, job: InstallJob, state: InstallerState,
        **patch: Any,
    ) -> None:
        job.state = state
        for k, v in patch.items():
            setattr(job, k, v)
        job.updated_at = _now_iso()
        logger.info("install %s: state=%s%s", job.id, state.value,
                    f" ({patch})" if patch else "")

    async def _run_pipeline(self, job: InstallJob) -> None:
        """Pasos 1-4 del §5. Si algo falla, marcamos `failed` y listo.
        El paso 5 (install + handshake) es separado: vive en `confirm`.
        """
        clone_dir = Path(job.install_dir)
        try:
            # 1. Clone
            await self._set_state(job, InstallerState.CLONING)
            commit = await scan.clone_repo(job.url, clone_dir)
            await self._set_state(job, InstallerState.CLONED,
                                  source_commit=commit)

            # 2. Scan estático
            await self._set_state(job, InstallerState.SCANNING)
            findings = await scan.static_scan(clone_dir)
            await self._set_state(job, InstallerState.SCANNED,
                                  scan_findings=findings)

            # 3. Vetting LLM (stub por ahora).
            await self._set_state(job, InstallerState.VETTING)
            verdict, report = await scan.vet_with_llm(clone_dir, findings)
            await self._set_state(job, InstallerState.VETTED,
                                  vet_verdict=verdict, vet_report=report)

            # 4. Detect run command y armar la propuesta. Si el
            # detector no puede inferir, proposal.needs_manual=True
            # y la UI/confirm exige override del humano.
            proposal = scan.detect_run_command(clone_dir)
            await self._set_state(
                job, InstallerState.AWAITING_CONFIRM,
                proposal=proposal)
        except Exception as e:  # noqa: BLE001
            logger.exception("install %s: pipeline falló", job.id)
            await self._set_state(
                job, InstallerState.FAILED, error=str(e))

    async def confirm(
        self, job_id: str, db, *,
        override: Optional[dict] = None,
    ) -> InstallJob:
        """Paso 5: install + handshake + INSERT en mcp_servers.

        Idempotente: si ya está healthy, devuelve el mismo job.
        Si vet_verdict es `rejected`, raise (la UI no debería llamar).
        Si la propuesta tiene `needs_manual=True`, raise con mensaje
        claro (la UI debería haber enviado `override` con el comando
        ya corregido por el humano).
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"job {job_id!r} no existe (¿expiró?)")
        if job.state == InstallerState.HEALTHY:
            return job
        if job.state == InstallerState.REJECTED:
            raise RuntimeError("vetting rechazó el MCP; instala otro o "
                               "fuerza el install a mano desde el form "
                               "del catálogo")
        if job.state not in (InstallerState.AWAITING_CONFIRM,
                             InstallerState.HANDSHAKE_FAILED,
                             InstallerState.INSTALL_FAILED):
            raise RuntimeError(
                f"job en estado {job.state.value!r}; no se puede "
                "confirmar todavía")

        # Merge override si vino.
        if override:
            job.override = dict(override)
        proposal = dict(job.proposal)
        proposal.update(job.override)
        proposal.update(override or {})

        if proposal.get("command", "") == "":
            raise RuntimeError(
                "no se pudo detectar comando y no vino override; "
                "pasa command/args/env en el body del confirm")

        # 5a. Install: solo logueamos "qué haría". El install real
        # (pip install / npm install) lo dispara el humano en su
        # shell, o lo dejamos como upgrade path. El handshake sobre
        # el binario ya instalado es lo que cuenta para v1.
        await self._set_state(job, InstallerState.INSTALLING)

        # 5b. Handshake. cwd = el clone: las propuestas tipo `npx -y .`
        # refieren al repo instalado (cwd vacío además revienta el
        # CreateProcess de Windows con WinError 123).
        ok, err = await scan.run_handshake(
            {"transport": "stdio",
             "command": proposal["command"],
             "args": list(proposal.get("args") or []),
             "env": dict(proposal.get("env") or {})},
            repo_path=str(job.install_dir),
            timeout_s=float(os.environ.get(
                "FOURBIS_MCP_INIT_TIMEOUT", "20")),
        )

        if not ok:
            # Materializamos la fila igual pero con health=handshake_failed,
            # enabled=0. Así el usuario ve el estado y decide qué hacer.
            await self._materialize_row(job, proposal, db,
                                        health="handshake_failed",
                                        enabled=False)
            await self._set_state(
                job, InstallerState.HANDSHAKE_FAILED, error=err)
            return job

        # 5c. Insert con health=ok + enabled=1.
        await self._materialize_row(job, proposal, db,
                                    health="ok", enabled=True)
        await self._set_state(job, InstallerState.HEALTHY)
        # El clone se CONSERVA: la fila lo referencia (install_dir) y
        # propuestas tipo `npx -y .` corren sobre él. Borrarlo rompía
        # el spin-up posterior. DELETE del MCP lo limpia (borra fila;
        # el dir queda como audit — plan §6).
        return job

    async def _materialize_row(
        self, job: InstallJob, proposal: dict, db,
        *, health: str, enabled: bool,
    ) -> None:
        """Inserta/actualiza la fila en mcp_servers con los datos del
        job + propuesta + (override del confirm) + handshake."""
        override = dict(job.override)
        mcp_name = override.pop("name", None) or job.slug
        capability = override.pop("capability", None) or "browser"
        row = {
            "name": mcp_name,
            "capability": capability,
            "transport": "stdio",
            "command": proposal.get("command", ""),
            "args": list(proposal.get("args") or []),
            "env": dict(proposal.get("env") or {}),
            "read_only": True,
            "on_demand": True,
            "idle_timeout_s": 300,
            "enabled": enabled,
            "source_url": job.url,
            "source_commit": job.source_commit,
            "install_dir": job.install_dir,
            "vet_verdict": job.vet_verdict,
            "vet_report": job.vet_report,
            "health": health,
        }
        await db.upsert_mcp_server(row)
