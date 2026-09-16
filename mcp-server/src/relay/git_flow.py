"""Flujo git determinista para conversaciones (/nuevo, /cerrar).

El relay controla el git, NO el experto LLM. Antes el push/PR quedaba a
criterio del experto (bash dentro del workspace) → no era determinista y
el push a main "no se hacía". Acá está la máquina:

- `/nuevo`  → `open_conversation_branch`: rama `<autor>-<fecha>` desde base limpio.
- `/cerrar` → `finalize_conversation_pr`: auto-commit de lo pendiente →
  ensure `develop` → push rama → PR a develop (recuperando la URL si ya existía).

El usuario hace manualmente el PR develop→main. El bot NUNCA toca main.

Helpers de subprocess compartidos con night.BranchWorker (`_git`):
`create_subprocess_exec` sin shell para no escapar comillas/%/&/^ ni
newlines en los args de `gh` (evita inyección/roturas en Windows).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("relay.git_flow")

GIT_TIMEOUT_S = 60.0
PUSH_TIMEOUT_S = 120.0
GH_TIMEOUT_S = 60.0
DEVELOP_BRANCH = "develop"
# Ramas que el bot NUNCA borra, pase lo que pase.
PROTECTED_BRANCHES = frozenset({"main", "master", DEVELOP_BRANCH})


class GitFlowError(Exception):
    """Error operacional del flujo git (working tree sucio, git falló, etc.).

    El caller lo traduce a una respuesta HTTP para el bot."""


async def _git(repo: str, *args: str, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str]:
    """git <args> en `repo`. Stdout limpio si funciona; diagnóstico si falla.

    Nunca lanza: si el spawn falla (cwd inexistente, `git` no está en el
    PATH) devolvemos (127, msg). Antes un OSError acá se propagaba hasta
    el handler HTTP y lo convertía en 500 (p.ej. /conversations con un
    repo_path roto). El caller ya trata rc!=0 como "no es repo git".
    """
    rc, out, err = await _git_out(repo, *args, timeout=timeout)
    if not rc and err:
        logger.warning("git %s: %s", args[0] if args else "", err.strip())
    return rc, out if not rc else out + err


async def _git_out(repo: str, *args: str,
                   timeout: float = GIT_TIMEOUT_S) -> tuple[int, str, str]:
    """git <args> con stderr APARTE: (rc, stdout, stderr).

    Windows con `core.autocrlf` escribe
    un `warning: … LF will be replaced by CRLF` por archivo ANTES de la
    salida real, y en un stream `-z` esas líneas se comen los primeros
    registros: el primer archivo del diff aparecía como binario y con el
    status cortado. Todo lo que se parsea (numstat, name-status,
    name-only, ls-files, el texto del diff) usa esta.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=repo,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except (OSError, ValueError) as e:
        return 127, "", f"git spawn falló (cwd={repo!r}): {e}"
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"git {' '.join(args)} superó {timeout}s"
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    return (proc.returncode or 0,
            out_b.decode("utf-8", errors="replace"),
            err_b.decode("utf-8", errors="replace"))


