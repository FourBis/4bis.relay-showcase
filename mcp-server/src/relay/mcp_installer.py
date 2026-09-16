"""Pipeline F2 del plan MCP_REGISTRY — alta desde GitHub.

Reproduce el flujo §5 del plan:
  1. Clone pineado (`git clone --depth 1 <url>`) → `source_commit`.
  2. Scan estático (determinista, sin ejecutar nada): `package.json`
     y `pyproject.toml` por red flags (postinstall, binarios
     prebuilt, env vars raros, etc.).
  3. Vetting por LLM (read-only sobre el clone). Iter 9.4
     (2026-07-18): el relay hace el file I/O read-only (repo_reader),
     mete el resumen al LLM y le pide verdict+reasons. Sin tools, sin
     Agent, sin loop. La interfaz es `vet_with_llm(clone_dir, findings)
     → (verdict, report)` y se enchufa cuando se quiera sin tocar el
     resto (un punto de inyección).
  4. El job queda en `awaiting_confirm` y la UI te muestra el
     veredicto + scripts de install.
  5. `POST /admin/api/mcp/install/{job_id}/confirm` corre install +
     handshake y materializa la fila en `mcp_servers` con
     `enabled=1`/`0` según `health`.

Los jobs viven en memoria (no en DB): son transitorios (minutos) y
si el relay cae antes del confirm, se pierden — aceptable. La UI
puede mostrar "job expirado, intenta de nuevo".

Punto de extensión para tests: todas las piezas pesadas
(`clone_repo`, `static_scan`, `vet_with_llm`, `detect_run_command`,
`run_handshake`) son funciones importables que los tests
monkeypatchean. El orquestador (`McpInstaller`) queda determinista.

Honestidad de seguridad (§5 del plan): NO es sandbox. Mitigamos con
commit pineado + scan estático + (futuro) veredicto LLM + confirmación
humana obligatoria + aislamiento de dir. Está documentado a propósito.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .mcp_pool import probe_handshake

logger = logging.getLogger("relay.mcp_installer")

# Tipos de estado. Usan Enum(str) para serializar tal cual a JSON
# (la UI los usa como string en el badge).
class InstallerState(str, Enum):
    PENDING = "pending"
    CLONING = "cloning"
    CLONED = "cloned"
    SCANNING = "scanning"
    SCANNED = "scanned"
    VETTING = "vetting"
    VETTED = "vetted"
    AWAITING_CONFIRM = "awaiting_confirm"
    INSTALLING = "installing"
    HEALTHY = "healthy"
    HANDSHAKE_FAILED = "handshake_failed"
    INSTALL_FAILED = "install_failed"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class InstallJob:
    id: str
    url: str
    slug: str
    install_dir: str
    state: InstallerState = InstallerState.PENDING
    source_commit: Optional[str] = None
    scan_findings: list[str] = field(default_factory=list)
    vet_verdict: str = "unknown"
    vet_report: str = ""
    error: Optional[str] = None
    # Lo que se va a insertar en mcp_servers cuando confirmas.
    # Detectado de heuristics del repo, pero editable vía confirm.
    proposal: dict = field(default_factory=dict)
    # Override del humano en /confirm: si viene, reemplaza proposal
    # antes de install. Sirve para "el detector se equivocó, ajusta
    # comando/args/env a mano".
    override: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_public(self) -> dict:
        """Lo que la UI consume (no expone campos internos sensibles)."""
        d = asdict(self)
        d["state"] = self.state.value
        return d


# ---------- Layout / paths ----------

def mcp_installs_root() -> Path:
    """Donde se clonan los MCPs durante install. ~/.4bis/mcp-installs/."""
    root = Path(os.environ.get("FOURBIS_MCP_INSTALLS_DIR",
                               str(Path.home() / ".4bis" / "mcp-installs")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------- 1. clone pineado ----------

async def clone_repo(url: str, dest: Path) -> str:
    """`git clone --depth 1 <url> <dest>` y devuelve el SHA del HEAD.

    Lanza RuntimeError con mensaje amigable si git falla (URL mala,
    sin red, binario faltante). No se hace nada destructivo en el
    destino: si ya existe, aborta.
    """
    if dest.exists():
        raise RuntimeError(f"install_dir ya existe: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1", url, str(dest)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode(errors="replace").strip() or "git clone falló"
        raise RuntimeError(f"clone falló: {msg}")
    # SHA del HEAD.
    head_proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(dest), "rev-parse", "HEAD",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    sha_out, _ = await head_proc.communicate()
    return sha_out.decode().strip()


# ---------- 2. scan estático (sin ejecutar nada) ----------

_INSTALL_HOOKS = ("preinstall", "postinstall", "install", "prepare")
_PREBUILT_GLOBS = (
    "**/*.so", "**/*.dll", "**/*.dylib",
    "**/*.exe", "**/*.bin",
    "**/*.whl",  # wheels prebuilt no audited
)
_CRED_HINT = re.compile(
    r"(?i)\b(AWS_|AZURE_|GCP_|GH_TOKEN|GITHUB_TOKEN|API_KEY|SECRET|PASSWORD)\b")


async def static_scan(clone_dir: Path) -> list[str]:
    """Heurística barata y determinista. NO ejecuta nada del repo.

    Devuelve una lista plana de hallazgos (string). Vacía = nada
    sospechoso a primera vista. Es **asesora, no garantía** — el LLM
    vetting que viene después (cuando se enchufe) debería tener la
    última palabra.
    """
    findings: list[str] = []

    # package.json: install hooks + deps raras + binarios.
    pkg = clone_dir / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            findings.append(f"package.json inválido: {e}")
            data = {}
        scripts = (data.get("scripts") or {})
        for hook in _INSTALL_HOOKS:
            if hook in scripts:
                findings.append(
                    f"package.json tiene script {hook!r}: "
                    f"{scripts[hook][:120]!r}")
        deps = list((data.get("dependencies") or {}).keys()) + \
               list((data.get("devDependencies") or {}).keys())
        # Sólo marcamos si hay MUCHAS (heurística perezosa).
        if len(deps) > 50:
            findings.append(f"package.json tiene {len(deps)} deps (revisa)")

    # pyproject.toml: presence de install hooks via setup.py / setup.cfg.
    for hook_path in ("setup.py", "setup.cfg"):
        if (clone_dir / hook_path).is_file():
            findings.append(
                f"{hook_path} presente — pip puede ejecutar setup hooks")

    # Binarios prebuilt colgados en el árbol.
    for pat in _PREBUILT_GLOBS:
        try:
            first = next(clone_dir.glob(pat), None)
        except (ValueError, OSError):
            first = None
        if first is not None:
            findings.append(
                f"binario prebuilt presente ({pat}): {first.name}")
            break  # uno basta como flag

    # README/manifestos que mencionan credenciales esperadas.
    for txt_name in ("README.md", "readme.md", "package.json"):
        p = clone_dir / txt_name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = _CRED_HINT.search(text[:5000])
        if m:
            findings.append(
                f"{txt_name} menciona credenciales ({m.group(0)}) — "
                "revisar qué pide")
            break

    return findings


# ---------- 3. vetting LLM (F2 — ADR-033) ----------

# System prompt del auditor. Iter 9.4: el LLM ya NO tiene tools — el
# relay le pasa el árbol del clon + archivos clave en el user message.
# El LLM solo interpreta y vota JSON. Esta parte es solo el system
# prompt (instrucciones de formato + criterios de verdict).
_VETTING_PROMPT = """\
Eres un auditor de servidores MCP. El relay YA leyó el árbol del clon
y los archivos clave y te los puso en el user message. Tu trabajo es
**interpretar** esos datos y responder SOLO un JSON con dos campos:

  {{
    "verdict": "safe" | "suspect" | "rejected",
    "reasons": ["motivo 1", "motivo 2", ...]
  }}

