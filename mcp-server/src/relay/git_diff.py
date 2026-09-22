"""git diff: funciones del flujo git_flow."""
from __future__ import annotations
from pathlib import Path
from typing import Optional
from . import git_branches, git_conversations, git_process

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
    rc, out = await git_process._git(repo, "merge-base", a, b)
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
    base = await _merge_base(repo, await git_branches.work_base_branch(repo), branch)
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
    if not await git_branches.is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    out["base"] = base = await git_branches.work_base_branch(repo)
    out["is_current"] = is_current = (await git_conversations._current_branch(repo)) == branch
    if not await git_branches._branch_exists(repo, branch):
        out["error"] = (f"la rama local {branch} ya no existe "
                        f"(se borra al abrir el PR; el diff está en GitHub)")
        return out
    out["exists"] = True
    rc, count = await git_process._git(repo, "rev-list", "--count", f"{base}..{branch}")
    if rc == 0 and count.strip().isdigit():
        out["commits"] = int(count.strip())
    rango = await _diff_range(repo, branch, mode, is_current)
    rc_n, numstat, err_n = await git_process._git_out(repo, "diff", "--numstat", "-z",
                                          "-M", *rango)
    rc_s, namest, _ = await git_process._git_out(repo, "diff", "--name-status", "-z",
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
        rc_p, pend, _ = await git_process._git_out(repo, "diff", "--name-only", "-z",
                                       "HEAD")
        if rc_p == 0:
            pendientes = {p for p in pend.split("\0") if p}
        rc_u, uns, _ = await git_process._git_out(repo, "ls-files", "--others",
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
    if await git_branches._branch_exists(repo, f"origin/{branch}"):
        rc, ahead = await git_process._git(repo, "rev-list", "--count",
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
    if not await git_branches.is_git_repo(repo):
        out["error"] = f"{repo} no es un repo git"
        return out
    is_current = (await git_conversations._current_branch(repo)) == branch
    rango = await _diff_range(repo, branch, mode, is_current)
    ctx = max(0, min(int(context), 25))
    rel_old = _rel_inside(repo, old_path) if old_path else None
    pathspec = [rel] + ([rel_old] if rel_old and rel_old != rel else [])
    rc, texto, err = await git_process._git_out(repo, "diff", f"--unified={ctx}", "-M",
                                    *rango, "--", *pathspec)
    if rc != 0:
        out["error"] = f"git diff falló: {err[-200:]}"
        return out
    if not texto.strip():
        # Ningún cambio trackeado: puede ser un untracked (o un path que
        # ya no está en el rango porque cambió el modo).
        rc_u, uns, _ = await git_process._git_out(repo, "ls-files", "--others",
                                      "--exclude-standard", "-z", "--", rel)
        if rc_u == 0 and rel in {p for p in uns.split("\0") if p}:
            out["untracked"] = True
            texto, out["binary"] = _untracked_diff(repo, rel)
    if texto.startswith("Binary files ") or "\nBinary files " in texto:
        out["binary"] = True
    out["diff"], out["full_size"], out["truncated"] = git_conversations._cap(texto, cap)
    return out
