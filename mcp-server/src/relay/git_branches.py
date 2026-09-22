"""git branches: funciones del flujo git_flow."""
from __future__ import annotations
import logging
import re
import time
from typing import Optional
from . import git_conversations, git_process

logger = logging.getLogger("relay.git_flow")

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
    rc, _ = await git_process._git(repo, "rev-parse", "--is-inside-work-tree")
    return rc == 0


async def has_commits(repo: str) -> bool:
    """True si HEAD apunta a un commit. False en un repo recién `git init`
    sin commit (HEAD unborn), aunque tenga refs de `origin/*` traídas por
    un fetch. Ahí no hay base local de la cual sacar la rama de trabajo, y
    forzar un checkout de `origin/<base>` sobre archivos untracked los
    destruiría. El caller degrada a conversación sin rama."""
    rc, _ = await git_process._git(repo, "rev-parse", "--verify", "--quiet", "HEAD")
    return rc == 0


async def detect_base_branch(repo: str) -> str:
    """Rama base del repo: 'main' o 'master' (u otra si origin/HEAD apunta ahí).

    Primero intenta `origin/HEAD`; si no hay remoto, prueba main y master
    locales; default 'main'."""
    rc, out = await git_process._git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    ref = out.strip()
    if rc == 0 and ref:
        # refs/remotes/origin/main → main
        return ref.rsplit("/", 1)[-1]
    for cand in ("main", "master"):
        rc, _ = await git_process._git(repo, "rev-parse", "--verify", "--quiet", cand)
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
    if await _branch_exists(repo, git_process.DEVELOP_BRANCH):
        return git_process.DEVELOP_BRANCH
    if await _branch_exists(repo, f"origin/{git_process.DEVELOP_BRANCH}"):
        return f"origin/{git_process.DEVELOP_BRANCH}"
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
    rc, out = await git_process._git(repo, *args)
    if rc != 0:
        return False, f"git status falló (rc={rc}): {out[-200:]}"
    return (not out.strip()), out.strip()[:300]


async def _branch_exists(repo: str, branch: str) -> bool:
    rc, _ = await git_process._git(repo, "rev-parse", "--verify", "--quiet", branch)
    return rc == 0


async def branch_status(repo: str, branch: str,
                        base: str = git_process.DEVELOP_BRANCH) -> dict:
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
    out["current_branch"] = await git_conversations._current_branch(repo)
    out["is_current"] = (out["current_branch"] == branch)
    if not await _branch_exists(repo, branch):
        return out
    out["exists"] = True
    # merged: listamos las ramas que están en `base` y vemos si la nuestra
    # aparece. Salida 1 línea por rama.
    rc, merged_out = await git_process._git(repo, "branch", "--list",
                                branch, "--merged", base)
    if rc == 0:
        # `git branch --list` muestra la rama con un '* ' si es la actual;
        # strip espacios/asterisco para comparar.
        for line in merged_out.splitlines():
            if line.strip().lstrip("*").strip() == branch:
                out["merged"] = True
                break
    # ahead/behind: rev-list --count sobre los dos lados.
    rc_a, a = await git_process._git(repo, "rev-list", "--count", f"{base}..{branch}")
    rc_b, b = await git_process._git(repo, "rev-list", "--count", f"{branch}..{base}")
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
    current = await git_conversations._current_branch(repo)
    out["was_current"] = (current == branch)
    if out["was_current"]:
        out["error"] = (f"no puedo borrar la rama actual ({branch}); "
                        f"haz checkout a otra rama primero")
        return out
    flag = "-D" if force else "-d"
    rc, txt = await git_process._git(repo, "branch", flag, branch)
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
    realidad del repo). Caso de ejemplo: un chat donde el bot leyó
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
    rc, out = await git_process._git(repo, "fetch", "origin", timeout=timeout)
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
    rc_a, ahead = await git_process._git(repo, "rev-list", "--count", f"{remote}..{base}")
    rc_b, behind = await git_process._git(repo, "rev-list", "--count", f"{base}..{remote}")
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
    if await git_conversations._current_branch(repo) == base:
        rc, out = await git_process._git(repo, "merge", "--ff-only", remote)
    else:
        rc, out = await git_process._git(repo, "update-ref", "-m",
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
    if branch in git_process.PROTECTED_BRANCHES:
        out["error"] = f"{branch} es rama protegida; no la borro"
        logger.warning("git_flow: %s", out["error"])
        return out
    base = await work_base_branch(repo)
    if base.startswith("origin/"):
        base = await detect_base_branch(repo)   # no hay local donde pararse
    out["base"] = base
    rc, txt = await git_process._git(repo, "checkout", base)
    if rc != 0:
        out["error"] = f"checkout {base} falló, dejo la rama: {txt[-200:]}"
        return out
    res = await delete_local_branch(repo, branch, force=True)
    out["deleted"] = bool(res["deleted"])
    out["error"] = res["error"]
    return out