Eje (1) SEGURIDAD. Red flags que justifican `rejected`:
- Ejecuta binarios externos no declarados o descarga código remoto en install
- Exfiltra datos via HTTP/red a dominios desconocidos o hardcodeados
- Lee variables de entorno sensibles (AWS_*, GITHUB_TOKEN, *.PASSWORD,
  *.SECRET) y las postea a la red
- Scripts de install (postinstall, preinstall) corren código no declarado
- Ofuscación: eval/exec sobre base64, hex invertido, etc.
- Binarios prebuilt (.dll/.so/.exe/.node) sin source visible
- Crypto de mining, persistence via cron/registry, etc.

Eje (2) VALIDEZ. Si no es realmente un MCP server → `suspect` o `rejected`:
- No declara tools MCP (no implementa initialize + tools/list, o no es stdio/http)
- README no explica cómo correrlo
- package.json/pyproject.toml/Cargo.toml sin entry point claro
- Es claramente otra cosa (CLI tool, librería, framework)

Veredictos:
- `safe`: ambos ejes OK. Sin red flags significativos. Es un MCP válido.
- `suspect`: dudas. Algo raro pero no concluyente. El humano debería mirar.
- `rejected`: claramente inseguro o NO es un MCP server.

RESTRICCIONES:
- NO podes invocar herramientas — esta sesión es read-only y el
  relay te proveyó todo el contexto. Si necesitas info adicional,
  marcalo en `reasons` y la UI lo confirma manualmente después.

