"""Gates de build/test y diagnóstico previo para el worker nocturno."""
from __future__ import annotations

import logging

from .night_plan import GateCleanupError
from .night_types import TRANSIENT_BUILD_SIGNATURES

logger = logging.getLogger("relay.night")


class NightGatesMixin:
    """Conserva gates y preflight con los ejecutores inyectables del worker."""

    # -- gates --

    @staticmethod
    def _is_transient_build_error(rc: int, out: str) -> bool:
        """El build falló por el restore en frío flaky del SDK, no por el
        código (ver TRANSIENT_BUILD_SIGNATURES). Ambas firmas presentes
        para no confundir con un error de NuGet real (paquete faltante)."""
        if rc == 0:
            return False
        return all(sig in out for sig in TRANSIENT_BUILD_SIGNATURES)

    async def _build_sh(self, cmd: str) -> tuple[int, str]:
        """Corre un build y reintenta UNA vez si falló por el restore en
        frío flaky (el segundo build encuentra el restore caliente). Un
        error real de compilación (CS####) no matchea → no se reintenta."""
        rc, out = await self._sh(cmd)
        if self._is_transient_build_error(rc, out):
            logger.warning(
                "build falló por restore en frío flaky (NuGet path1); "
                "reintento con restore caliente")
            rc, out = await self._sh(cmd)
        return rc, out

    async def _run_gates(self) -> tuple[bool, str]:
        """TDD estricto: build (si hay) + test (obligatorio) con exit 0."""
        if self.cfg.build_cmd:
            rc, out = await self._build_sh(self.cfg.build_cmd)
            if rc != 0:
                return False, f"build falló (exit {rc}):\n{out[-2000:]}"
        if not self.cfg.test_cmd:
            return False, ("sin test_cmd configurado ni auto-detectado: "
                           "TDD estricto → no se commitea sin gate de tests")
        rc, out = await self._sh(self.cfg.test_cmd)
        if rc != 0:
            return False, f"tests fallaron (exit {rc}):\n{out[-2000:]}"
        return True, ""

    async def _pre_flight_build(self) -> str:
        """Corre el build en master para detectar errores pre-existentes.

        Si falla, devuelve un bloque de texto con los errores para
        inyectar en el prompt del experto. Si pasa (o no hay
        build_cmd), devuelve string vacío.

        Esto cubre el caso típico: el repo master tiene tests rotos
        que NO son de la tarea actual. Sin pre-flight, el experto
        termina su trabajo y el gate rojo le bloquea el commit por
        algo que no le compete. Con pre-flight, ve los errores y
        puede arreglarlos como parte de su tarea.

        ponytail: timeout cap 180s (el build puede ser lento). Si
        supera, devolvemos string vacío y seguimos — el gate al
        final dirá si está roto.
        """
        if not self.cfg.build_cmd:
            return ""
        try:
            rc, out = await self._sh(self.cfg.build_cmd, timeout=180.0)
            if self._is_transient_build_error(rc, out):
                # Warmup: el pre-flight se come el restore en frío flaky
                # para que el gate final arranque caliente.
                rc, out = await self._sh(self.cfg.build_cmd, timeout=180.0)
        except GateCleanupError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("pre_flight_build explotó: %r", e)
            return ""
        if rc == 0:
            return ""
        # Filtrar errores de C# (cs####) — son los más comunes y los
        # más accionables. Si hay errores de otro tipo, también los
        # incluimos (último fallback).
        err_lines: list[str] = []
        for ln in out.splitlines():
            s = ln.strip()
            if not s:
                continue
            if " error CS" in s or "error MSB" in s:
                err_lines.append(s)
            elif err_lines and len(err_lines) < 30 and not s.startswith(" "):
                # líneas de resumen tipo "5 Errores" / "Tiempo..."
                err_lines.append(s)
        if not err_lines:
            return (f"\n\n⚠️ Pre-flight build falló (exit {rc}). "
                    f"Output (últimos 1500 chars):\n```\n{out[-1500:]}\n```\n")
        body = "\n".join(err_lines[:30])
        return (f"\n\n⚠️ Pre-flight build falló en master (exit {rc}). "
                f"La rama actual hereda estos errores pre-existentes. "
                f"Para que tu tarea pueda mergear, arreglálos primero "
                f"(o como parte de tu tarea) — el gate al final exigirá "
                f"que build y tests pasen.\n```\n{body}\n```\n")
