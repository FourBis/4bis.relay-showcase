"""Redacción de credenciales antes de persistir un tool step (2026-09-02).

Por qué existe: los expertos pasan tokens y passwords por línea de comandos
(`curl -H "Authorization: token …"`, `psql "postgres://user:pass@host"`), y el
relay guardaba el comando ENTERO en `chats.progress_events`. Un barrido sobre
la base y los 1240 `.md` de chats encontró 1 token de GitHub (94 copias), 22
headers `Authorization`, 22 `password=`, 8 connection strings, 7 passwords en
URL, 3 `api_key` y 1 AWS key, en siete proyectos de clientes.

Cómo correr:
    cd mcp-server
    python -m pytest tests/test_redaccion_secretos.py -q
"""
from __future__ import annotations

from relay.experts import _redactar, _format_tool_step

TOKEN_GH = "gho_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _tapado(s: str) -> bool:
    return "[REDACTED" in s


# ---------- cada patrón tapa, y conserva la etiqueta ----------

def test_header_authorization():
    out = _redactar(f'curl -H "Authorization: token {TOKEN_GH}" https://api.github.com')
    assert TOKEN_GH not in out
    # La etiqueta sobrevive: quien lee el log sabe QUÉ había.
    assert "Authorization: token [REDACTED]" in out
    assert "https://api.github.com" in out


def test_token_con_prefijo_reconocible():
    out = _redactar(f"echo {TOKEN_GH}")
    assert TOKEN_GH not in out
    assert "gho_[REDACTED]" in out


def test_aws_access_key():
    # Clave de ejemplo de la doc de AWS: AKIA + 16 chars, el largo real.
    out = _redactar("aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "[REDACTED-AWS-KEY]" in out


def test_aws_key_corta_no_matchea():
    """Un AKIA de largo equivocado NO es una key: no se redacta de más."""
    txt = "AKIACORTA123"
    assert _redactar(txt) == txt


def test_password_en_url():
    out = _redactar('psql "postgres://admin:Sup3rS3cret@db.internal:5432/app"')
    assert "Sup3rS3cret" not in out
    # user, host, puerto y base siguen visibles: sin eso el log no sirve.
    assert "admin:[REDACTED]@db.internal:5432/app" in out


def test_connection_string_y_password_suelto():
    out = _redactar("sqlcmd -Q \"...\" ; Password=j4m0nSerrano; User=sa")
    assert "j4m0nSerrano" not in out
    assert "Password=[REDACTED]" in out
    assert "User=sa" in out          # lo que no es secreto no se toca


def test_api_key_y_client_secret():
    out = _redactar("run --api_key=abcd1234efgh5678 --client_secret: zzzz9999xxxx")  # gitleaks:allow -- synthetic redaction fixture
    assert "abcd1234efgh5678" not in out
    assert "zzzz9999xxxx" not in out


def test_clave_privada_pem_tapa_el_cuerpo():
    pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIEowIBAAKCAQEAxGgH0j2k3l4m5n6o7p8q9r0s\n"
           "-----END RSA PRIVATE KEY-----")  # gitleaks:allow -- synthetic redaction fixture
    out = _redactar(pem)
    assert "MIIEowIBAAKCAQEA" not in out
    assert "BEGIN RSA PRIVATE KEY" in out   # se ve que HABÍA una clave


# ---------- lo que NO se debe tocar ----------

def test_comandos_normales_intactos():
    for cmd in (
        "git status --short",
        "dotnet build 2>&1 | tail -30",
        "docker compose up -d --build",
        'gh api graphql -F query=\'{ viewer { login } }\'',
        "cd /c/Users/demo && ls -la",
        "python -m pytest tests/ -q",
    ):
        assert _redactar(cmd) == cmd, cmd


def test_no_confunde_palabras_parecidas():
    # "password" como texto, sin `=` ni `:`, no es una credencial.
    txt = "el usuario olvido su password y pidio reset"
    assert _redactar(txt) == txt


def test_es_idempotente():
    once = _redactar(f'curl -H "Authorization: token {TOKEN_GH}"')
    assert _redactar(once) == once


def test_vacio_y_none():
    assert _redactar("") == ""
    assert _redactar(None) is None


# ---------- el punto de estrangulamiento real ----------

def test_format_tool_step_redacta_las_tres_cadenas():
    """Es el productor único de lo que se persiste en progress_events."""
    msg, diff, cmd = _format_tool_step(
        "shell",
        {"cmd": f'curl -H "Authorization: token {TOKEN_GH}" https://api.github.com',
         "cwd": "C:/repos/x"},
    )
    assert TOKEN_GH not in (msg or "")
    assert TOKEN_GH not in (cmd or "")
    assert TOKEN_GH not in (diff or "")
    # Y sigue siendo un mensaje útil.
    assert "shell" in msg


def test_format_tool_step_no_rompe_el_caso_comun():
    msg, diff, cmd = _format_tool_step("shell", {"cmd": "git status", "cwd": ""})
    assert "git status" in msg
    assert cmd is None or "git status" in cmd


def test_password_entrecomillado():
    """3 de 17 connection strings del corpus real usaban `PASSWORD='x'`.

    La clase de caracteres del valor excluye comillas, así que sin mover
    la comilla de apertura al grupo conservado el valor no arrancaba y el
    secreto se filtraba entero.
    """
    for txt, secreto in (
        ("PASSWORD='supersecreto123'", "supersecreto123"),
        ('password="otroSecreto99"', "otroSecreto99"),
        ("Password='x1y2z3'; User=sa", "x1y2z3"),
    ):
        out = _redactar(txt)
        assert secreto not in out, txt
        assert "[REDACTED]" in out
