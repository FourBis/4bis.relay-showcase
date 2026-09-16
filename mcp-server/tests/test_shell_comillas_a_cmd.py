"""Regresión: `cmd.exe` no parsea argumentos como el CRT (2026-09-07).

`build_argv` devuelve una LISTA y `run()` la pasa a
`asyncio.create_subprocess_exec(*argv)`. En Windows eso pasa por
`subprocess.list2cmdline`, que escapa cada `"` interna como `\\"` —la
convención del CRT de Microsoft, que es la que usan `bash.exe`,
`pwsh.exe` y cualquier programa normal.

`cmd.exe` es el ÚNICO de los tres intérpretes que NO usa esa convención:
para él la barra invertida es un carácter literal. Así que todo comando
ruteado a `cmd` llega con una `\\` de más pegada a cada comilla:

    cd /d "C:\\repo" && python -c "print(2+2)"
    → cmd ve:  cd /d \\"C:\\repo\\" && python -c \\"print(2+2)\\"

y falla de dos formas, ninguna de las cuales nombra la causa:

  - con espacios dentro de las comillas, el argumento se corta en el
    primer espacio (`"import` → SyntaxError);
  - sin espacios, el argumento llega ENTERO pero con las comillas
    adentro como texto, y el proceso sale con exit=0 y salida vacía —
    el modo de falla silenciosa que este módulo evita a propósito.

El segundo test fija lo otro que rompió el mismo incidente: el modelo
encadena con `;` (válido en PowerShell y en sh, NO en cmd, donde el
resto de la línea se vuelve argumento del primer comando).

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_shell_comillas_a_cmd.py -q
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from relay import shell  # noqa: E402

WINDOWS = sys.platform == "win32"
pytestmark = pytest.mark.skipif(not WINDOWS, reason="el bug es de cmd.exe")


def _corre(cmd: str, **kw) -> dict:
    return asyncio.run(shell.run(cmd, timeout=60, **kw))


def test_las_comillas_sobreviven_al_ruteo_a_cmd(tmp_path):
    """Un comando ruteado a `cmd` no puede perder sus comillas."""
    cmd = f'cd /d "{tmp_path}" && {sys.executable} -c "print(2+2)"'
    argv, kind = shell.build_argv(cmd)
    assert kind == "cmd", "precondición: este comando rutea a cmd"
    res = _corre(cmd)
    assert res["exit"] == 0, res["out"]
    assert "4" in res["out"], res["out"]


def test_payload_sin_espacios_no_falla_en_silencio(tmp_path):
    """El caso peor: exit=0 y salida vacía, que el modelo lee como éxito."""
    cmd = f'cd /d "{tmp_path}" && {sys.executable} -c "print(7*6)"'
    assert shell.build_argv(cmd)[1] == "cmd"
    res = _corre(cmd)
    assert "42" in res["out"], f"exit={res['exit']} out={res['out']!r}"


def test_el_punto_y_coma_no_se_rutea_a_cmd():
    """`;` encadena en PowerShell y en sh; en cmd NO existe.

    `cd C:\\repo; git status` ruteado a cmd deja el `; git status` como
    parte del argumento de `cd`, y el error que vuelve —"El sistema no
    puede encontrar la ruta especificada"— habla de un directorio que sí
    existe.
    """
    cmd = r"cd C:\Users\demo\source\repos\AuroraDemo; git status"
    _, kind = shell.build_argv(cmd)
    assert kind != "cmd", f"un `;` no puede ir a cmd.exe (fue a {kind})"