async def _exec(repo: str, program: str, *args: str,
                timeout: float = GH_TIMEOUT_S) -> tuple[int, str]:
    """Como _git pero para cualquier programa (p.ej. `gh`). Sin shell: cada
    arg va literal, así el título/body del PR no necesitan escaping."""
    try:
        proc = await asyncio.create_subprocess_exec(
            program, *args, cwd=repo,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except (OSError, ValueError) as e:
        return 127, f"{program} spawn falló (cwd={repo!r}): {e}"
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"{program} {' '.join(args)} superó {timeout}s"
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out_b.decode("utf-8", errors="replace")


def branch_name(author: Optional[str]) -> str:
    """`<autor-sanitizado>-<YYYY-MM-DD>`. Fallback 'user' si no hay autor.

    Sanitiza el autor a [a-z0-9-] para que sea un ref git válido (sin
    espacios, sin caracteres raros de un username de Discord)."""
    safe = re.sub(r"[^a-z0-9]+", "-", (author or "user").strip().lower()).strip("-")
    return f"{safe or 'user'}-{time.strftime('%Y-%m-%d')}"


async def unique_branch_name(repo: str, author: Optional[str]) -> str:
    """Primer nombre de rama LIBRE: `<autor>-<fecha>`, `…-2`, `…-3`, …

    `branch_name` sola choca cuando hay dos conversaciones del mismo autor
    el mismo día. Antes eso "funcionaba" reusando la rama; desde que
    /cerrar borra la local (2026-07-27) el choque es peor: la rama local
    ya no está pero la REMOTA sigue viva con su PR abierto, así que el
    /nuevo siguiente creaba una rama homónima y el push la secuestraba —
    commits de otra conversación entrando al PR anterior.

    Por eso se chequean las dos: `refs/heads/<x>` y `origin/<x>`. Si el PR
    se mergeó y GitHub borró la rama, el nombre queda libre de nuevo y se
    reusa (no hay nada que secuestrar) — así el caso normal sigue siendo
    el nombre legible sin sufijo.

    El caller ya hizo `fetch`, así que los `origin/*` están frescos.
    """
    base = branch_name(author)
    for n in range(1, 100):
        cand = base if n == 1 else f"{base}-{n}"
        if (not await _branch_exists(repo, cand)
                and not await _branch_exists(repo, f"origin/{cand}")):
            return cand
    # 99 conversaciones del mismo autor el mismo día: dejá de trabajar.
    return f"{base}-{time.strftime('%H%M%S')}"


async def is_git_repo(repo: str) -> bool:
    rc, _ = await _git(repo, "rev-parse", "--is-inside-work-tree")
    return rc == 0


async def has_commits(repo: str) -> bool:
    """True si HEAD apunta a un commit. False en un repo recién `git init`
    sin commit (HEAD unborn), aunque tenga refs de `origin/*` traídas por
    un fetch. Ahí no hay base local de la cual sacar la rama de trabajo, y
    forzar un checkout de `origin/<base>` sobre archivos untracked los
    destruiría. El caller degrada a conversación sin rama."""
    rc, _ = await _git(repo, "rev-parse", "--verify", "--quiet", "HEAD")
    return rc == 0


async def detect_base_branch(repo: str) -> str:
    """Rama base del repo: 'main' o 'master' (u otra si origin/HEAD apunta ahí).

    Primero intenta `origin/HEAD`; si no hay remoto, prueba main y master
    locales; default 'main'."""
    rc, out = await _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    ref = out.strip()
    if rc == 0 and ref:
        # refs/remotes/origin/main → main
        return ref.rsplit("/", 1)[-1]
    for cand in ("main", "master"):
        rc, _ = await _git(repo, "rev-parse", "--verify", "--quiet", cand)
        if rc == 0:
            return cand
    return "main"


async def work_base_branch(repo: str) -> str:
    """Rama de la que se saca una rama de trabajo (/nuevo): `develop` si existe.

    Distinta de `detect_base_branch` (que es el TRUNK del repo, main/
    master, y sigue siendo la base para crear develop y para el PR).

    Bug 2026-07-22: /nuevo ramificaba desde el trunk pero el PR de
    /cerrar va SIEMPRE a develop. En este repo develop estaba 9 commits
    adelante de main, así que cada /nuevo rebobinaba el working tree —
    la Admin UI volvió al diseño anterior y el experto leía código
    viejo del que después tenía que salir un PR contra develop.

    Prioridad: develop local > origin/develop > trunk. Se prefiere el
    local (es lo que el humano ve en disco); si está atrasado del
    remoto, eso lo arregla un pull, no esta función.
    """
    if await _branch_exists(repo, DEVELOP_BRANCH):
        return DEVELOP_BRANCH
    if await _branch_exists(repo, f"origin/{DEVELOP_BRANCH}"):
        return f"origin/{DEVELOP_BRANCH}"
    return await detect_base_branch(repo)


async def working_tree_clean(
    repo: str, *, include_untracked: bool = True,
) -> tuple[bool, str]:
    """(limpio?, detalle). `include_untracked=False` NO cuenta archivos
    sin trackear (`??`) como sucio.

    Untracked default cuenta (lo usa el autocommit de /cerrar, que hace
    `git add -A` y necesita verlos). Pero el gate de `open_conversation_branch`
    lo apaga: crear una rama nueva sobre archivos untracked es seguro (no
    se pierden ni chocan), y en la práctica esos archivos son artefactos
    de tooling —`.claude/`, `.mcp.json`, `.vscode/`— que no
    están en el .gitignore del repo y bloqueaban CADA /nuevo con un
    'working tree sucio' por cambios que el usuario nunca hizo."""
    args = ["status", "--porcelain"]
    if not include_untracked:
        args.append("--untracked-files=no")
    rc, out = await _git(repo, *args)
    if rc != 0:
        return False, f"git status falló (rc={rc}): {out[-200:]}"
    return (not out.strip()), out.strip()[:300]


async def _branch_exists(repo: str, branch: str) -> bool:
    rc, _ = await _git(repo, "rev-parse", "--verify", "--quiet", branch)
    return rc == 0


async def branch_status(repo: str, branch: str,
                        base: str = DEVELOP_BRANCH) -> dict:
    """Estado de la rama local de la conversación: existe, mergeada a base,
    cuántos commits ahead/behind, y si es la rama actualmente checked-out.

    Pensado para el botón "borrar rama local" del panel de chat: la UI
    necesita saber si borrar es seguro (merged=True) o destructivo
    (merged=False con commits ahead) para armar el confirm.

    Defaults `base=develop`: el flujo de /cerrar apunta PRs a develop, así
    que "mergeada a develop" es la condición natural. Si el caller
    necesita otra base (main, master), la pasa explícita.

    Devuelve:
        {
          "branch": str,
          "base": str,
          "exists": bool,         # la rama existe local
          "merged": bool,         # está en `base` (o `base` la contiene)
          "ahead": int,           # commits en `branch` no en `base`
          "behind": int,          # commits en `base` no en `branch`
          "is_current": bool,     # HEAD apunta a la rama
          "current_branch": str,  # rama actual del repo
        }

    `merged` se chequea con `git branch --merged <base> --list <branch>`.
    Si la rama no existe local, `merged=False` y `ahead=behind=0` (no
    tenemos qué contar). Nunca lanza: errores de git devuelven
    `exists=False` con el mensaje en `error` y el caller decide.
    """
    out = {"branch": branch, "base": base, "exists": False, "merged": False,
           "ahead": 0, "behind": 0, "is_current": False,
           "current_branch": "", "error": None}
    if not await is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    out["current_branch"] = await _current_branch(repo)
    out["is_current"] = (out["current_branch"] == branch)
    if not await _branch_exists(repo, branch):
        return out
    out["exists"] = True
    # merged: listamos las ramas que están en `base` y vemos si la nuestra
    # aparece. Salida 1 línea por rama.
    rc, merged_out = await _git(repo, "branch", "--list",
                                branch, "--merged", base)
    if rc == 0:
        # `git branch --list` muestra la rama con un '* ' si es la actual;
        # strip espacios/asterisco para comparar.
        for line in merged_out.splitlines():
            if line.strip().lstrip("*").strip() == branch:
                out["merged"] = True
                break
    # ahead/behind: rev-list --count sobre los dos lados.
    rc_a, a = await _git(repo, "rev-list", "--count", f"{base}..{branch}")
    rc_b, b = await _git(repo, "rev-list", "--count", f"{branch}..{base}")
    if rc_a == 0 and a.strip().isdigit():
        out["ahead"] = int(a.strip())
    if rc_b == 0 and b.strip().isdigit():
        out["behind"] = int(b.strip())
    return out


async def delete_local_branch(repo: str, branch: str, *,
                              force: bool = False) -> dict:
    """Borra una rama local del repo. NO toca el remoto (la rama
    publicada la maneja el humano por GitHub, este endpoint es solo
    para limpiar la copia local que se va acumulando tras cada /cerrar).

    Guard: si la rama es la actual (`is_current=True`), rechaza. El
    humano tiene que hacer `git checkout develop` o lo que quiera
    antes de pedir el delete — borrar la rama bajo tus pies te deja
    en detached HEAD con todos los commits sin ref.

    Args:
        force: si True, usa `-D` (borrar aunque tenga cambios sin
            mergear). Sin `force`, usa `-d` que falla si la rama no
            está fully merged. La UI DEBE mostrarle al humano qué
            va a perder antes de mandar `force=True`.

    Returns:
        {"deleted": bool, "was_current": bool, "error": str|None}

    Nunca lanza. Errores de git caen en `error`.
    """
    out: dict = {"deleted": False, "was_current": False, "error": None}
    if not await is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    if not await _branch_exists(repo, branch):
        out["error"] = f"rama local {branch} no existe"
        return out
    current = await _current_branch(repo)
    out["was_current"] = (current == branch)
    if out["was_current"]:
        out["error"] = (f"no puedo borrar la rama actual ({branch}); "
                        f"haz checkout a otra rama primero")
        return out
    flag = "-D" if force else "-d"
    rc, txt = await _git(repo, "branch", flag, branch)
    if rc != 0:
        out["error"] = f"`git branch {flag} {branch}` falló: {txt[-200:]}"
        return out
    out["deleted"] = True
    logger.info("git_flow: borrada rama local %s (force=%s) en %s",
                branch, force, repo)
    return out


async def fetch_origin_safe(repo: str, *, timeout: float = 30.0) -> bool:
    """`git fetch origin` best-effort. Devuelve True si anduvo.

    Bug fix 2026-07-20: antes /nuevo y los night runs asumían que el
    local estaba sincronizado con el remoto, y operaban sobre lo que
    veían. Si el local tenía días de stale data, el experto recibía
    un contexto viejo y respondía coherente con eso (no con la
    realidad del repo). Caso real: chat de sae donde el bot leyó
    `master` viejo y propuso cambios ya mergeados.

    Ahora: SIEMPRE que arranca un /nuevo o un night run, hacemos fetch
    primero. Si falla (sin red, auth vencida, remote caído), loggeamos
    warning y seguimos con lo que haya — no rompemos el flujo por un
    problema de red. Pero el log queda como señal para el operador.

    NOTA: NO se llama desde ningun otro lugar — solo desde
    `open_conversation_branch` y `BranchWorker.ensure_branch` (night).
    NO es un background watcher: el cbm_watcher ya hace lo suyo.
    El criterio: gatillar fetch SOLO en momentos donde arrancar con
    local stale tiene costo real (crear rama, planificar tareas,
    auditar código).
    """
    rc, out = await _git(repo, "fetch", "origin", timeout=timeout)
    if rc != 0:
        logger.warning(
            "git_flow: fetch origin falló en %s (rc=%d): %s "
            "— sigo con el local como esté",
            repo, rc, out.strip()[:200])
        return False
    return True


async def sync_base_with_origin(repo: str, base: str) -> str:
    """Fast-forward `base` local a `origin/<base>`. Devuelve la ref a usar.

    Bug 2026-07-27: /nuevo hacía `fetch` pero después ramificaba desde la
    rama LOCAL (`develop`), que podía estar días atrás del remoto — las
    ramas de trabajo nacían desactualizadas y el experto leía código viejo
    (mismo síntoma que el bug del 22/7, otra causa).

    Solo fast-forward:
      - local al día o adelante         → no toca nada, devuelve `base`.
      - local atrás y sin commits propios → mueve el ref local y devuelve `base`.
      - divergió (ahead>0 y behind>0)   → NO toca el local (mergear/rebasear
        es decisión del humano) y devuelve `origin/<base>`, que es contra
        lo que el PR se va a abrir igual.

    El ref local se mueve con `update-ref` (sin tocar el working tree)
    salvo que `base` sea la rama checked-out, donde va `merge --ff-only`.
    El caller ya validó que el tree está limpio.
    """
    if base.startswith("origin/"):
        return base
    remote = f"origin/{base}"
    if not await _branch_exists(repo, remote):
        return base
    rc_a, ahead = await _git(repo, "rev-list", "--count", f"{remote}..{base}")
    rc_b, behind = await _git(repo, "rev-list", "--count", f"{base}..{remote}")
    n_ahead = int(ahead.strip()) if rc_a == 0 and ahead.strip().isdigit() else 0
    n_behind = int(behind.strip()) if rc_b == 0 and behind.strip().isdigit() else 0
    if n_behind == 0:
        return base
    if n_ahead:
        logger.warning(
            "git_flow: %s local divergió de %s (%d ahead / %d behind); "
            "ramifico desde %s y dejo el local como está",
            base, remote, n_ahead, n_behind, remote)
        return remote
    if await _current_branch(repo) == base:
        rc, out = await _git(repo, "merge", "--ff-only", remote)
    else:
        rc, out = await _git(repo, "update-ref", "-m",
                             f"4bis: fast-forward a {remote}",
                             f"refs/heads/{base}", remote)
    if rc != 0:
        logger.warning("git_flow: no pude actualizar %s a %s (%s); ramifico "
                       "desde %s", base, remote, out.strip()[:200], remote)
        return remote
    logger.info("git_flow: %s actualizada a %s (+%d commits)",
                base, remote, n_behind)
    return base


async def cleanup_conversation_branch(repo: str, branch: str) -> dict:
    """Post-PR de /cerrar: vuelve a la base y borra la rama local.

    La rama ya está pusheada y con PR abierto, así que la copia local no
    aporta nada y se acumulaba una por conversación (era limpieza manual
    desde la Admin UI). NO toca el remoto: eso lo maneja GitHub al mergear.

    `-D` y no `-d`: la rama está pusheada pero NO mergeada a develop
    (la mergea el PR), así que `-d` fallaría siempre.

    Guards: nunca borra `PROTECTED_BRANCHES`, y si el checkout a la base
    falla deja la rama donde está (borrar la rama actual = detached HEAD).

    Returns: {"deleted": bool, "branch": str, "base": str, "error": str|None}
    """
    out: dict = {"deleted": False, "branch": branch, "base": "", "error": None}
    if branch in PROTECTED_BRANCHES:
        out["error"] = f"{branch} es rama protegida; no la borro"
        logger.warning("git_flow: %s", out["error"])
        return out
    base = await work_base_branch(repo)
    if base.startswith("origin/"):
        base = await detect_base_branch(repo)   # no hay local donde pararse
    out["base"] = base
    rc, txt = await _git(repo, "checkout", base)
    if rc != 0:
        out["error"] = f"checkout {base} falló, dejo la rama: {txt[-200:]}"
        return out
    res = await delete_local_branch(repo, branch, force=True)
    out["deleted"] = bool(res["deleted"])
    out["error"] = res["error"]
    return out


# Artefactos de tooling que ensucian el working tree sin ser cambios del
# usuario. Bloqueaban /nuevo y se colaban al PR por el `git add -A` de
# /cerrar. Los mandamos al exclude LOCAL del repo: no toca el .gitignore
# trackeado ni se commitea, solo esta copia deja de verlos.
_TOOL_EXCLUDES = (".claude/", ".mcp.json", ".4bis/")
_EXCLUDE_MARK = "# 4bis.relay: artefactos de tooling (auto)"


async def ensure_local_excludes(repo: str) -> None:
    """Agrega _TOOL_EXCLUDES a `.git/info/exclude`, idempotente y
    best-effort (si falla, no rompe el /nuevo).

    Usa `git rev-parse --git-common-dir` para dar con el .git real: en un
    worktree `.git` es un archivo y el exclude compartido vive en el repo
    principal, no en el path del worktree."""
    rc, out = await _git(repo, "rev-parse", "--git-common-dir")
    if rc != 0 or not out.strip():
        return
    common = Path(out.strip())
    if not common.is_absolute():
        common = Path(repo) / common
    exclude = common / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if _EXCLUDE_MARK in existing:
            return  # ya lo agregamos antes
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if existing.endswith("\n") or not existing else "\n"
        block = f"{prefix}{_EXCLUDE_MARK}\n" + "\n".join(_TOOL_EXCLUDES) + "\n"
        with exclude.open("a", encoding="utf-8") as f:
            f.write(block)
        logger.info("git_flow: excludes de tooling agregados a %s", exclude)
    except OSError as e:
        logger.warning("git_flow: no pude escribir %s (%r), sigo", exclude, e)


async def open_conversation_branch(repo: str, author: Optional[str]) -> str:
    """/nuevo: crea y hace checkout de la rama de la conversación.

    Rama `<autor>-<fecha>[-N]` desde `work_base_branch` (develop si
    existe: es donde va el PR). Devuelve el nombre de rama. Lanza
    GitFlowError si el repo no es git o el tree está sucio.

    El nombre lo elige `unique_branch_name`: SIEMPRE una rama nueva. Antes
    reusaba la homónima del día, que es justo lo que pisa el PR de la
    conversación anterior.

    Bug fix 2026-07-20: hace `git fetch origin` antes de detectar la base
    para que `origin/HEAD`/`origin/develop` reflejen el remote real, no
    el cache de hace N horas/días.
    """
    if not await is_git_repo(repo):
        raise GitFlowError(f"{repo} no es un repo git")
    # Best-effort fetch. Si falla (red/auth) seguimos con lo que haya —
    # el caller igual va a poder crear la rama, solo que contra refs
    # potencialmente stale.
    await fetch_origin_safe(repo)
    # Excluir los artefactos de tooling ANTES del check: así ni bloquean
    # /nuevo ni se cuelan al PR en el `git add -A` de /cerrar.
    await ensure_local_excludes(repo)
    # Solo cambios TRACKEADOS bloquean: branchear sobre untracked es seguro
    # y esos suelen ser artefactos de tooling (.claude/, .mcp.json, …) que
    # el usuario no considera cambios. Ver working_tree_clean.
    clean, detail = await working_tree_clean(repo, include_untracked=False)
    if not clean:
        raise GitFlowError(
            "hay cambios trackeados sin commitear, no puedo crear una rama "
            f"limpia: {detail}. Commiteá o descartá esos cambios (los "
            "archivos sin trackear no molestan).")
    # Fast-forward de la base al remoto: sin esto el fetch de arriba solo
    # actualizaba `origin/*` y la rama nueva salía del local stale.
    base = await sync_base_with_origin(repo, await work_base_branch(repo))
    branch = await unique_branch_name(repo, author)
    rc, out = await _git(repo, "checkout", "-b", branch, base)
    if rc != 0:
        raise GitFlowError(f"checkout -b {branch} desde {base} falló: {out[-200:]}")
    logger.info("git_flow: rama %s creada desde %s", branch, base)
    return branch


async def _ensure_develop(repo: str, base: str) -> Optional[str]:
    """Crea la rama `develop` (trunk) desde base si no existe (local ni remota).

    Devuelve un mensaje de error o None si OK. Empuja develop al remoto para
    que el PR tenga contra qué abrirse. Deja el working tree en la rama en
    la que estaba (no cambia el checkout)."""
    if await _branch_exists(repo, DEVELOP_BRANCH):
        return None
    if await _branch_exists(repo, f"origin/{DEVELOP_BRANCH}"):
        return None  # existe remota; el PR puede targetearla igual
    # Crear develop desde base sin mover el checkout actual: `git branch`.
    rc, out = await _git(repo, "branch", DEVELOP_BRANCH, base)
    if rc != 0:
        return f"crear rama develop desde {base} falló: {out[-200:]}"
    rc, out = await _git(repo, "push", "-u", "origin", DEVELOP_BRANCH,
                         timeout=PUSH_TIMEOUT_S)
    if rc != 0:
        return f"push de develop falló: {out[-200:]}"
    logger.info("git_flow: rama develop creada desde %s y pusheada", base)
    return None


async def _current_branch(repo: str) -> str:
    rc, out = await _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if rc == 0 else ""


async def _autocommit_pending(repo: str, branch: str, message: str) -> Optional[str]:
    """Commitea lo que el experto dejó sin commitear en la rama.

    El experto LLM no siempre commitea (y antes eso hacía fallar el
    /cerrar con "sin commits propios" + dejaba el tree sucio, lo que
    encima bloqueaba el próximo /nuevo). El relay lo hace determinista.

    Devuelve mensaje de error o None si OK (incluye "nada que commitear").
    """
    clean, _ = await working_tree_clean(repo)
    current = await _current_branch(repo)
    if current != branch:
        # /nuevo dejó HEAD en la rama; si algo lo movió, solo seguimos
        # si el tree está limpio (checkout seguro). Sucio en otra rama
        # = estado anómalo: commitear ahí mezclaría trabajo ajeno.
        if not clean:
            return (f"HEAD está en {current or '?'} (no {branch}) con cambios "
                    f"sin commitear; resuelve a mano y reintenta /cerrar")
        rc, out = await _git(repo, "checkout", branch)
        if rc != 0:
            return f"checkout {branch} falló: {out[-200:]}"
        clean, _ = await working_tree_clean(repo)
    if clean:
        return None
    rc, out = await _git(repo, "add", "-A")
    if rc != 0:
        return f"git add falló: {out[-200:]}"
    rc, out = await _git(repo, "commit", "-m", message)
    if rc != 0:
        return f"git commit falló: {out[-300:]}"
    logger.info("git_flow: auto-commit de cambios pendientes en %s", branch)
    return None


async def _existing_pr_url(repo: str, branch: str) -> Optional[str]:
    """URL del PR abierto de `branch`, si ya existe (reintento de /cerrar
    después de un fallo parcial, o PR abierto a mano)."""
    rc, out = await _exec(repo, "gh", "pr", "view", branch,
                          "--json", "url", "--jq", ".url")
    url = out.strip().splitlines()[-1] if out.strip() else ""
    return url if rc == 0 and url.startswith("http") else None


async def branch_diff(repo: str, base: str, branch: str) -> tuple[str, str]:
    """(stat, diff) de `base...branch`. ("", "") si git falla."""
    rc, stat, _ = await _git_out(repo, "diff", "--stat", f"{base}...{branch}")
    if rc != 0:
        return "", ""
    rc, diff, _ = await _git_out(repo, "diff", f"{base}...{branch}")
    return (stat.strip(), diff) if rc == 0 else ("", "")


DIFF_CAP = 200_000


def _cap(text: str, cap: int) -> tuple[str, int, bool]:
    """(texto capeado en un salto de línea, tamaño real, truncado?)."""
    if len(text) <= cap:
        return text, len(text), False
    cut = text[:cap]
    nl = cut.rfind("\n")
    return (cut[:nl] if nl > 0 else cut), len(text), True


async def conversation_diff(repo: str, branch: str, *,
                            cap: int = DIFF_CAP) -> dict:
    """Qué cambió en la rama de una conversación. Git puro, sin LLM.

    El `git-diff` que ya existía por proyecto mira solo el working tree,
    así que una vez que el experto commitea no muestra nada. Acá el diff
    es contra la base (lo que va a ir al PR), en dos partes porque el
    experto no siempre commitea:

      - `stat` / `diff`: commits de la rama (`base...branch`).
      - `pending_stat` / `pending_diff`: cambios en el working tree sin
        commitear. SOLO si la rama es la checked-out — si HEAD está en
        otra, el tree no es de esta rama y mezclarlos mentiría.
      - `untracked`: archivos nuevos sin `git add` (no salen en el diff).

    Cada diff se capea a `cap` chars (`*_full_size` / `*_truncated`
    dicen cuánto era y si se cortó): un diff de 5MB cuelga al browser.

    Returns dict, nunca lanza. `error` explica el caso vacío (no es repo
    git, rama borrada tras el PR, etc.).
    """
    out: dict = {
        "branch": branch, "base": "", "exists": False, "is_current": False,
        "commits": 0, "stat": "", "diff": "", "full_size": 0,
        "truncated": False, "pending_stat": "", "pending_diff": "",
        "pending_full_size": 0, "pending_truncated": False,
        "untracked": [], "error": None,
    }
    if not await is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    out["base"] = base = await work_base_branch(repo)
    out["is_current"] = (await _current_branch(repo)) == branch
    if not await _branch_exists(repo, branch):
        out["error"] = (f"la rama local {branch} ya no existe "
                        f"(se borra al abrir el PR; el diff está en GitHub)")
        return out
    out["exists"] = True
    rc, count = await _git(repo, "rev-list", "--count", f"{base}..{branch}")
    if rc == 0 and count.strip().isdigit():
        out["commits"] = int(count.strip())
    stat, diff = await branch_diff(repo, base, branch)
    out["stat"] = stat
    out["diff"], out["full_size"], out["truncated"] = _cap(diff, cap)
    if out["is_current"]:
        # `diff HEAD` = staged + unstaged, o sea todo lo que el experto
        # tocó y todavía no commiteó.
        rc, pstat, _ = await _git_out(repo, "diff", "--stat", "HEAD")
        rc2, pdiff, _ = await _git_out(repo, "diff", "HEAD")
        if rc == 0:
            out["pending_stat"] = pstat.strip()
        if rc2 == 0:
            (out["pending_diff"], out["pending_full_size"],
             out["pending_truncated"]) = _cap(pdiff, cap)
        rc, uns, _ = await _git_out(repo, "ls-files", "--others",
                                    "--exclude-standard")
        if rc == 0:
            out["untracked"] = [l for l in uns.splitlines() if l.strip()][:200]
    return out


async def finalize_conversation_pr(
    repo: str, branch: str, *, title: str, body: str,
    body_builder: Optional[Callable[[str, str], Awaitable[tuple[str, bool]]]] = None,
) -> dict:
    """/cerrar: auto-commit pendiente → ensure develop → push → PR a develop.

    `body_builder(stat, diff)` (opcional) se llama una vez que la rama
    tiene commits propios y devuelve `(texto, draft)`: el texto se
    antepone al `body` y `draft=True` abre el PR como borrador (lo usa
    el verify de /cerrar cuando el build/test viene rojo — el PR se abre
    igual, marcado, en vez de dejar el trabajo varado sin PR).

    Best-effort: devuelve dict con `pr_url` (o None), `error` (o None),
    `committed` (True si hubo auto-commit) y `draft`. Nunca lanza; la
    conversación se cierra igual aunque el git falle.
    """
    result: dict = {"pr_url": None, "error": None, "committed": False,
                    "draft": False}
    try:
        if not await is_git_repo(repo):
            result["error"] = f"{repo} no es un repo git"
            return result
        base = await detect_base_branch(repo)
        # Cambios sin commitear del experto → commit determinista del relay.
        had_pending = not (await working_tree_clean(repo))[0]
        err = await _autocommit_pending(repo, branch, title)
        if err:
            result["error"] = err
            logger.warning("git_flow: %s", err)
            return result
        result["committed"] = had_pending
        # ¿Hay commits propios en la rama vs base? Sin commits no hay PR.
        rc, out = await _git(repo, "rev-list", "--count", f"{base}..{branch}")
        if rc != 0 or not out.strip() or out.strip() == "0":
            result["error"] = (
                f"rama {branch} sin commits propios sobre {base}; no abro PR")
            logger.info("git_flow: %s", result["error"])
            return result
        # Body redactado desde el diff real (los commits suelen decir
        # solo "fix") + verify. Best-effort: si falla, queda el body base.
        if body_builder is not None:
            stat, diff = await branch_diff(repo, base, branch)
            try:
                built, result["draft"] = await body_builder(stat, diff)
            except Exception:  # noqa: BLE001
                logger.exception("body_builder rompió (uso body base)")
                built = ""
            if built:
                body = f"{built}\n\n---\n{body}"
        # develop trunk.
        err = await _ensure_develop(repo, base)
        if err:
            result["error"] = err
            return result
        # push de la rama de la conversación.
        rc, out = await _git(repo, "push", "-u", "origin", branch,
                             timeout=PUSH_TIMEOUT_S)
        if rc != 0:
            result["error"] = f"push de {branch} falló: {out[-200:]}"
            logger.warning("git_flow: %s", result["error"])
            return result
        # PR contra develop.
        draft_args = ("--draft",) if result["draft"] else ()
        rc, out = await _exec(
            repo, "gh", "pr", "create",
            "--base", DEVELOP_BRANCH, "--head", branch,
            "--title", title, "--body", body, *draft_args)
        if rc != 0:
            # Reintento de /cerrar tras fallo parcial: el PR ya existe
            # pero el relay no guardó la URL. Recuperarla en vez de fallar.
            if "already exists" in out:
                url = await _existing_pr_url(repo, branch)
                if url:
                    result["pr_url"] = url
                    logger.info("git_flow: PR ya existía para %s: %s", branch, url)
                    return result
            result["error"] = f"gh pr create falló: {out[-300:]}"
            logger.warning("git_flow: %s", result["error"])
            return result
        url = out.strip().splitlines()[-1] if out.strip() else ""
        result["pr_url"] = url or None
        logger.info("git_flow: PR abierto %s → %s: %s", branch, DEVELOP_BRANCH, url)
        return result
    except Exception as e:  # noqa: BLE001 — best-effort, nunca romper el /cerrar
        result["error"] = f"{type(e).__name__}: {e}"
        logger.exception("git_flow: finalize_conversation_pr explotó")
        return result


# =====================================================================
# Visor de diff navegable (2026-08-24)
# =====================================================================
#
# `conversation_diff` (arriba) sirve el diff ENTERO de un saque, y eso
# es lo que tenía la UI: dos <pre> gigantes, cortados por el cap en la
# mitad de un archivo, sin forma de saltar a uno. Acá está la otra
# forma de pedir lo mismo, la que usa un dev: primero la LISTA de
# archivos (numstat, barata y completa — el cap no la corta), después
# el diff de UN archivo a demanda.
#
# Tres rangos, porque "qué cambió" tiene tres respuestas distintas:
#   all        merge-base(base, rama) → working tree. Lo que va a
#              quedar en el PR después del auto-commit de /cerrar.
#   committed  merge-base → rama. Solo los commits.
#   pending    HEAD → working tree. Solo lo que el experto no commiteó.

DIFF_MODES = ("all", "committed", "pending")
# Un archivo entero rara vez pasa los 200k del cap global; este es el
# techo por archivo para que un .min.js generado no cuelgue el browser.
FILE_DIFF_CAP = 400_000
# Techo de líneas al sintetizar el diff de un untracked (ver
# `_untracked_diff`): un dump de 100k líneas no se revisa, se abre.
UNTRACKED_MAX_LINES = 4_000


def _num(tok: str) -> int:
    """Celda de numstat a int. `-` (binario) -> -1."""
    tok = tok.strip()
    return int(tok) if tok.isdigit() else -1


def _parse_numstat_z(raw: str) -> dict[str, tuple[int, int, str]]:
    r"""`git diff --numstat -z` -> {path: (added, removed, old_path)}.

    Formato -z: `added\tremoved\tpath\0`, y en un rename/copy el record
    termina en el tab y los dos paths vienen como los tokens
    siguientes: `added\tremoved\t\0old\0new\0`.

    `-z` y no la salida normal a propósito: sin él git cita los paths
    con acentos/espacios (`"src/a\303\261o.py"`) y el rename llega
    mangleado como `{old => new}`, así que el path que le mandaríamos
    de vuelta a `git diff --` no existiría.
    """
    toks = raw.split("\0")
    out: dict[str, tuple[int, int, str]] = {}
    i = 0
    while i < len(toks):
        rec = toks[i]
        if not rec:
            i += 1
            continue
        parts = rec.split("\t")
        if len(parts) < 3:
            i += 1
            continue
        added, removed, path = parts[0], parts[1], parts[2]
        if path:
            out[path] = (_num(added), _num(removed), "")
            i += 1
        else:                                    # rename: paths aparte
            old = toks[i + 1] if i + 1 < len(toks) else ""
            new = toks[i + 2] if i + 2 < len(toks) else ""
            if new:
                out[new] = (_num(added), _num(removed), old)
            i += 3
    return out


def _parse_name_status_z(raw: str) -> dict[str, str]:
    """`git diff --name-status -z` -> {path: status}. `R100`/`C75` traen
    los dos paths como tokens siguientes (igual que numstat)."""
    toks = raw.split("\0")
    out: dict[str, str] = {}
    i = 0
    while i < len(toks):
        st = toks[i].strip()
        if not st:
            i += 1
            continue
        if st[0] in ("R", "C"):
            new = toks[i + 2] if i + 2 < len(toks) else ""
            if new:
                out[new] = st[0]
            i += 3
        else:
            path = toks[i + 1] if i + 1 < len(toks) else ""
            if path:
                out[path] = st[0]
            i += 2
    return out


async def _merge_base(repo: str, a: str, b: str = "HEAD") -> str:
    """merge-base de dos refs, o `a` si git no puede calcularla.

    El rango del visor se ancla en la merge-base y no en la base a
    secas: si develop avanzó desde que salió la rama, `git diff develop`
    mostraría los commits de develop como si el experto los hubiera
    borrado.
    """
    rc, out = await _git(repo, "merge-base", a, b)
    ref = out.strip().splitlines()[-1] if out.strip() else ""
    return ref if rc == 0 and ref else a


async def _diff_range(repo: str, branch: str, mode: str,
                      is_current: bool) -> list[str]:
    """Refs del rango de `mode`, listas para `git diff <refs> --`.

    Un solo ref = contra el working tree. Si la rama no es la
    checked-out, `all` y `pending` no pueden mirar el tree (es de otra
    rama) y degradan a `committed`.
    """
    if mode == "pending" and is_current:
        return ["HEAD"]
    base = await _merge_base(repo, await work_base_branch(repo), branch)
    if mode == "all" and is_current:
        return [base]
    return [base, branch]


async def diff_file_list(repo: str, branch: str, *,
                         mode: str = "all") -> dict:
    """Lista de archivos tocados, con contadores. Sin texto de diff.

    Una llamada al abrir el visor: `--numstat` no se corta por tamaño,
    así que la lista está COMPLETA aunque el diff pese megas. El texto
    lo pide `diff_file` archivo por archivo.

    Returns:
        {branch, base, mode, exists, is_current, commits, files, untracked,
         totals: {files, added, removed}, remote_ahead, error}
        files: [{path, old_path, status, added, removed, binary, pending}]
        `pending` = el archivo tiene cambios sin commitear (badge en la UI).

    Nunca lanza.
    """
    mode = mode if mode in DIFF_MODES else "all"
    out: dict = {"branch": branch, "base": "", "mode": mode, "exists": False,
                 "is_current": False, "commits": 0, "files": [],
                 "untracked": [], "totals": {"files": 0, "added": 0,
                                             "removed": 0}, "error": None}
    if not await is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    out["base"] = base = await work_base_branch(repo)
    out["is_current"] = is_current = (await _current_branch(repo)) == branch
    if not await _branch_exists(repo, branch):
        out["error"] = (f"la rama local {branch} ya no existe "
                        f"(se borra al abrir el PR; el diff está en GitHub)")
        return out
    out["exists"] = True
    rc, count = await _git(repo, "rev-list", "--count", f"{base}..{branch}")
    if rc == 0 and count.strip().isdigit():
        out["commits"] = int(count.strip())
    rango = await _diff_range(repo, branch, mode, is_current)
    rc_n, numstat, err_n = await _git_out(repo, "diff", "--numstat", "-z",
                                          "-M", *rango)
    rc_s, namest, _ = await _git_out(repo, "diff", "--name-status", "-z",
                                     "-M", *rango)
    if rc_n != 0:
        out["error"] = f"git diff --numstat falló: {err_n[-200:]}"
        return out
    counts = _parse_numstat_z(numstat)
    status = _parse_name_status_z(namest) if rc_s == 0 else {}
    # Qué está sin commitear, para el badge. Una llamada más, y solo
    # tiene sentido si el tree es de esta rama.
    pendientes: set[str] = set()
    if is_current:
        rc_p, pend, _ = await _git_out(repo, "diff", "--name-only", "-z",
                                       "HEAD")
        if rc_p == 0:
            pendientes = {p for p in pend.split("\0") if p}
        rc_u, uns, _ = await _git_out(repo, "ls-files", "--others",
                                      "--exclude-standard", "-z")
        if rc_u == 0:
            out["untracked"] = [p for p in uns.split("\0") if p][:500]
    for path in sorted(counts):
        added, removed, old = counts[path]
        out["files"].append({
            "path": path, "old_path": old,
            "status": status.get(path, "M"),
            "added": max(added, 0), "removed": max(removed, 0),
            "binary": added < 0 or removed < 0,
            "pending": path in pendientes,
        })
    out["totals"] = {
        "files": len(out["files"]),
        "added": sum(f["added"] for f in out["files"]),
        "removed": sum(f["removed"] for f in out["files"]),
    }
    # Commits locales que todavia no estan en origin: es lo que decide si
    # el boton Push tiene algo que hacer. -1 = la rama no esta pusheada.
    if await _branch_exists(repo, f"origin/{branch}"):
        rc, ahead = await _git(repo, "rev-list", "--count",
                               f"origin/{branch}..{branch}")
        out["remote_ahead"] = (int(ahead.strip())
                               if rc == 0 and ahead.strip().isdigit() else 0)
    else:
        out["remote_ahead"] = -1
    return out


def _rel_inside(repo: str, path: str) -> Optional[str]:
    """`path` relativo al repo si cae ADENTRO, o None.

    Los paths llegan por HTTP (el visor los manda de vuelta): un
    `../../.ssh/id_rsa` en un `git diff --` o en un `git restore --`
    sale del repo. Se valida con resolve(), no con string matching.
    """
    p = (path or "").strip().replace("\\", "/")
    if not p or p.startswith("/") or ":" in p.split("/")[0]:
        return None
    try:
        root = Path(repo).resolve()
        full = (root / p).resolve()
        full.relative_to(root)
    except (OSError, ValueError):
        return None
    return p


def _untracked_diff(repo: str, path: str) -> tuple[str, bool]:
    """(diff `+`-only de un archivo sin trackear, binario?).

    Un untracked no sale en `git diff` (git no lo conoce). El truco
    habitual es `git add -N`, pero eso escribe el índice del repo
    mientras el experto puede estar commiteando: se sintetiza el diff
    acá y no se toca git.
    """
    full = Path(repo) / path
    try:
        raw = full.read_bytes()
    except OSError as e:
        return f"# no pude leer {path}: {e}", False
    if b"\0" in raw[:8192]:
        return "", True
    lineas = raw.decode("utf-8", errors="replace").splitlines()
    recortado = len(lineas) > UNTRACKED_MAX_LINES
    lineas = lineas[:UNTRACKED_MAX_LINES]
    cuerpo = "\n".join("+" + ln for ln in lineas)
    if recortado:
        cuerpo += f"\n+... (primeras {UNTRACKED_MAX_LINES} líneas)"
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
            f"--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lineas)} @@\n{cuerpo}\n"), False


