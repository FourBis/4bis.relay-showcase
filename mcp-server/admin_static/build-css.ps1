# Regenera static/admin.css (bundle Tailwind + reglas propias).
# Fuente de verdad: static/admin.src.css — NO editar admin.css a mano.
#
# Uso:  pwsh mcp-server/admin_static/build-css.ps1
#
# Baja el Tailwind standalone CLI v3.4.17 a %TEMP%\twcli si no está
# (una sola vez; no requiere node/npm).
$ErrorActionPreference = "Stop"
$cli = Join-Path $env:TEMP "twcli\tailwindcss.exe"
if (-not (Test-Path $cli)) {
  New-Item -ItemType Directory -Force (Split-Path $cli) | Out-Null
  Write-Host "Bajando tailwindcss standalone v3.4.17..."
  Invoke-WebRequest -Uri "https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-windows-x64.exe" -OutFile $cli -UseBasicParsing
}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $here
# Push-Location mueve la ubicación de PowerShell, pero NO el directorio
# del PROCESO, que es el que miran las APIs de .NET. Sin esta línea,
# `[System.IO.File]::ReadAllBytes("static/admin.src.css")` de más abajo
# resuelve contra el cwd de quien invocó el script y revienta con
# "Could not find a part of the path ...\4bis.relay\static\admin.src.css"
# — o sea, corriendo el script como dice su propio encabezado
# (`pwsh mcp-server/admin_static/build-css.ps1`, desde la raíz del repo).
# El CLI de Tailwind sí escribía bien, así que el bundle se regeneraba y
# el script moría JUSTO ANTES de sellarlo: admin.css quedaba sin sello y
# el test acusaba un build viejo que en realidad estaba recién hecho.
[System.Environment]::CurrentDirectory = $here
try {
  & $cli -c tailwind.admin.config.js -i static/admin.src.css -o static/admin.css --minify
  if ($LASTEXITCODE -ne 0) { throw "Tailwind falló (exit=$LASTEXITCODE); no se sella el CSS." }

  # Sella el bundle con el sha256 de la fuente. admin.css es output
  # commiteado: sin esto, editar admin.src.css y olvidar el build deja
  # el admin sirviendo CSS viejo en silencio. Lo verifica
  # tests/test_admin_css_build.py.
  #
  # 2026-08-16: el hash va sobre la fuente con los saltos NORMALIZADOS a
  # LF. Get-FileHash hashea los bytes del disco, que en Windows son CRLF,
  # mientras que .gitattributes guarda LF en el repo: el sello nunca
  # coincidia fuera de esta maquina y el test fallaba en falso, acusando
  # a un bundle viejo que estaba al dia.
  $bytes = [System.IO.File]::ReadAllBytes("static/admin.src.css")
  $texto = [System.Text.Encoding]::UTF8.GetString($bytes).Replace("`r`n", "`n")
  $sha = [System.Security.Cryptography.SHA256]::Create()
  $src = -join ($sha.ComputeHash(
    [System.Text.Encoding]::UTF8.GetBytes($texto)) |
    ForEach-Object { $_.ToString("x2") })
  Add-Content -Path static/admin.css -Value "/*!src-sha256:$src*/" -NoNewline
  Write-Host "admin.css sellado con src-sha256:$src"

  # Segundo sello: el conjunto de CLASES que usan el HTML y los .js.
  # El de arriba detecta "editaste admin.src.css y no rebuildeaste"; este
  # detecta el otro olvido, que paso de verdad el 2026-08-16: agregaste
  # una clase de Tailwind en el HTML/JS y no rebuildeaste, asi que el
  # bundle no la tiene y la clase no hace nada, en silencio.
  #
  # El calculo vive en sello_clases.py y no aca a proposito: escribir el
  # mismo hash dos veces (PowerShell + Python) es literalmente como nacio
  # el bug de CRLF de este mismo archivo.
  # El python del venv como en run_full.ps1; `python` a secas si no esta.
  $py = Join-Path $here "..\.venv\Scripts\python.exe"
  if (-not (Test-Path $py)) { $py = "python" }
  & $py sello_clases.py --write
  if ($LASTEXITCODE -ne 0) { throw "Falló el sello de clases (exit=$LASTEXITCODE)." }
} finally {
  Pop-Location
}
