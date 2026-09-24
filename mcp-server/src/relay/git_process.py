"""git process: funciones del flujo git_flow."""
from __future__ import annotations
import asyncio
import logging

logger = logging.getLogger("relay.git_flow")

_GIT_NETWORK = frozenset({"fetch", "push", "pull", "clone", "ls-remote", "submodule"})


def _git_env(args: tuple[str, ...]) -> dict | None:
    index = 0
    while index < len(args):
        option = args[index]
        if option in {"-c", "-C"}:
            index += 2
            continue
        if option.startswith("-c") or option.startswith("-C"):
            index += 1
            continue
        break
    verb = args[index] if index < len(args) else ""
    return {"kind": "network"} if verb in _GIT_NETWORK else ({"kind": "commit"} if verb == "commit" else None)

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
        env = None
        kind = _git_env(args)
        if kind:
            from .github_credentials import git_env
            env = await git_env(commit=kind["kind"] == "commit")
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=repo,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"env": env} if env is not None else {}))
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
        env = None
        if program == "gh":
            from .github_credentials import gh_env
            env = await gh_env()
        proc = await asyncio.create_subprocess_exec(
            program, *args, cwd=repo,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **({"env": env} if env is not None else {}))
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