async def diff_file(repo: str, branch: str, path: str, *,
                    mode: str = "all", cap: int = FILE_DIFF_CAP,
                    context: int = 3, old_path: str = "") -> dict:
    """Diff de UN archivo del rango de `mode`. Untracked incluido.

    `old_path` (el que la lista trae para un rename) entra al pathspec
    junto con el nuevo: git detecta los renames DESPUÉS de limitar por
    path, así que pidiendo solo el destino un archivo renombrado se ve
    como un alta completa en vez de "renombrado, sin cambios".

    Returns:
        {path, mode, diff, full_size, truncated, binary, untracked, error}

    Nunca lanza. `error` con el motivo si el path es inválido o git falló.
    """
    mode = mode if mode in DIFF_MODES else "all"
    out: dict = {"path": path, "mode": mode, "diff": "", "full_size": 0,
                 "truncated": False, "binary": False, "untracked": False,
                 "error": None}
    rel = _rel_inside(repo, path)
    if rel is None:
        out["error"] = "path fuera del repo"
        return out
    out["path"] = rel
    if not await is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    is_current = (await _current_branch(repo)) == branch
    rango = await _diff_range(repo, branch, mode, is_current)
    ctx = max(0, min(int(context), 25))
    rel_old = _rel_inside(repo, old_path) if old_path else None
    pathspec = [rel] + ([rel_old] if rel_old and rel_old != rel else [])
    rc, texto, err = await _git_out(repo, "diff", f"--unified={ctx}", "-M",
                                    *rango, "--", *pathspec)
    if rc != 0:
        out["error"] = f"git diff falló: {err[-200:]}"
        return out
    if not texto.strip():
        # Ningún cambio trackeado: puede ser un untracked (o un path que
        # ya no está en el rango porque cambió el modo).
        rc_u, uns, _ = await _git_out(repo, "ls-files", "--others",
                                      "--exclude-standard", "-z", "--", rel)
        if rc_u == 0 and rel in {p for p in uns.split("\0") if p}:
            out["untracked"] = True
            texto, out["binary"] = _untracked_diff(repo, rel)
    if texto.startswith("Binary files ") or "\nBinary files " in texto:
        out["binary"] = True
    out["diff"], out["full_size"], out["truncated"] = _cap(texto, cap)
    return out


