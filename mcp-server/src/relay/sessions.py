"""Registro de sesiones VS Code handshakeadas (ADR-009, compat).

El push por SSE ya no se usa (2026-08-10). La extensión solo
handshakea: el relay guarda un registro en memoria con el último
target (workspace folder name) para que el bot Discord arme un menú
de targets vivos en `GET /sessions` y para que `commands.list_sessions`
liste qué VS Code están conectadas. Si la VS Code muere, su sesión
cae cuando el relay reinicia (no hay persistencia — son liveness).

    sessions[sessionId] = SessionState(
        session_id=<vscode.env.sessionId>,
        name=<legible>,
        machine_id=<vscode machine UUID>,
        last_target=<workspaceFolders[0].name> or None,
        last_handshake_ts=<ISO 8601>,
        last_workspace_folders=[{path, name}, ...] or [],
    )

Concurrencia: un lock async protege el dict.

LIFO: si llega un handshake con sessionId ya conocido, la sesión se
sobrescribe (upsert) — el handshake más reciente gana. Antes había
un writer SSE que se reemplazaba; ya no.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Optional


_SID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_\- .]{1,64}$")


def validate_sid(sid: Any) -> str:
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        raise ValueError("sessionId debe ser string alfanumérico 8-128 chars")
    return sid


def validate_name(name: Any) -> str:
    if not isinstance(name, str):
        raise ValueError("name debe ser string")
    n = name.strip()
    if not n:
        raise ValueError("name vacío")
    if not _NAME_RE.match(n):
        raise ValueError("name tiene caracteres inválidos (1-64, letras/dígitos/_/-/espacio/.)")
    return n


def normalize_target(workspace_folders: Any) -> Optional[str]:
    """Devuelve `workspaceFolders[0].name` o None si no hay."""
    if not isinstance(workspace_folders, list) or not workspace_folders:
        return None
    first = workspace_folders[0]
    if not isinstance(first, dict):
        return None
    name = first.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


# ponytail: helper minimo para ADR-011. No clase, no dataclass,
# no settings. Solo lo que el server necesita.
def extract_workspace_block(workspace_folders: Any) -> str:
    """Devuelve el bloque markdown "## Workspace abierto" o "" si no aplica.

    Reglas (ADR-011):
    - Si no hay folder con `path` válido → devuelve "" (no inventar).
    - Toma `folders[0].path` como cwd primario y lo nombra con `name`
      (si viene) entre corchetes.
    - Si hay más de un folder con path, los lista como sub-bullets
      "  - name: path" para que el LLM sepa que existen.
    - Folders sin path o sin name se ignoran silenciosamente.

    Se usa en `experts.build_instructions` para inyectar el cwd del
    proyecto en el system prompt de los expertos (no del push, que
    ya no existe). El push path usaba `last_workspace_folders` de la
    sesión; acá se llama con una lista armada ad-hoc del project.
    """
    if not isinstance(workspace_folders, list) or not workspace_folders:
        return ""

    from pathlib import Path as _P
    paths: list[tuple[str, str]] = []  # (name, path)
    for folder in workspace_folders:
        if not isinstance(folder, dict):
            continue
        path = folder.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        name = folder.get("name")
        label = name.strip() if isinstance(name, str) and name.strip() else _P(path).name or path
        paths.append((label, path.strip()))

    if not paths:
        return ""

    primary_name, primary_path = paths[0]
    lines = [
        "## Workspace abierto",
        "",
        f"Cwd absoluto (primary): `{primary_path}` [{primary_name}]",
        "Usa esta ruta como base para `read_file`, `list_directory`,",
        "`run_in_terminal`, etc.",
    ]
    if len(paths) > 1:
        lines.append("")
        lines.append("Otras carpetas abiertas en esta ventana:")
        for name, path in paths[1:]:
            lines.append(f"- **{name}**: `{path}`")
    return "\n".join(lines)


@dataclass
class SessionState:
    """Estado vivo de una sesión VS Code handshakeada."""

    session_id: str
    name: str
    machine_id: str
    last_target: Optional[str] = None
    last_handshake_ts: Optional[str] = None  # ISO 8601

    # Lista cruda del último handshake. Vacía si la VS Code no envió
    # folder. Cada item es `{"path": str, "name": str}`. Se guarda
    # para debug en /sessions; el bloque de workspace en el system
    # prompt (ADR-011) se inyectaba vía POST /prompts, que ya no se
    # ejerce. Si vuelve a haber push, se reactiva el uso.
    last_workspace_folders: list[dict] = field(default_factory=list)


class SessionRegistry:
    """Mantiene las sesiones VS Code handshakeadas en memoria."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()

    # ---- handshake ----

    async def upsert_from_handshake(
        self,
        session_id: str,
        name: str,
        machine_id: str,
        workspace_folders: Any,
        ts: str,
    ) -> SessionState:
        """Crea o actualiza la sesión. Devuelve el estado (nuevo o
        actualizado). Si la sesión ya existía, upsert: el handshake
        más reciente gana (LIFO por sessionId)."""
        session_id = validate_sid(session_id)
        name = validate_name(name)
        if not isinstance(machine_id, str) or not machine_id:
            raise ValueError("machineId requerido")
        last_target = normalize_target(workspace_folders)

        async with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                sess = SessionState(
                    session_id=session_id,
                    name=name,
                    machine_id=machine_id,
                )
                self._sessions[session_id] = sess
            else:
                sess.name = name
                sess.machine_id = machine_id

            sess.last_target = last_target
            sess.last_handshake_ts = ts
            if isinstance(workspace_folders, list) and workspace_folders:
                cleaned: list[dict] = []
                for folder in workspace_folders:
                    if not isinstance(folder, dict):
                        continue
                    path = folder.get("path")
                    name_v = folder.get("name")
                    if not isinstance(path, str) or not path.strip():
                        continue
                    if not isinstance(name_v, str) or not name_v.strip():
                        name_v = ""
                    cleaned.append({"path": path.strip(), "name": name_v.strip()})
                sess.last_workspace_folders = cleaned
            else:
                sess.last_workspace_folders = []
            return sess

    # ---- snapshot ----

    async def list_sessions(self) -> list[dict[str, Any]]:
        """Snapshot serializable de todas las sesiones. Para debug y
        para que el bot Discord arme un menú de targets."""
        async with self._lock:
            return [
                {
                    "session_id": s.session_id,
                    "name": s.name,
                    "machineId": s.machine_id,
                    "last_target": s.last_target,
                    "last_handshake_ts": s.last_handshake_ts,
                    "workspace_folders": s.last_workspace_folders,
                }
                for s in self._sessions.values()
            ]

    # ---- lookup ----

    async def list_targets(self) -> list[str]:
        async with self._lock:
            targets = sorted({
                sess.last_target for sess in self._sessions.values()
                if sess.last_target
            })
            return targets
