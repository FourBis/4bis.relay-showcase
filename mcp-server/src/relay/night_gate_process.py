"""Fail-closed termination of the process tree owned by a shell gate."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import subprocess

CLEANUP_TIMEOUT_S = 15.0


class GateCleanupError(RuntimeError):
    """Tree termination/reaping is uncertain: the caller must not change checkout."""


async def finish_cleanup(coro):
    """Repeated cancellation cannot return control while cleanup is in flight."""
    cleanup = asyncio.create_task(coro)
    while True:
        try:
            return await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            if cleanup.cancelled():
                raise GateCleanupError("La limpieza del gate fue cancelada; requiere recuperación manual") from None


async def terminate_tree(proc, reader):
    """Kill the owned session/tree, then confirm process exit and pipe EOF.

    On Windows a shell that already exited may no longer identify its children
    to taskkill. That is an uncertain cleanup, never a successful rollback gate.
    """
    try:
        if os.name == "nt":
            if proc.returncode is not None:
                raise GateCleanupError("El shell ya terminó; no se puede confirmar su árbol de procesos por PID")
            executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            result = await asyncio.to_thread(subprocess.run,
                [str(executable), "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, check=False, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode != 0:
                raise GateCleanupError("taskkill no confirmó el cierre del árbol del gate")
        else:
            try:
                # run_gate starts a new session, whose process group is its PID.
                # Do not resolve getpgid(parent): the shell may already be dead.
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # The owned process group no longer exists.
        await asyncio.wait_for(asyncio.shield(reader), timeout=CLEANUP_TIMEOUT_S)
        await asyncio.wait_for(proc.wait(), timeout=CLEANUP_TIMEOUT_S)
    except Exception as error:
        # A direct kill can reduce the damage, but cannot certify tree cleanup.
        try:
            proc.kill()
        except OSError:
            pass
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=CLEANUP_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            pass
        if isinstance(error, GateCleanupError):
            raise
        raise GateCleanupError("No se confirmó el cierre del árbol del gate; requiere recuperación manual") from error
