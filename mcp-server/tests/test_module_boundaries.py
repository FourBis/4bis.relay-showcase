"""Las rutas dependen de estado compartido, no de los módulos que las registran."""
import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_python_modules_stay_readable():
    """500 líneas como guía, hasta 650 para conservar una unidad cohesiva."""
    root = Path(__file__).parents[1] / "src" / "relay"
    oversized = {
        str(path.relative_to(root)): len(path.read_text(encoding="utf-8-sig").splitlines())
        for path in root.rglob("*.py")
        if len(path.read_text(encoding="utf-8-sig").splitlines()) > 650
    }
    assert not oversized, f"Separar por responsabilidad antes de ampliar: {oversized}"


def test_expert_runs_do_not_share_mutable_state():
    from relay.expert_run_state import ExpertRunState

    first, second = ExpertRunState(), ExpertRunState()
    first.progress_events.append({"phase": "thinking"})
    first._inflight["shell"] = 1.0
    first.tool_meter["shell"] = {"calls": 1}
    first.recent_tool_calls.append("shell")
    assert second.progress_events == []
    assert second._inflight == second.tool_meter == {}
    assert not second.recent_tool_calls


@pytest.mark.parametrize("name", [
    "app_state", "admin_common", "admin_crm", "admin_config", "admin_mcp",
    "voice_routes", "bitacora", "progress", "reporting",
])
def test_domains_do_not_import_composition_roots(name):
    source = Path(__file__).parents[1] / "src" / "relay" / f"{name}.py"
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.module in ("relay", None):
                assert not {a.name for a in node.names} & {"admin", "server"}
            assert node.module not in {"relay.admin", "relay.server", "admin", "server"}
        elif isinstance(node, ast.Import):
            assert not {a.name for a in node.names} & {"relay.admin", "relay.server"}


def test_evidence_and_progress_do_not_load_the_expert_runtime(tmp_path):
    env = {**os.environ, "FOURBIS_DB_PATH": str(tmp_path / "relay.db"),
           "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import relay.bitacora, relay.progress; "
         "assert 'relay.experts' not in sys.modules; "
         "assert 'pydantic_ai' not in sys.modules"],
        env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
