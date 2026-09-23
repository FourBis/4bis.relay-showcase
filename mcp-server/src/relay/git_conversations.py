"""git conversations: funciones del flujo git_flow."""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional
from . import git_branches, git_process

logger = logging.getLogger("relay.git_flow")

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
    rc, out = await git_process._git(repo, "rev-parse", "--git-common-dir")
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
    if not await git_branches.is_git_repo(repo):
        raise git_process.GitFlowError(f"{repo} no es un repo git")
    # Best-effort fetch. Si falla (red/auth) seguimos con lo que haya —
    # el caller igual va a poder crear la rama, solo que contra refs
    # potencialmente stale.
    await git_branches.fetch_origin_safe(repo)
    # Excluir los artefactos de tooling ANTES del check: así ni bloquean
    # /nuevo ni se cuelan al PR en el `git add -A` de /cerrar.
    await ensure_local_excludes(repo)
    # Solo cambios TRACKEADOS bloquean: branchear sobre untracked es seguro
    # y esos suelen ser artefactos de tooling (.claude/, .mcp.json, …) que
    # el usuario no considera cambios. Ver working_tree_clean.
    clean, detail = await git_branches.working_tree_clean(repo, include_untracked=False)
    if not clean:
        raise git_process.GitFlowError(
            "hay cambios trackeados sin commitear, no puedo crear una rama "
            f"limpia: {detail}. Commiteá o descartá esos cambios (los "
            "archivos sin trackear no molestan).")
    # Fast-forward de la base al remoto: sin esto el fetch de arriba solo
    # actualizaba `origin/*` y la rama nueva salía del local stale.
    base = await git_branches.sync_base_with_origin(repo, await git_branches.work_base_branch(repo))
    branch = await git_branches.unique_branch_name(repo, author)
    rc, out = await git_process._git(repo, "checkout", "-b", branch, base)
    if rc != 0:
        raise git_process.GitFlowError(f"checkout -b {branch} desde {base} falló: {out[-200:]}")
    logger.info("git_flow: rama %s creada desde %s", branch, base)
    return branch


async def _ensure_develop(repo: str, base: str) -> Optional[str]:
    """Crea la rama `develop` (trunk) desde base si no existe (local ni remota).

    Devuelve un mensaje de error o None si OK. Empuja develop al remoto para
    que el PR tenga contra qué abrirse. Deja el working tree en la rama en
    la que estaba (no cambia el checkout)."""
    if await git_branches._branch_exists(repo, git_process.DEVELOP_BRANCH):
        return None
    if await git_branches._branch_exists(repo, f"origin/{git_process.DEVELOP_BRANCH}"):
        return None  # existe remota; el PR puede targetearla igual
    # Crear develop desde base sin mover el checkout actual: `git branch`.
    rc, out = await git_process._git(repo, "branch", git_process.DEVELOP_BRANCH, base)
    if rc != 0:
        return f"crear rama develop desde {base} falló: {out[-200:]}"
    rc, out = await git_process._git(repo, "push", "-u", "origin", git_process.DEVELOP_BRANCH,
                         timeout=git_process.PUSH_TIMEOUT_S)
    if rc != 0:
        return f"push de develop falló: {out[-200:]}"
    logger.info("git_flow: rama develop creada desde %s y pusheada", base)
    return None


async def _current_branch(repo: str) -> str:
    rc, out = await git_process._git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if rc == 0 else ""


async def _autocommit_pending(repo: str, branch: str, message: str) -> Optional[str]:
    """Commitea lo que el experto dejó sin commitear en la rama.

    El experto LLM no siempre commitea (y antes eso hacía fallar el
    /cerrar con "sin commits propios" + dejaba el tree sucio, lo que
    encima bloqueaba el próximo /nuevo). El relay lo hace determinista.

    Devuelve mensaje de error o None si OK (incluye "nada que commitear").
    """
    clean, _ = await git_branches.working_tree_clean(repo)
    current = await _current_branch(repo)
    if current != branch:
        # /nuevo dejó HEAD en la rama; si algo lo movió, solo seguimos
        # si el tree está limpio (checkout seguro). Sucio en otra rama
        # = estado anómalo: commitear ahí mezclaría trabajo ajeno.
        if not clean:
            return (f"HEAD está en {current or '?'} (no {branch}) con cambios "
                    f"sin commitear; resuelve a mano y reintenta /cerrar")
        rc, out = await git_process._git(repo, "checkout", branch)
        if rc != 0:
            return f"checkout {branch} falló: {out[-200:]}"
        clean, _ = await git_branches.working_tree_clean(repo)
    if clean:
        return None
    rc, out = await git_process._git(repo, "add", "-A")
    if rc != 0:
        return f"git add falló: {out[-200:]}"
    rc, out = await git_process._git(repo, "commit", "-m", message)
    if rc != 0:
        return f"git commit falló: {out[-300:]}"
    logger.info("git_flow: auto-commit de cambios pendientes en %s", branch)
    return None


