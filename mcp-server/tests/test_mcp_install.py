"""Tests F2 del plan MCP_REGISTRY: pipeline de install desde GitHub.

Cubre:
  - POST /admin/api/mcp/install arranca un job, devuelve 202 con job_id.
  - El pipeline (clone + scan + vet) corre async y termina en
    awaiting_confirm (o failed según monkeypatches).
  - POST /admin/api/mcp/install/{id}/confirm corre el handshake +
    materializa la fila en mcp_servers.
  - Override del humano en confirm: command/args/env/capability/name.
  - Estados de error: clone falla, handshake falla, job inexistente.

Cómo se testea sin red ni git:
  Monkeypatcheamos TODAS las piezas pesadas del installer
  (`clone_repo`, `static_scan`, `vet_with_llm`, `detect_run_command`,
  `run_handshake`). El orquestador (`McpInstaller`) corre real.
  El LLM vetting stub que devuelve "unknown" se monkeypatchea a un
  veredicto propio para validar el wiring completo.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_mcp_install.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from relay.db import Database
from relay.experts import ModelUnavailable, run_expert as experts_run_expert
from relay.mcp_installer import (
    InstallerState, McpInstaller,
    clone_repo, static_scan, vet_with_llm,
    detect_run_command, run_handshake,
    _parse_vetting_response,
)

# Misma lógica que admin.py para resolver el AppKey bajo pytest:
# sys.modules["__main__"] es pytest, no relay.server.
_main = sys.modules.get("__main__")
if _main is not None and getattr(_main, "MCP_INSTALLER_KEY", None) is not None:
    MCP_INSTALLER_KEY = _main.MCP_INSTALLER_KEY
else:
    from relay.server import MCP_INSTALLER_KEY  # type: ignore


_ENV_KEYS = ("FOURBIS_DB_PATH", "FOURBIS_CHATS_DIR", "FOURBIS_JSONL_DIR",
             "FOURBIS_MCP_INSTALLS_DIR")


async def _wait_state(
    installer: McpInstaller, job_id: str,
    *targets: "InstallerState", timeout_s: float = 2.0,
) -> str:
    """Espera (polling) hasta que el job llegue a uno de los `targets`."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    seen: list[str] = []
    while loop.time() < deadline:
        job = installer.get(job_id)
        if job is not None:
            seen.append(job.state.value)
            if job.state in targets:
                return job.state.value
        await asyncio.sleep(0.05)
    job = installer.get(job_id)
    final = job.state.value if job else "None"
    raise AssertionError(
        f"job {job_id} no llegó a {[t.value for t in targets]} "
        f"en {timeout_s}s. Visto: {seen}. Final: {final}")


def _good_config() -> dict:
    return {"command": "echo", "args": ["hello"],
            "env": {}, "needs_manual": False}


def _needs_manual_config() -> dict:
    return {"command": "", "args": [], "env": {}, "needs_manual": True}


