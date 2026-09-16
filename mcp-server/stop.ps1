# Detiene el relay — kill agressivo de TODO relay.server que matchee.
# Bug fix 2026-07-08: este script antes usaba `Get-Process python | Where ...`
# que SOLO ve procesos del usuario actual. Si un zombie de un start
# previo (o un python de sistema) está sirviendo :8413, este script
# no lo encontraba y quedaba vivo. Ahora usamos CIM/WMI (ve todos los
# procesos del sistema), kill por PID, y verificamos puerto libre al final.

$ErrorActionPreference = "Stop"

Write-Host "[stop] buscando procesos relay.server (cualquier user)..."

$killTargets = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*relay.server*' } |
    Select-Object ProcessId, Name, CommandLine, CreationDate

if (-not $killTargets) {
    Write-Host "[stop] no hay relay corriendo."
} else {
    foreach ($p in $killTargets) {
        Write-Host "[stop] matando PID=$($p.ProcessId)  cmd=$($p.CommandLine)"
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        } catch {
            Write-Host "[stop] WARN: no pude matar PID $($p.ProcessId): $_"
        }
    }
}

# Verificación adicional: puerto 8413 debe quedar libre.
# Esperar hasta 5s a que el socket libere.
Write-Host "[stop] verificando puerto 8413 libre..."
$portFree = $false
for ($i = 0; $i -lt 10; $i++) {
    $conn = Get-NetTCPConnection -LocalPort 8413 -State Listen -ErrorAction SilentlyContinue
    if (-not $conn) {
        $portFree = $true
        break
    }
    Start-Sleep -Milliseconds 500
}

$remaining = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*relay.server*' }
if ($remaining) {
    Write-Host "[stop] ERROR: todavia hay procesos relay.server vivos:"
    $remaining | Format-Table ProcessId, CommandLine
    exit 1
}
if (-not $portFree) {
    Write-Host "[stop] ERROR: puerto 8413 sigue ocupado tras 5s."
    Get-NetTCPConnection -LocalPort 8413 -ErrorAction SilentlyContinue |
        Format-Table LocalAddress, LocalPort, OwningProcess, State
    exit 1
}

Write-Host "[stop] OK: relay detenido y puerto 8413 libre."

exit 0
