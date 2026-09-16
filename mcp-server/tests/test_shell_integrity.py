"""Comandos reales: el intérprete no debe convertir fallos en éxitos."""
import shutil
import asyncio
from contextlib import asynccontextmanager

import pytest

from relay import shell, shell_tools
from relay.experts import Bitacora


@pytest.mark.skipif(not shell.bash_exe(), reason="requiere Bash")
@pytest.mark.parametrize("cmd", ["false | cat", "false; echo no-debe-ejecutarse"])
async def test_bash_preserves_failures(tmp_path, cmd):
    result = await shell.run(cmd, cwd=str(tmp_path), shell_kind="sh", timeout=10)
    assert result["exit"] != 0
    assert "no-debe-ejecutarse" not in result["out"]


@pytest.mark.skipif(not shutil.which("pwsh"), reason="requiere PowerShell 7")
@pytest.mark.parametrize("cmd", [
    "Get-Item ./archivo-inexistente; Write-Output no-debe-ejecutarse",
    "cmd /c exit 7; Write-Output no-debe-ejecutarse",
])
async def test_powershell_preserves_failures(tmp_path, cmd):
    result = await shell.run(cmd, cwd=str(tmp_path), shell_kind="powershell", timeout=10)
    assert result["exit"] != 0
    assert "\nno-debe-ejecutarse\n" not in result["out"]


async def test_background_records_actual_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(shell, "BG_LOG_DIR", tmp_path / "logs")
    ledger = Bitacora()

    @asynccontextmanager
    async def inflight(name):
        yield

    tool = shell_tools.shell_tools(repo=str(tmp_path), techo_s=10,
                                   bitacora=ledger, en_vuelo=inflight)[0]
    output = await tool.function("exit 7", background=True)
    assert "exit=7" in output
    assert ledger.comandos[-1].endswith("exit=7")


async def test_background_same_command_never_overwrites_log(tmp_path, monkeypatch):
    monkeypatch.setattr(shell, "BG_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(shell.time, "strftime", lambda *args: "fixed-second")
    first = await shell.lanzar("exit 0", base=str(tmp_path))
    second = await shell.lanzar("exit 0", base=str(tmp_path))
    assert first["log"] != second["log"]


async def test_cancelled_background_start_is_reaped_before_return(tmp_path, monkeypatch):
    from types import SimpleNamespace
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    process = SimpleNamespace(pid=123, returncode=None)
    process.wait = lambda timeout: process.returncode
    def spawn(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        return process
    def kill(proc):
        assert proc is process
        proc.returncode = -1
    monkeypatch.setattr(shell.subprocess, "Popen", spawn)
    monkeypatch.setattr(shell, "_kill_tree", kill)
    task = asyncio.create_task(shell.lanzar("server", base=str(tmp_path)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -1, "sin PID entregado, el caller no puede limpiar el proceso"