class TestMcpInstallPipeline(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.environ["FOURBIS_DB_PATH"] = str(base / "test.db")
        os.environ["FOURBIS_CHATS_DIR"] = str(base / "chats")
        os.environ["FOURBIS_JSONL_DIR"] = str(base / "jsonl")
        os.environ["FOURBIS_MCP_INSTALLS_DIR"] = str(base / "installs")
        self.db = Database()
        await self.db.init_schema()

        self._patches: list = []

        from relay.server import create_app
        self.app = create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.installer: McpInstaller = self.app[MCP_INSTALLER_KEY]

    async def asyncTearDown(self) -> None:
        for p in self._patches:
            p.stop()
        await self.client.close()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def _patch(self, target, value):
        """Monkeypatchea `target` (función) con `value` en su módulo."""
        p = patch(f"{target.__module__}.{target.__name__}", value)
        p.start()
        self._patches.append(p)
        return p

    async def test_install_arranca_y_termina_en_awaiting(self) -> None:
        """Happy path: clone + scan + vet → awaiting_confirm."""
        self._patch(clone_repo, AsyncMock(return_value="abc1234"))
        self._patch(static_scan, AsyncMock(return_value=[]))
        self._patch(vet_with_llm, AsyncMock(
            return_value=("safe", "todo bien")))
        self._patch(detect_run_command, lambda _d: _good_config())

        r = await self.client.post(
            "/admin/api/mcp/install",
            json={"url": "https://github.com/org/mcp-toy"})
        self.assertEqual(r.status, 202)
        body = await r.json()
        self.assertIn("job_id", body)
        self.assertEqual(body["state"], "pending")
        job_id = body["job_id"]

        final = await _wait_state(
            self.installer, job_id, InstallerState.AWAITING_CONFIRM)
        self.assertEqual(final, "awaiting_confirm")

        r = await self.client.get(
            f"/admin/api/mcp/install/{job_id}")
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["state"], "awaiting_confirm")
        self.assertEqual(body["source_commit"], "abc1234")
        self.assertEqual(body["vet_verdict"], "safe")
        self.assertEqual(body["proposal"]["command"], "echo")

    async def test_confirm_materializa_fila_con_handshake_ok(self) -> None:
        self._patch(clone_repo, AsyncMock(return_value="abc1234"))
        self._patch(static_scan, AsyncMock(return_value=[]))
        self._patch(vet_with_llm, AsyncMock(return_value=("safe", "")))
        self._patch(detect_run_command, lambda _d: _good_config())
        self._patch(run_handshake, AsyncMock(return_value=(True, "")))

        r = await self.client.post(
            "/admin/api/mcp/install",
            json={"url": "https://github.com/org/mcp-toy"})
        job_id = (await r.json())["job_id"]
        await _wait_state(self.installer, job_id,
                          InstallerState.AWAITING_CONFIRM)

        r = await self.client.post(
            f"/admin/api/mcp/install/{job_id}/confirm", json={})
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["state"], "healthy")

        r = await self.client.get("/admin/api/mcp")
        names = [m["name"] for m in (await r.json())["mcp_servers"]]
        self.assertIn("mcp-toy", names)
        row = next(m for m in (await r.json())["mcp_servers"]
                   if m["name"] == "mcp-toy")
        self.assertTrue(row["enabled"])
        self.assertEqual(row["health"], "ok")
        self.assertEqual(row["vet_verdict"], "safe")

    async def test_confirm_con_override_y_needs_manual(self) -> None:
        self._patch(clone_repo, AsyncMock(return_value="deadbeef"))
        self._patch(static_scan, AsyncMock(
            return_value=["postinstall: x"]))
        self._patch(vet_with_llm, AsyncMock(
            return_value=("suspect", "ojo con esto")))
        self._patch(detect_run_command, lambda _d: _needs_manual_config())
        self._patch(run_handshake, AsyncMock(return_value=(True, "")))

        r = await self.client.post(
            "/admin/api/mcp/install",
            json={"url": "https://github.com/org/w-e-i-r-d"})
        job_id = (await r.json())["job_id"]
        await _wait_state(self.installer, job_id,
                          InstallerState.AWAITING_CONFIRM)

        # Sin override → 400 (needs_manual).
        r = await self.client.post(
            f"/admin/api/mcp/install/{job_id}/confirm", json={})
        self.assertEqual(r.status, 400)

        # Con override → ok.
        r = await self.client.post(
            f"/admin/api/mcp/install/{job_id}/confirm",
            json={"command": "npx", "args": ["-y", "weird-mcp"],
                  "capability": "browser", "name": "weird-mcp"})
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["state"], "healthy")

        r = await self.client.get("/admin/api/mcp")
        rows = (await r.json())["mcp_servers"]
        self.assertTrue(any(m["name"] == "weird-mcp"
                            and m["capability"] == "browser"
                            for m in rows))

    async def test_handshake_failed_no_habilita(self) -> None:
        self._patch(clone_repo, AsyncMock(return_value="feed1234"))
        self._patch(static_scan, AsyncMock(return_value=[]))
        self._patch(vet_with_llm, AsyncMock(return_value=("safe", "")))
        self._patch(detect_run_command, lambda _d: _good_config())
        self._patch(run_handshake, AsyncMock(
            return_value=(False, "handshake timeout (20s)")))

        r = await self.client.post(
            "/admin/api/mcp/install",
            json={"url": "https://github.com/org/broken"})
        job_id = (await r.json())["job_id"]
        await _wait_state(self.installer, job_id,
                          InstallerState.AWAITING_CONFIRM)

        r = await self.client.post(
            f"/admin/api/mcp/install/{job_id}/confirm", json={})
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertEqual(body["state"], "handshake_failed")
        self.assertIn("timeout", body["error"])

        r = await self.client.get("/admin/api/mcp")
        row = next(m for m in (await r.json())["mcp_servers"]
                   if m["name"] == "broken")
        self.assertFalse(row["enabled"])
        self.assertEqual(row["health"], "handshake_failed")

    async def test_clone_falla_queda_en_failed(self) -> None:
        self._patch(clone_repo, AsyncMock(side_effect=RuntimeError(
            "clone falló: sin red")))

        r = await self.client.post(
            "/admin/api/mcp/install",
            json={"url": "https://github.com/org/fake"})
        job_id = (await r.json())["job_id"]
        await _wait_state(self.installer, job_id, InstallerState.FAILED)

        r = await self.client.post(
            f"/admin/api/mcp/install/{job_id}/confirm", json={})
        self.assertEqual(r.status, 409)

    async def test_url_invalida_400(self) -> None:
        r = await self.client.post(
            "/admin/api/mcp/install", json={"url": ""})
        self.assertEqual(r.status, 400)
        r = await self.client.post(
            "/admin/api/mcp/install",
            json={"url": "ftp://algo"})
        self.assertEqual(r.status, 400)

    async def test_job_inexistente_404(self) -> None:
        r = await self.client.get("/admin/api/mcp/install/fantasma")
        self.assertEqual(r.status, 404)
        r = await self.client.post(
            "/admin/api/mcp/install/fantasma/confirm", json={})
        self.assertEqual(r.status, 404)

    async def test_contrato_json_para_ui(self) -> None:
        """El shape que la UI (`tab-mcp.js`) consume en _renderJob().

        Garantiza: si cambiamos campos en InstallJob.to_public(), falla
        acá y no silenciosamente en el browser. Es el test de contrato
        mínimo entre backend y frontend — sin framework JS, solo Python.
        """
        self._patch(clone_repo, AsyncMock(return_value="abc1234"))
        self._patch(static_scan, AsyncMock(
            return_value=["postinstall sospechoso"]))
        self._patch(vet_with_llm, AsyncMock(
            return_value=("suspect", "ojo")))
        self._patch(detect_run_command, lambda _d: {
            "command": "npx", "args": ["-y", "."],
            "env": {}, "needs_manual": False})

        r = await self.client.post(
            "/admin/api/mcp/install",
            json={"url": "https://github.com/org/ui-contract"})
        job_id = (await r.json())["job_id"]
        await _wait_state(self.installer, job_id,
                          InstallerState.AWAITING_CONFIRM)

        r = await self.client.get(
            f"/admin/api/mcp/install/{job_id}")
        self.assertEqual(r.status, 200)
        body = await r.json()

        # Todos los campos que la UI renderiza deben estar presentes
        # (no necesariamente no-vacíos, pero presentes con tipos
        # compatibles).
        required_str = [
            "id", "url", "slug", "state", "vet_verdict",
            "install_dir", "source_commit", "vet_report",
            "updated_at", "created_at",
        ]
        for k in required_str:
            self.assertIn(k, body, f"falta key {k!r}")
            self.assertIsInstance(body[k], str, f"{k} no es string")

        self.assertIsInstance(body["scan_findings"], list)
        self.assertIsInstance(body["proposal"], dict)
        # Campos opcionales.
        for opt in ("error", "override"):
            self.assertTrue(opt in body)