# =====================================================================
# Acciones git del visor (2026-08-24)
# =====================================================================
#
# Lo que un dev hace DESPUÉS de mirar el diff. El relay las corre él
# (mismo criterio que /nuevo y /cerrar: el git no lo maneja el experto),
# con los guards del flujo de ramas: la rama de trabajo no es protegida,
# el PR va a develop, y main/master no se tocan nunca desde acá.

def _guard_work_branch(branch: str) -> Optional[str]:
    """Motivo por el que `branch` no es una rama de trabajo, o None."""
    if not (branch or "").strip():
        return "esta conversación no tiene rama de trabajo"
    if branch in PROTECTED_BRANCHES:
        return (f"{branch} es rama protegida; el flujo es rama de trabajo "
                f"-> PR a {DEVELOP_BRANCH}")
    return None


def _safe_paths(repo: str, paths) -> tuple[list[str], Optional[str]]:
    """(paths relativos validados, error). Lista vacía = "todo"."""
    if not paths:
        return [], None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return [], "paths debe ser una lista de strings"
    limpios = []
    for p in paths[:500]:
        rel = _rel_inside(repo, p)
        if rel is None:
            return [], f"path fuera del repo: {p!r}"
        limpios.append(rel)
    return limpios, None


async def commit_paths(repo: str, branch: str, *, message: str,
                       paths: Optional[list[str]] = None) -> dict:
    """Commitea la rama de la conversación. `paths` vacío = `git add -A`.

    Guard: HEAD tiene que estar EN la rama de la conversación. Commitear
    con HEAD en otra rama mete el trabajo del experto en la rama de
    otro, y eso no se deshace con un botón.

    Returns: {committed, sha, message, files, error}
    """
    out: dict = {"committed": False, "sha": "", "message": message,
                 "files": 0, "error": None}
    err = _guard_work_branch(branch)
    if err:
        out["error"] = err
        return out
    msg = (message or "").strip()
    if not msg:
        out["error"] = "hace falta un mensaje de commit"
        return out
    if not await is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    current = await _current_branch(repo)
    if current != branch:
        out["error"] = (f"HEAD está en {current or '?'}, no en {branch}; "
                        f"no commiteo en una rama ajena")
        return out
    rels, perr = _safe_paths(repo, paths)
    if perr:
        out["error"] = perr
        return out
    if rels:
        rc, txt = await _git(repo, "add", "--", *rels)
    else:
        rc, txt = await _git(repo, "add", "-A")
    if rc != 0:
        out["error"] = f"git add falló: {txt[-200:]}"
        return out
    rc, txt, _ = await _git_out(repo, "diff", "--cached", "--name-only")
    staged = [ln for ln in txt.splitlines() if ln.strip()] if rc == 0 else []
    if not staged:
        out["error"] = "no hay nada staged para commitear"
        return out
    out["files"] = len(staged)
    rc, txt = await _git(repo, "commit", "-m", msg)
    if rc != 0:
        out["error"] = f"git commit falló: {txt[-300:]}"
        return out
    rc, sha = await _git(repo, "rev-parse", "--short", "HEAD")
    out["sha"] = sha.strip() if rc == 0 else ""
    out["committed"] = True
    logger.info("git_flow: commit %s en %s (%d archivos)",
                out["sha"], branch, out["files"])
    return out