Hallazgos del scan estático que YA corrió (tu trabajo es validar o descartar):
{scan_findings}

Formato de salida:
- JSON válido únicamente (sin ```json fences ni comentarios)
- Sin texto antes ni después del JSON
"""


def _parse_vetting_response(text: str) -> tuple[str, str] | None:
    """Extrae verdict + reasons del JSON embebido en la respuesta del LLM.

    Devuelve (verdict, formatted_report) o None si no se pudo parsear.
    Acepta JSON envuelto en ```json fences o con texto alrededor.
    """
    import re as _re
    candidates = [
        text,
        _re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(),
                flags=_re.MULTILINE),
    ]
    for cand in candidates:
        # Buscar el primer bloque { ... } balanceado.
        for m in _re.finditer(r"\{", cand):
            start = m.start()
            depth = 0
            for i in range(start, len(cand)):
                if cand[i] == "{":
                    depth += 1
                elif cand[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(cand[start:i + 1])
                            v = obj.get("verdict")
                            r = obj.get("reasons")
                            if v in ("safe", "suspect", "rejected") \
                                    and isinstance(r, list) \
                                    and all(isinstance(x, str) for x in r):
                                return v, "; ".join(r)
                        except (json.JSONDecodeError, ValueError):
                            pass
                        break
    return None


async def vet_with_llm(
    clone_dir: Path, scan_findings: list[str],
) -> tuple[str, str]:
    """Devuelve (verdict, report). Verdict ∈ {safe, suspect, rejected, unknown}.

    Iter 9.4 (2026-07-18): antes usábamos run_expert con un proyecto
    sintetico y el LLM debía leer el clon via tools (read_file/list_dir).
    Bug: ese proyecto __vet__ no tenía mcp_servers ni wrappers nativos
    cargados → el Agent solo veía `cbm_query` (y el clon no estaba
    indexado), así que el LLM reportaba "no puedo leer" → fallback
    a `suspect` manual siempre. Costo: 1 round LLM quemado + confirmar
    a mano en el 100% de los installs.

    Fix: el relay hace el I/O read-only en Python (repo_reader) ANTES
    de invocar al LLM. El LLM solo interpreta un resumen estructurado
    (estructura + archivos clave) que ya viene en el user message — sin
    Agent, sin tools, sin loop. Si el resumen es >cap, lo capeamos.
    Cero tokens gastados en leer archivos; cero riesgo de "el LLM no
    puede leer el clon".

    Read-only real: el relay nunca ejecuta código del clon, ni siquiera
    lo lee más allá de los archivos clave. El LLM no tiene tools.
    Si llega a fallar, devolvemos `unknown` + nota.

    Modelo configurable via `FOURBIS_VET_MODEL` (default: `FOURBIS_MODEL`).
    Timeout: `FOURBIS_VET_TIMEOUT` (default 60s).
    """
    from .repo_reader import summarize  # lazy: evita import circular
    notes_static = ""
    if scan_findings:
        notes_static = f" {len(scan_findings)} hallazgos del scan estatico."

    # 1. Resumen read-only del clon (en proceso, deterministico).
    #    Si la lectura falla por IO, devolvemos unknown honesto.
    try:
        summary = summarize(clone_dir, max_depth=3)
    except Exception as e:  # noqa: BLE001
        logger.warning("vet_with_llm: summarize fallo: %r", e)
        return ("unknown",
                f"No pude leer el clon para auditar ({type(e).__name__}: "
                f"{e}).{notes_static} Confirmar manualmente en la UI.")

    # 2. El user message es el prompt del auditor + el resumen estructurado.
    #    El LLM no necesita tools — solo lee y vota JSON.
    findings_bullets = "\n".join(f"- {f}" for f in scan_findings) or "(sin hallazgos)"
    summary_text = summary.to_prompt_text()
    # Cap chabón: si el clon es enorme, cortamos a 100KB y avisamos al LLM.
    SUMMARY_CAP = 100_000
    if len(summary_text) > SUMMARY_CAP:
        summary_text = (summary_text[:SUMMARY_CAP]
                        + f"\n\n[…truncado a {SUMMARY_CAP} chars. "
                        "Si necesitas más, pídele al humano que abra los "
                        "archivos en el clon manualmente.]")
    user = (
        f"Valide este clon como MCP server.\n\n"
        f"Hallazgos del scan estático a evaluar:\n{findings_bullets}\n\n"
        f"Datos del clon (leídos por el relay, no necesitas tools):\n\n"
        f"{summary_text}\n\n"
        "Responde SOLO el JSON pedido."
    )

    # 3. Correr via run_consult (sin tools, un solo round).
    from . import config as relay_config
    from .experts import ModelUnavailable, run_consult

    spec = os.environ.get("FOURBIS_VET_MODEL", "") or relay_config.model_spec()
    timeout_s = float(os.environ.get("FOURBIS_VET_TIMEOUT", "60"))
    # run_consult trae cascada interna (system_config > env > default).
    # Le metemos el override.
    try:
        os.environ["FOURBIS_EXPERT_TIMEOUT_S"] = str(int(timeout_s))
    except ValueError:
        pass

    try:
        result = await run_consult(
            user=user,
            system_prompt=_VETTING_PROMPT.format(
                scan_findings="\n".join(f"- {f}" for f in scan_findings)
                or "(sin hallazgos)"),
            model_override=spec if spec != relay_config.model_spec() else "",
            db=None,
        )
    except ModelUnavailable as e:
        return ("unknown",
                f"Vetting LLM no disponible ({e}).{notes_static} "
                "Sin API key o modelo no configurado.")
    except Exception as e:  # noqa: BLE001
        logger.warning("vet_with_llm: run_consult fallo: %r", e)
        return ("unknown", f"Vetting LLM fallo ({type(e).__name__}: {e}).{notes_static}")

    # 4. Parsear la respuesta.
    content = (result.get("content") or "").strip()
    parsed = _parse_vetting_response(content)
    if parsed is None:
        preview = content[:200].replace("\n", " ")
        return ("unknown",
                f"Vetting LLM respondio sin JSON parseable.{notes_static} "
                f"preview: {preview!r}")

    verdict, reasons = parsed
    return (verdict, reasons or "(sin razones)")


# ---------- 4. detect run command (heurística) ----------

def detect_run_command(clone_dir: Path) -> dict:
    """Devuelve {command, args, env} para arrancar el MCP stdio.

    Heurística simple — si no puede inferir nada, devuelve dict con
    `command=""` y la install queda esperando override del humano.
    Upgrade path: mirar `package.json` bin / scripts, o leer docs más
    rico (manifests MCP, Dockerfile, etc.).
    """
    pkg = clone_dir / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        bin_name = (data.get("bin") or "")
        if isinstance(bin_name, dict):
            bin_name = next(iter(bin_name.values()), "")
        if not bin_name and data.get("main"):
            bin_name = data["main"]
        if bin_name:
            return {"command": "npx", "args": ["-y", "."],
                    "env": {}, "needs_manual": False}

    pyproject = clone_dir / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="ignore")
        # Proyecto con [project.scripts] o [project] con name conocido:
        # no podemos inferir el entry point sin parsear TOML; devolvemos
        # un placeholder que la UI completa.
        if "[project" in text or "[tool" in text or "[project.scripts]" in text:
            return {
                "command": "python",
                "args": ["-m", "<module>"],
                "env": {},
                "needs_manual": True,  # el humano debe editar args
            }

    return {"command": "", "args": [], "env": {},
            "needs_manual": True}


# ---------- 5. handshake one-shot ----------

async def run_handshake(
    cfg: dict, *, repo_path: str = "",
    timeout_s: Optional[float] = None,
) -> tuple[bool, str]:
    """Wrapper de `probe_handshake()` con timeout manejable y reporte
    de error legible. Devuelve (ok, error_msg).
    """
    if timeout_s is None:
        timeout_s = float(os.environ.get("FOURBIS_MCP_INIT_TIMEOUT", "20"))
    try:
        await asyncio.wait_for(
            probe_handshake(cfg, repo_path=repo_path),
            timeout=timeout_s)
        return (True, "")
    except asyncio.TimeoutError:
        return (False, f"handshake timeout ({timeout_s:.0f}s)")
    except Exception as e:  # noqa: BLE001
        return (False, f"handshake failed: {type(e).__name__}: {e}")


# ---------- Orquestador ----------

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
            commit = await clone_repo(job.url, clone_dir)
            await self._set_state(job, InstallerState.CLONED,
                                  source_commit=commit)

            # 2. Scan estático
            await self._set_state(job, InstallerState.SCANNING)
            findings = await static_scan(clone_dir)
            await self._set_state(job, InstallerState.SCANNED,
                                  scan_findings=findings)

            # 3. Vetting LLM (stub por ahora).
            await self._set_state(job, InstallerState.VETTING)
            verdict, report = await vet_with_llm(clone_dir, findings)
            await self._set_state(job, InstallerState.VETTED,
                                  vet_verdict=verdict, vet_report=report)

            # 4. Detect run command y armar la propuesta. Si el
            # detector no puede inferir, proposal.needs_manual=True
            # y la UI/confirm exige override del humano.
            proposal = detect_run_command(clone_dir)
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
        ok, err = await run_handshake(
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


