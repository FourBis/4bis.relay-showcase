"""Clonado, análisis estático, vetting y handshake de MCPs."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional
from .mcp_pool import probe_handshake

logger = logging.getLogger("relay.mcp_installer")

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
