"""Ledger, detección de stack, gates y configuración del modo nocturno."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .night_types import (
    _CHAR_TO_STATE, _STATE_TO_CHAR, _TASK_RE, GATE_TIMEOUT_S, NightTask,
)

logger = logging.getLogger("relay.night")


def render_plan(
    *, project_slug: str, run_id: str, directive: str,
    tasks: list[NightTask],
) -> str:
    """Tareas → markdown del ledger (formato estricto, una línea por tarea)."""
    today = time.strftime("%Y-%m-%d")
    lines = [
        f"# Night plan — {project_slug} — {today}",
        f"Run: {run_id} · Directiva: {json.dumps(directive or '(auto)', ensure_ascii=False)}",
        "",
    ]
    for t in tasks:
        refs = ", ".join(f"`{r}`" for r in t.refs)
        line = f"- [{_STATE_TO_CHAR[t.status]}] {t.id}: {t.title} (Refs: {refs})"
        if t.note:
            line += f" — {t.note}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def parse_plan(text: str) -> list[NightTask]:
    """Ledger markdown → tareas. Líneas que no matchean se ignoran
    (headers, prosa) — el formato de tarea es estricto a propósito."""
    tasks: list[NightTask] = []
    for raw in text.splitlines():
        m = _TASK_RE.match(raw.rstrip())
        if not m:
            continue
        char, task_id, title, refs_raw, note = m.groups()
        refs = re.findall(r"`([^`]+)`", refs_raw or "")
        tasks.append(NightTask(
            id=task_id, title=title.strip(), refs=refs,
            status=_CHAR_TO_STATE.get(char, "pending"),
            note=(note or "").strip(),
        ))
    return tasks


def _slugify(text: str, max_len: int = 30) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def run_branch_name(project_slug: str) -> str:
    """Rama ÚNICA por noche-proyecto (Iter 5.1 — ADR-028 enmienda).

    Antes era `night/<fecha>-t-NNN-<slug>` (una rama por tarea,
    descartada al rollback). Ahora todas las tareas del run van a la
    MISMA rama; los commits buenos se acumulan, los rolls back se
    borran con `git reset --hard HEAD~1`. PR único al final.
    """
    safe = re.sub(r"[^a-z0-9]+", "-", project_slug.lower()).strip("-")
    return f"night/{time.strftime('%Y-%m-%d')}-{safe}"


# ---------- NightConfig: por proyecto, con auto-detect por stack ----------


def autodetect_cmds(repo_path: str) -> tuple[Optional[str], Optional[str]]:
    """(build_cmd, test_cmd) según markers del repo. None = no detectado.

    Busca también un nivel adentro (backend/, src/, etc.) porque muchos
    proyectos monorepo-style tienen el sln/csproj en un subdirectorio.
    Caso anonimizado: WorkshopDemo tiene `backend/WorkshopDemo.sln` y por
    eso `night_config` autogenerado volvía `test_cmd=None` → todas las
    tareas se descartaban por "sin test_cmd" (TDD estricto). Bug fix
    2026-07-20.
    """
    p = Path(repo_path)
    try:
        # Buscar sln/csproj hasta 2 niveles de profundidad. Depth 1 es
        # el caso normal (backend/, src/). Depth 2 cubre el caso raro
        # de monorepos más profundos.
        for depth in (0, 1, 2):
            pattern = "/".join(["*"] * depth + ["*.sln"]) if depth else "*.sln"
            if list(p.glob(pattern)):
                return "dotnet build", "dotnet test --no-build"
            pattern_csproj = (
                "/".join(["*"] * depth + ["*.csproj"]) if depth else "*.csproj"
            )
            if list(p.glob(pattern_csproj)):
                return "dotnet build", "dotnet test --no-build"
        pkg = p / "package.json"
        if pkg.is_file():
            try:
                scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
            except (json.JSONDecodeError, OSError):
                scripts = {}
            build = "npm run build" if "build" in scripts else None
            test = "npm test" if "test" in scripts else None
            return build, test
        if (p / "pyproject.toml").is_file():
            return None, "pytest"
    except OSError:
        pass
    return None, None


def resolve_cwd_for_cmd(repo: str, cmd: str) -> str:
    """cwd a usar para correr `cmd` en `repo`.

    Reglas:
    - Si el comando empieza con `dotnet` y el repo NO tiene sln/csproj
      en la raíz, buscar el primer subdirectorio que SÍ tenga (depth 1-2).
      Devolver path absoluto a ese subdir.
    - Para todo lo demás (npm, pytest, custom), devolver `repo`.
    - Si el repo ya tiene sln en raíz, devolver `repo` (no cambiar).
    """
    first = cmd.strip().split(None, 1)[0] if cmd.strip() else ""
    if not first.endswith("dotnet"):
        return repo
    # ¿Hay sln/csproj en raíz?
    rp = Path(repo)
    try:
        if list(rp.glob("*.sln")) or list(rp.glob("*.csproj")):
            return repo
    except OSError:
        return repo
    # Buscar hasta depth 2.
    try:
        for depth in (1, 2):
            for sub in rp.glob("/".join(["*"] * depth)):
                if not sub.is_dir():
                    continue
                if (list(sub.glob("*.sln")) or list(sub.glob("*.csproj"))
                        or list(sub.glob("*.slnx"))):
                    return str(sub.resolve())
    except OSError:
        pass
    return repo


async def run_gate(repo: str, cmd: str,
                   timeout: float = GATE_TIMEOUT_S) -> tuple[int, str]:
    """Corre `cmd` (string libre, con shell) en el cwd correcto de `repo`.

    ÚNICA ruta con shell del repo (ver comentario en BranchWorker._sh):
    build_cmd/test_cmd son strings editables desde la Admin UI.
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=resolve_cwd_for_cmd(repo, cmd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as e:
        return 127, f"no pude lanzar {cmd!r}: {e}"
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"{cmd!r} superó {timeout}s"
    return proc.returncode or 0, out_b.decode("utf-8", errors="replace")


_GATE_NOISE_RE = re.compile(
    r"error [A-Z]+\d+|error MSB\d+|: error |FAILED|Failed!|assert|Error:",
    re.IGNORECASE)
VERIFY_TIMEOUT_S = 420.0        # /cerrar es interactivo: menos que el gate nocturno


def _gate_excerpt(out: str, max_lines: int = 15) -> str:
    """Líneas relevantes del output de un build/test fallado; si no hay
    ninguna que matchee, la cola cruda."""
    hits = [l.strip() for l in out.splitlines() if _GATE_NOISE_RE.search(l)]
    if hits:
        return "\n".join(dict.fromkeys(hits))[:2000]
    return out.strip()[-1200:]


async def verify_repo(repo: str, project: dict) -> tuple[Optional[bool], str]:
    """Corre build+test del proyecto (config o auto-detect) para el PR de
    /cerrar. Devuelve (ok, markdown).

    ok=None → no hay comandos que correr (stack no detectado); el markdown
    lo dice para que el reviewer sepa que NADIE verificó, en vez de asumir
    verde. Si el build falla no se corren tests (regla de 4bis-shortcuts).
    Nunca lanza: verificar es best-effort, el PR se abre igual (el estado
    va en el body y el PR queda draft si está rojo).
    """
    cfg = NightConfig.from_project(project)
    lines: list[str] = []
    ok: Optional[bool] = None
    try:
        for label, cmd in (("build", cfg.build_cmd), ("tests", cfg.test_cmd)):
            if not cmd:
                continue
            rc, out = await run_gate(repo, cmd, timeout=VERIFY_TIMEOUT_S)
            if rc == 0:
                lines.append(f"- ✅ `{cmd}` OK")
                ok = True if ok is None else ok
                continue
            ok = False
            lines.append(
                f"- ❌ `{cmd}` falló (exit {rc})\n\n```\n{_gate_excerpt(out)}\n```")
            if label == "build":
                lines.append("- ⏭️ tests no corridos (el build falló)")
                break
    except Exception:  # noqa: BLE001 — best-effort, nunca romper /cerrar
        logger.exception("verify_repo rompió en %s (sigo sin verificación)", repo)
        return None, "## Verificación\n\n⚠️ La verificación rompió; ver logs del relay."
    if ok is None:
        return None, ("## Verificación\n\n⚠️ Sin `build_cmd`/`test_cmd` para este "
                      "proyecto: **nadie compiló ni testeó estos cambios**. "
                      "Configuralos en la Admin UI (night_config).")
    head = "✅ verde" if ok else "❌ ROJO — no mergear sin arreglar"
    return ok, f"## Verificación\n\n{head}\n\n" + "\n".join(lines)


@dataclass
class NightConfig:
    """projects.night_config (JSON) + defaults razonables en código."""
    build_cmd: Optional[str] = None
    test_cmd: Optional[str] = None
    max_diff_lines: int = 200
    discord_channel: str = "#equipo-demo"
    base_branch: str = "main"

    @classmethod
    def from_project(cls, project: dict) -> "NightConfig":
        raw = project.get("night_config") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except json.JSONDecodeError:
                raw = {}
        auto_build, auto_test = autodetect_cmds(project["repo_path"])
        return cls(
            build_cmd=raw.get("build_cmd") or auto_build,
            test_cmd=raw.get("test_cmd") or auto_test,
            max_diff_lines=int(raw.get("max_diff_lines") or 200),
            discord_channel=raw.get("discord_channel") or "#equipo-demo",
            # Bug fix 2026-07-20: si `base_branch` no está seteado o es
            # el default "main"/"master" genérico, autodetectar la rama
            # de integración real del repo. Preferir `develop` si
            # existe (muchos proyectos 4Bis la usan como rama de
            # integración, p.ej. WorkshopDemo). Si no, caer al
            # `git symbolic-ref refs/remotes/origin/HEAD`.
            base_branch=(
                raw.get("base_branch")
                or cls._autodetect_base_branch(project["repo_path"])
                or "main"
            ),
        )

    @staticmethod
    def _autodetect_base_branch(repo_path: str) -> Optional[str]:
        """Devuelve la rama de integración del repo o None.

        Orden de preferencia (verificado contra WorkshopDemo donde el
        bug era basear contra master cuando la integración va a
        develop):
          1. `develop` si existe local o como origin/develop
          2. `origin/HEAD` (lo que el remote marca como default)
          3. main / master locales como fallback

        Solo toca disco via subprocess.run (no async) porque se llama
        desde el constructor de NightConfig, que es sync. Si el repo
        tiene miles de refs esto tarda ~50ms — aceptable.
        """
        try:
            from pathlib import Path
            rp = Path(repo_path)
            if not (rp / ".git").exists():
                return None
            # Rama local directa via `git rev-parse --verify`.
            def _local_exists(name: str) -> bool:
                r = subprocess.run(
                    ["git", "-C", repo_path, "rev-parse", "--verify",
                     "--quiet", name],
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=5)
                return r.returncode == 0
            for cand in ("develop",):
                if _local_exists(cand) or _local_exists(f"origin/{cand}"):
                    return cand
            r = subprocess.run(
                ["git", "-C", repo_path, "symbolic-ref", "--quiet",
                 "refs/remotes/origin/HEAD"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5)
            ref = (r.stdout or "").strip()
            if r.returncode == 0 and ref:
                return ref.rsplit("/", 1)[-1]
            for cand in ("main", "master"):
                if _local_exists(cand):
                    return cand
        except Exception:  # noqa: BLE001
            return None
        return None


# ---------- Fase 1: TaskGenerator (Planificador) ----------

PLAN_INSTRUCTIONS = """\
Eres el planificador del modo nocturno de 4bis.relay. Recibes una
directiva humana (o logs de error) más contexto del repositorio
(archivos indexados, git status/diff). Tu trabajo: descomponerla en
tareas ATÓMICAS que un agente autónomo pueda ejecutar de noche, una
por una, todas compartiendo la MISMA rama git (rama ÚNICA del run,
NO una por tarea), con tests como gate.

Reglas duras:
- NO uses herramientas (read_file, search_graph, etc). Planifica
  SOLO con la información que te paso en el prompt (directiva +
  archivos indexados + git status/diff). Si necesitas más contexto,
  devuelve lista vacía con un campo `note` describiendo qué falta.
  Iterar con tools NO está permitido en esta fase.
- Cada tarea referencia SOLO archivos que aparezcan en la lista de
  archivos indexados que te doy. No inventes paths.
- Una tarea = un cambio pequeño y verificable (diff < 200 líneas).
- Descripción técnica imperativa, una frase, sin adornos.
- NUNCA propongas tocar .env, secrets, migraciones de DB ni configs
  de deploy.
- Si la directiva lista varios puntos numerados (P0.1, P0.2, 1., 2.,
  a., b., o cualquier numeración explícita), genera UNA tarea por
  punto, en el mismo orden. NO agrupes varios puntos en una sola
  tarea: son archivos distintos, independientes entre sí.
- Si la directiva no da para tareas concretas y verificables,
  devuelve lista vacía (mejor cero tareas que tareas inventadas).
- Responde ÚNICAMENTE el JSON del output_type pedido. Sin
  explicaciones, sin prose, sin fences de markdown.

EJEMPLO LITERAL DEL JSON ESPERADO (una tarea, formato de salida):
[
  {
    "title": "Agregar test TransferPayment_RejectsNonPositiveAmount en CommerceDemo.Tests/Services/PaymentServiceTests.cs",
    "refs": ["CommerceDemo.Tests/Services/PaymentServiceTests.cs", "CommerceDemo/Services/PaymentService.cs"]
  }
]

`refs` SIEMPRE es una LISTA de strings (aunque tenga un solo path).
NO uses objetos anidados como {"item": "..."} — siempre arrays planos."""