class TestStaticScanHeuristic(unittest.IsolatedAsyncioTestCase):

    async def test_detecta_postinstall_en_package_json(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "package.json").write_text(json.dumps({
                "name": "x",
                "scripts": {"postinstall": "curl evil"},
                "dependencies": {},
            }))
            findings = await static_scan(d)
            self.assertTrue(
                any("postinstall" in f.lower() for f in findings))

    async def test_pyproject_sin_flags(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
            findings = await static_scan(d)
            self.assertEqual(findings, [])

    async def test_detecta_binario_prebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "lib.dll").write_bytes(b"x")
            findings = await static_scan(d)
            self.assertTrue(
                any("binario" in f.lower() for f in findings))


# ---------- _parse_vetting_response (puro) ----------

class TestParseVettingResponse(unittest.TestCase):
    """El parser del JSON que devuelve el LLM. Puro (sin red, sin DB,
    sin asyncio). Acepta JSON pelado, envuelto en ```json fences,
    con texto antes/después, y rejects lo que no matchea."""

    def test_plain_json_safe(self):
        v, r = _parse_vetting_response(
            '{"verdict": "safe", "reasons": ["todo bien"]}')
        self.assertEqual(v, "safe")
        self.assertIn("todo bien", r)

    def test_plain_json_suspect(self):
        v, r = _parse_vetting_response(
            '{"verdict": "suspect", "reasons": ["a", "b"]}')
        self.assertEqual(v, "suspect")
        self.assertEqual(r, "a; b")

    def test_plain_json_rejected(self):
        v, _ = _parse_vetting_response(
            '{"verdict": "rejected", "reasons": ["evil"]}')
        self.assertEqual(v, "rejected")

    def test_json_en_fences(self):
        v, _ = _parse_vetting_response(
            "```json\n"
            '{"verdict": "safe", "reasons": ["x"]}\n'
            "```")
        self.assertEqual(v, "safe")

    def test_json_con_texto_alrededor(self):
        v, _ = _parse_vetting_response(
            "Pensemos... {\"verdict\": \"safe\", \"reasons\": [\"x\"]} "
            "y eso seria todo.")
        self.assertEqual(v, "safe")

    def test_json_con_objeto_anidado(self):
        # JSON simple con un objeto chico dentro de un reason string
        # (no un objeto adentro del objeto, que sería ambiguo).
        v, _ = _parse_vetting_response(
            '{"verdict": "safe", "reasons": ["nested", "ok"]}')
        self.assertEqual(v, "safe")
        # Y el caso limite: el parser agarra el primer { balanceado.
        # Si los braces dentro de strings lo confunden, devolvemos
        # None (degradamos a 'unknown' en el handler). Eso es OK:
        # el LLM puede ser no-estructurado y queremos ser honestos.
        self.assertIsNone(_parse_vetting_response(
            '{"verdict": "safe", "reasons": [{' + '}'))

    def test_invalid_verdict_returns_none(self):
        # verdict que no esta en el whitelist -> None
        self.assertIsNone(_parse_vetting_response(
            '{"verdict": "maybe", "reasons": ["x"]}'))

    def test_missing_reasons_returns_none(self):
        self.assertIsNone(_parse_vetting_response(
            '{"verdict": "safe"}'))

    def test_non_string_reasons_returns_none(self):
        self.assertIsNone(_parse_vetting_response(
            '{"verdict": "safe", "reasons": [1, 2]}'))

    def test_broken_json_returns_none(self):
        self.assertIsNone(_parse_vetting_response(
            '{"verdict": "safe", "reasons": [}'))
        self.assertIsNone(_parse_vetting_response(
            'esto no es json para nada'))
        self.assertIsNone(_parse_vetting_response(''))