async def _existing_pr_url(repo: str, branch: str) -> Optional[str]:
    """URL del PR abierto de `branch`, si ya existe (reintento de /cerrar
    después de un fallo parcial, o PR abierto a mano)."""
    rc, out = await git_process._exec(repo, "gh", "pr", "view", branch,
                          "--json", "url", "--jq", ".url")
    url = out.strip().splitlines()[-1] if out.strip() else ""
    return url if rc == 0 and url.startswith("http") else None


async def branch_diff(repo: str, base: str, branch: str) -> tuple[str, str]:
    """(stat, diff) de `base...branch`. ("", "") si git falla."""
    rc, stat, _ = await git_process._git_out(repo, "diff", "--stat", f"{base}...{branch}")
    if rc != 0:
        return "", ""
    rc, diff, _ = await git_process._git_out(repo, "diff", f"{base}...{branch}")
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
    if not await git_branches.is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    out["base"] = base = await git_branches.work_base_branch(repo)
    out["is_current"] = (await _current_branch(repo)) == branch
    if not await git_branches._branch_exists(repo, branch):
        out["error"] = (f"la rama local {branch} ya no existe "
                        f"(se borra al abrir el PR; el diff está en GitHub)")
        return out
    out["exists"] = True
    rc, count = await git_process._git(repo, "rev-list", "--count", f"{base}..{branch}")
    if rc == 0 and count.strip().isdigit():
        out["commits"] = int(count.strip())
    stat, diff = await branch_diff(repo, base, branch)
    out["stat"] = stat
    out["diff"], out["full_size"], out["truncated"] = _cap(diff, cap)
    if out["is_current"]:
        # `diff HEAD` = staged + unstaged, o sea todo lo que el experto
        # tocó y todavía no commiteó.
        rc, pstat, _ = await git_process._git_out(repo, "diff", "--stat", "HEAD")
        rc2, pdiff, _ = await git_process._git_out(repo, "diff", "HEAD")
        if rc == 0:
            out["pending_stat"] = pstat.strip()
        if rc2 == 0:
            (out["pending_diff"], out["pending_full_size"],
             out["pending_truncated"]) = _cap(pdiff, cap)
        rc, uns, _ = await git_process._git_out(repo, "ls-files", "--others",
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
        if not await git_branches.is_git_repo(repo):
            result["error"] = f"{repo} no es un repo git"
            return result
        base = await git_branches.detect_base_branch(repo)
        # Cambios sin commitear del experto → commit determinista del relay.
        had_pending = not (await git_branches.working_tree_clean(repo))[0]
        err = await _autocommit_pending(repo, branch, title)
        if err:
            result["error"] = err
            logger.warning("git_flow: %s", err)
            return result
        result["committed"] = had_pending
        # ¿Hay commits propios en la rama vs base? Sin commits no hay PR.
        rc, out = await git_process._git(repo, "rev-list", "--count", f"{base}..{branch}")
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
        rc, out = await git_process._git(repo, "push", "-u", "origin", branch,
                             timeout=git_process.PUSH_TIMEOUT_S)
        if rc != 0:
            result["error"] = f"push de {branch} falló: {out[-200:]}"
            logger.warning("git_flow: %s", result["error"])
            return result
        # PR contra develop.
        draft_args = ("--draft",) if result["draft"] else ()
        rc, out = await git_process._exec(
            repo, "gh", "pr", "create",
            "--base", git_process.DEVELOP_BRANCH, "--head", branch,
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
        logger.info("git_flow: PR abierto %s → %s: %s", branch, git_process.DEVELOP_BRANCH, url)
        return result
    except Exception as e:  # noqa: BLE001 — best-effort, nunca romper el /cerrar
        result["error"] = f"{type(e).__name__}: {e}"
        logger.exception("git_flow: finalize_conversation_pr explotó")
        return result
