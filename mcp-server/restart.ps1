# Reinicia el relay: stop + start en background.
# Uso: .\restart.ps1
#
# Bug original: start.ps1 es bloqueante (queda en foreground).
# Solución: ejecutar start.ps1 con Start-Process -PassThru y salir
# sin esperar a que termine. El child queda corriendo independiente
# de esta terminal.
#
# Verifica después de 4s que el puerto 8413 está abierto de nuevo.
# Si no abre, exit 1 con diagnóstico claro.

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
Set-Location $repoRoot

# PID que sirve el puerto ANTES de parar. La verificacion de abajo mira
# que el proceso sea OTRO, no solo que el puerto conteste: si stop.ps1
# falla y el relay viejo sigue vivo, el puerto responde igual y esto
# reportaba "OK" sobre un proceso que nunca se reinicio. Medido el
# 2026-09-07 — un relay de 17:51 sobrevivio a tres restart "exitosos"
# hasta que compare los PID a mano.
$pidPrevio = (Get-NetTCPConnection -LocalPort 8413 -State Listen `
    -ErrorAction SilentlyContinue).OwningProcess | Select-Object -First 1

# 1. Stop (usa el script existente, no reinventar).
Write-Host "[restart] llamando a stop.ps1..."
& "$repoRoot\stop.ps1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[restart] ERROR: stop.ps1 falló con exit $LASTEXITCODE" -ForegroundColor Red
    exit 1
}

# 2. Start en background.
# Start-Process con -NoNewWindow deja el child attached a la consola
# del caller (que es esta terminal de run_shell), pero con -PassThru
# devolvemos el Process object y no esperamos a que termine.
Write-Host "[restart] arrancando relay en background..."
$proc = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","$repoRoot\start.ps1" `
    -WorkingDirectory $repoRoot `
    -NoNewWindow `
    -RedirectStandardOutput "$repoRoot\server.out.log" `
    -RedirectStandardError "$repoRoot\server.err.log" `
    -PassThru

Write-Host "[restart] relay child PID = $($proc.Id), esperando 4s a que abra :8413..."

# 3. Verificación: esperar hasta 10s a que el puerto esté abierto.
$portUp = $false
for ($i = 0; $i -lt 20; $i++) {
    $conn = Get-NetTCPConnection -LocalPort 8413 -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $portUp = $true
        break
    }
    Start-Sleep -Milliseconds 500
}

if (-not $portUp) {
    Write-Host "[restart] ERROR: puerto 8413 no abrió tras 10s." -ForegroundColor Red
    Write-Host "[restart] últimas líneas de server.err.log:"
    if (Test-Path "$repoRoot\server.err.log") {
        Get-Content "$repoRoot\server.err.log" -Tail 20
    }
    Write-Host "[restart] últimas líneas de server.out.log:"
    if (Test-Path "$repoRoot\server.out.log") {
        Get-Content "$repoRoot\server.out.log" -Tail 10
    }
    exit 1
}

# `$PID` es una variable automatica de PowerShell (el PID del propio
# shell) y es de solo lectura: asignarla tira WriteError y el script
# muere ACA, en el camino de exito, sin llegar al `exit 0` de abajo.
# O sea que un reinicio correcto se reportaba como fallado (exit 1).
# Medido el 2026-09-06.
$relayPid = (Get-NetTCPConnection -LocalPort 8413 -State Listen).OwningProcess |
    Select-Object -First 1
if ($pidPrevio -and $relayPid -eq $pidPrevio) {
    Write-Host "[restart] ERROR: el puerto responde pero es el MISMO proceso (PID=$relayPid). No se reinicio nada." -ForegroundColor Red
    exit 1
}
$edad = [math]::Round(((Get-Date) - (Get-Process -Id $relayPid).StartTime).TotalSeconds, 1)
Write-Host "[restart] OK: relay PID=$relayPid (antes $pidPrevio), arrancado hace ${edad}s" `
    -ForegroundColor Green
exit 0