# ---------- vet_with_llm: caminos de error ----------

class TestVetWithLlmFallsBack(unittest.IsolatedAsyncioTestCase):
    """vet_with_llm enchufa run_consult (iter 9.4: file I/O se hace en
    proceso via repo_reader, no via Agent tools). Si el modelo falla
    o devuelve algo no parseable, NO rompe el install: degradamos a
    'unknown' + nota honesta. Esto es importante porque el usuario
    quiere poder cargar MCPs aunque no tenga API key."""

    async def test_model_unavailable_returns_unknown(self):
        # Forzar el path 'no model': parcheamos build_model para
        # que tire ModelUnavailable.
        from relay.mcp_installer import vet_with_llm
        with patch("relay.expert_models.build_model",
                   side_effect=ModelUnavailable("sin key")):
            with tempfile.TemporaryDirectory() as t:
                d = Path(t)
                v, report = await vet_with_llm(d, ["finding1"])
                self.assertEqual(v, "unknown")
                self.assertIn("no disponible", report.lower())
                self.assertIn("1 hallazgos", report)

    async def test_empty_findings_still_work(self):
        from relay.mcp_installer import vet_with_llm
        # Sin findings, el prompt se completa igual.
        with patch("relay.experts.run_consult",
                   AsyncMock(return_value={"content": ""})):
            with tempfile.TemporaryDirectory() as t:
                d = Path(t)
                v, report = await vet_with_llm(d, [])
                self.assertEqual(v, "unknown")
                self.assertIn("preview", report.lower())

    async def test_valid_json_is_extracted(self):
        from relay.mcp_installer import vet_with_llm
        ok = ('{"verdict": "safe", "reasons": ["readme claro", '
              '"entry point python -m foo"]}')
        with patch("relay.experts.run_consult",
                   AsyncMock(return_value={"content": ok})):
            with tempfile.TemporaryDirectory() as t:
                d = Path(t)
                v, report = await vet_with_llm(d, [])
                self.assertEqual(v, "safe")
                self.assertIn("readme claro", report)

    async def test_fenced_json_is_extracted(self):
        from relay.mcp_installer import vet_with_llm
        ok = '```json\n{"verdict": "rejected", "reasons": ["evil"]}\n```'
        with patch("relay.experts.run_consult",
                   AsyncMock(return_value={"content": ok})):
            with tempfile.TemporaryDirectory() as t:
                d = Path(t)
                v, report = await vet_with_llm(d, ["postinstall: x"])
                self.assertEqual(v, "rejected")
                self.assertIn("evil", report)


if __name__ == "__main__":
    unittest.main()