async def push_branch(repo: str, branch: str) -> dict:
    """`git push -u origin <branch>`. Solo ramas de trabajo.

    Returns: {pushed, branch, output, error}
    """
    out: dict = {"pushed": False, "branch": branch, "output": "", "error": None}
    err = _guard_work_branch(branch)
    if err:
        out["error"] = err
        return out
    if not await is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    if not await _branch_exists(repo, branch):
        out["error"] = f"la rama local {branch} no existe"
        return out
    rc, txt = await _git(repo, "push", "-u", "origin", branch,
                         timeout=PUSH_TIMEOUT_S)
    out["output"] = txt.strip()[-500:]
    if rc != 0:
        out["error"] = f"push de {branch} falló: {txt[-300:]}"
        return out
    out["pushed"] = True
    logger.info("git_flow: push de %s a origin", branch)
    return out


async def open_pr(repo: str, branch: str, *, title: str,
                  body: str = "") -> dict:
    """Push + PR a develop, SIN cerrar la conversación.

    El /cerrar ya abría PR, pero cerraba el hilo con él: no había forma
    de publicar lo hecho y seguir trabajando. Este es ese camino. Si el
    PR ya existe devuelve su URL (mismo criterio que /cerrar).

    Returns: {pr_url, created, base, error}
    """
    out: dict = {"pr_url": None, "created": False, "base": DEVELOP_BRANCH,
                 "error": None}
    err = _guard_work_branch(branch)
    if err:
        out["error"] = err
        return out
    if not await is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    base = await detect_base_branch(repo)
    # Contra develop si ya existe; si no, contra el trunk. Al reves (contar
    # siempre contra develop) el `rc != 0` de una rama inexistente se
    # colaba como "hay commits" y el push creaba develop para un PR vacio.
    ref = (DEVELOP_BRANCH if await _branch_exists(repo, DEVELOP_BRANCH)
           else base)
    rc, txt = await _git(repo, "rev-list", "--count", f"{ref}..{branch}")
    if rc != 0 or not txt.strip() or txt.strip() == "0":
        out["error"] = (f"la rama {branch} no tiene commits propios sobre "
                        f"{ref}; commiteá algo antes del PR")
        return out
    err = await _ensure_develop(repo, base)
    if err:
        out["error"] = err
        return out
    push = await push_branch(repo, branch)
    if push["error"]:
        out["error"] = push["error"]
        return out
    rc, txt = await _exec(repo, "gh", "pr", "create",
                          "--base", DEVELOP_BRANCH, "--head", branch,
                          "--title", (title or branch).strip(),
                          "--body", body or "")
    if rc != 0:
        url = (await _existing_pr_url(repo, branch)
               if "already exists" in txt else None)
        if url:
            out["pr_url"] = url
            return out
        out["error"] = f"gh pr create falló: {txt[-300:]}"
        return out
    url = txt.strip().splitlines()[-1] if txt.strip() else ""
    out["pr_url"] = url or None
    out["created"] = bool(url)
    logger.info("git_flow: PR abierto (sin cerrar hilo) %s -> %s: %s",
                branch, DEVELOP_BRANCH, url)
    return out


