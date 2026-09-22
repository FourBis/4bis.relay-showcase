"""Captura acotada del estado y diff del repositorio."""
from __future__ import annotations
import logging
import subprocess

logger = logging.getLogger("relay.experts")


GIT_CAPTURE_TIMEOUT_S = 5.0
DIFF_MAX_BYTES = 20_000
# Mensaje UNA VEZ por repo si git no está, para no spammear en cada run.
_git_unavailable_logged: set[str] = set()


def _capture_git_diff_sync(repo_path: str) -> dict:
    """Captura `git status --porcelain` y `git diff HEAD --no-color`.

    Devuelve dict:
        {"ok": bool, "status": str, "diff": str, "sha": str, "branch": str,
         "stderr": str}

    Best-effort total: cualquier excepción / repo sin git / git no
    instalado → {"ok": False, "status": "skipped"}.

    Cambio 2026-07-08 (Sub-ola 2.7): ahora stdout y stderr van separados.
    Antes se concatenaban, lo que contaminaba el campo `diff` con
    warnings de git (mensajes en stderr según la locale: "nothing to
    commit, working tree clean", warnings de permisos, etc.) que
    terminaban inflando el response del endpoint admin y colgando el
    modal de la UI. `stderr` se expone para diagnóstico pero NO se
    mezcla con `diff`.
    """
    def _run(args: list[str], timeout: float = GIT_CAPTURE_TIMEOUT_S) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=repo_path, capture_output=True, text=True,
                # `text=True` a secas decodifica con la locale (cp1252 en
                # Windows) y un diff con acentos/UTF-8 revienta con
                # UnicodeDecodeError DENTRO del reader thread de
                # subprocess: el traceback se imprime suelto en el log y
                # acá volvía stdout vacío, o sea el diff se perdía en
                # silencio. git habla UTF-8: decodificamos como tal, y lo
                # que no entre se reemplaza (bug 2026-07-21).
                encoding="utf-8", errors="replace",
                timeout=timeout, check=False,
            )
            return proc.returncode, (proc.stdout or ""), (proc.stderr or "")
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return 1, "", ""

    # 1) ¿es un repo git?
    rc, _, _ = _run(["rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        if str(repo_path) not in _git_unavailable_logged:
            logger.info("git: %s no es working tree git (salteando bloque diff)", repo_path)
            _git_unavailable_logged.add(str(repo_path))
        return {"ok": False, "status": "not_git_repo"}

    # 2) branch + HEAD sha (best-effort, 2 calls en paralelo? no, simple)
    _, branch_text, _ = _run(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_text.strip() or "HEAD"
    _, sha_text, _ = _run(["rev-parse", "--short", "HEAD"])
    sha = sha_text.strip() or "?"

    # 3) status --porcelain (solo stdout; stderr es warnings de git)
    _, status_text, status_err = _run(["status", "--porcelain"])

    # 4) diff --no-color (solo stdout)
    rc, diff_text, diff_err = _run(["diff", "HEAD", "--no-color"])

    # stderr combinado para diagnóstico (NO se mezcla con diff)
    diag_stderr = (status_err + diff_err).strip()

    return {
        "ok": True,
        "status": status_text.strip(),
        "diff": diff_text,
        "sha": sha,
        "branch": branch,
        "stderr": diag_stderr,
    }


def _build_git_diff_block_sync(repo_path: str) -> str:
    """Compone el bloque markdown '## Cambios en el workspace (git)'.

    Devuelve "" si no es repo git o si git no está disponible.
    Aplica cap DIFF_MAX_BYTES al diff (trunca con footer).
    """
    info = _capture_git_diff_sync(repo_path)
    if not info.get("ok"):
        return ""

    status = info.get("status", "")
    diff = info.get("diff", "")
    branch = info.get("branch", "?")
    sha = info.get("sha", "?")

    lines = [
        "## Cambios en el workspace (git)",
        "",
        f"Branch: `{branch}` • HEAD: `{sha}`",
        "",
    ]
    if not status and not diff:
        lines.append("Working tree clean. Sin cambios pendientes.")
    else:
        if status:
            lines.append("Status (`git status --porcelain`):")
            lines.append("```")
            for ln in status.splitlines():
                if ln.strip():
                    lines.append(ln)
            lines.append("```")
            lines.append("")
        if diff:
            diff_text = diff
            truncated = False
            if len(diff_text.encode("utf-8")) > DIFF_MAX_BYTES:
                # Truncar en frontera de línea.
                truncated_bytes = diff_text.encode("utf-8")[:DIFF_MAX_BYTES].decode("utf-8", errors="ignore")
                last_nl = truncated_bytes.rfind("\n")
                if last_nl > 0:
                    truncated_bytes = truncated_bytes[:last_nl]
                diff_text = truncated_bytes
                truncated = True
            lines.append("Diff (`git diff HEAD --no-color`):")
            lines.append("```diff")
            lines.append(diff_text.rstrip())
            lines.append("```")
            if truncated:
                lines.append("")
                lines.append(
                    f"_Diff truncado a {DIFF_MAX_BYTES // 1024}KB. "
                    f"Para el resto: `git show {sha}` o `git diff {sha}~1..{sha}`._"
                )
    return "\n".join(lines)
