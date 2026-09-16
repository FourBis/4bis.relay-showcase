# Arranca el relay desde el venv del proyecto.
# Uso: .\start.ps1 — esta terminal queda ocupada con el relay.
# Bug fix 2026-07-08: ahora valida:
#   1. Que se va a usar el Python del venv (no el del sistema)
#   2. Que el puerto :8413 NO está ocupado antes de arrancar
#      (si lo está, error claro en vez de fallar silencioso)
#   3. Aviso explícito de qué ejecutable y cwd se va a usar

$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
Set-Location $repoRoot

# 1. Validar venv antes de hacer nada.
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[start] ERROR: no existe $venvPython" -ForegroundColor Red
    Write-Host "[start]   creá el venv primero: python -m venv .venv"
    exit 1
}

# 2. Verificar que el puerto está libre ANTES de arrancar.
$busy = Get-NetTCPConnection -LocalPort 8413 -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $busyPids = ($busy | Select-Object -ExpandProperty OwningProcess) -join ", "
    Write-Host "[start] ERROR: puerto 8413 ya está ocupado por PID(s): $busyPids" -ForegroundColor Red
    Write-Host "[start]   corré .\stop.ps1 primero, o matá el PID a mano:"
    Get-CimInstance Win32_Process -Filter "ProcessId IN ($busyPids)" |
        Select-Object ProcessId, Name, CommandLine |
        Format-Table
    exit 1
}

# 3. Setup env y arrancar.
& ".\.venv\Scripts\Activate.ps1"
$env:PYTHONPATH = "src"
$env:MCP_PORT = "8413"
$env:BOT_NOTIFY_URL = "http://localhost:8297/notify"
$env:GOOGLE_REAL = "0"
$env:STATE_DIR = Join-Path $repoRoot "state\agents"
New-Item -ItemType Directory -Force -Path $env:STATE_DIR | Out-Null

Write-Host "[start] ejecutable: $venvPython"
Write-Host "[start] cwd: $repoRoot"
Write-Host "[start] arrancando relay (esta terminal queda ocupada)..."

# & es "ejecutar como child", no como JOB. Se bloquea hasta que
# el proceso muera (Ctrl+C en esta terminal lo detiene).
& $venvPython -u -m relay.server