async def merge_pr(repo: str, branch: str, *, method: str = "squash",
                   delete_branch: bool = True) -> dict:
    """Mergea el PR de la rama, SOLO si va a develop.

    Guard duro: si el PR apunta a main/master se rechaza. El flujo del
    repo es rama -> develop -> (PR a mano) -> main; un botón que mergea
    a main desde el visor de diff es exactamente lo que no queremos.

    Returns: {merged, pr_url, base, state, error}
    """
    out: dict = {"merged": False, "pr_url": None, "base": "", "state": "",
                 "error": None}
    err = _guard_work_branch(branch)
    if err:
        out["error"] = err
        return out
    if method not in ("squash", "merge", "rebase"):
        out["error"] = f"método de merge inválido: {method!r}"
        return out
    rc, txt = await _exec(repo, "gh", "pr", "view", branch, "--json",
                          "url,baseRefName,state,isDraft")
    if rc != 0:
        out["error"] = f"no encontré PR para {branch}: {txt[-200:]}"
        return out
    try:
        info = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        out["error"] = f"no pude leer el PR: {txt[-200:]}"
        return out
    out["pr_url"] = info.get("url")
    out["base"] = base = info.get("baseRefName") or ""
    out["state"] = state = info.get("state") or ""
    if state != "OPEN":
        out["error"] = f"el PR está {state or '?'}, no OPEN"
        return out
    if base != DEVELOP_BRANCH:
        out["error"] = (f"el PR apunta a {base!r}; desde acá solo se mergea "
                        f"a {DEVELOP_BRANCH} (el PR a main lo haces vos)")
        return out
    args = ["pr", "merge", branch, f"--{method}"]
    if delete_branch:
        args.append("--delete-branch")
    rc, txt = await _exec(repo, "gh", *args, timeout=PUSH_TIMEOUT_S)
    if rc != 0:
        out["error"] = f"gh pr merge falló: {txt[-300:]}"
        return out
    out["merged"] = True
    logger.info("git_flow: PR de %s mergeado (%s) a %s", branch, method, base)
    return out


async def sync_base(repo: str) -> dict:
    """`git fetch origin` + fast-forward de la base. No destructivo.

    Returns: {fetched, base, ref, behind, error}
    """
    out: dict = {"fetched": False, "base": "", "ref": "", "behind": 0,
                 "error": None}
    if not await is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    out["fetched"] = await fetch_origin_safe(repo)
    out["base"] = base = await work_base_branch(repo)
    rc, behind = await _git(repo, "rev-list", "--count",
                            f"{base}..origin/{base}")
    if rc == 0 and behind.strip().isdigit():
        out["behind"] = int(behind.strip())
    out["ref"] = await sync_base_with_origin(repo, base)
    if not out["fetched"]:
        out["error"] = "fetch de origin falló (sin red o auth vencida)"
    return out


async def restore_paths(repo: str, branch: str, paths: list[str]) -> dict:
    """Descarta los cambios sin commitear de esos archivos (destructivo).

    `--source=HEAD --staged --worktree`: vuelve el archivo a como está
    en el último commit, staged incluido. Es lo que el botón promete
    ("descartar mis cambios en este archivo"); un `git restore` pelado
    solo pisa el worktree con el índice y dejaría lo staged vivo.

    Los untracked no los borra: eso es `rm`, no `restore`, y borrar un
    archivo que git no conoce no tiene reflog del que volver.

    Returns: {restored: [paths], error}
    """
    out: dict = {"restored": [], "error": None}
    err = _guard_work_branch(branch)
    if err:
        out["error"] = err
        return out
    rels, perr = _safe_paths(repo, paths)
    if perr or not rels:
        out["error"] = perr or "hace falta al menos un path"
        return out
    if not await is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    current = await _current_branch(repo)
    if current != branch:
        out["error"] = (f"HEAD está en {current or '?'}, no en {branch}; "
                        f"no descarto cambios de una rama ajena")
        return out
    rc, txt = await _git(repo, "restore", "--source=HEAD", "--staged",
                         "--worktree", "--", *rels)
    if rc != 0:
        out["error"] = f"git restore falló: {txt[-300:]}"
        return out
    out["restored"] = rels
    logger.warning("git_flow: descartados cambios de %d archivo(s) en %s: %s",
                   len(rels), branch, ", ".join(rels[:5]))
    return out
