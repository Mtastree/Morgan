# Launch the local chat desktop application (pywebview + WebView2).
#
#   .\desktop.cmd
#   .\desktop.cmd --port 8771
#   .\desktop.ps1
#
# Launches the standalone desktop window without requiring separate browser or manual commands.
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$projectRoot = $PSScriptRoot
$lite = Join-Path $projectRoot 'src\hermes-lite'

$python = $null
$pythonw = $null

$pythonCandidates = @(
    (Join-Path $lite '.venv\Scripts\python.exe'),
    (Join-Path $lite '.python\python.exe')
)
foreach ($c in $pythonCandidates) {
    if (Test-Path $c) { $python = $c; break }
}

$pythonwCandidates = @(
    (Join-Path $lite '.venv\Scripts\pythonw.exe'),
    (Join-Path $lite '.python\pythonw.exe')
)
foreach ($c in $pythonwCandidates) {
    if (Test-Path $c) { $pythonw = $c; break }
}

if (-not $python) {
    throw 'hermes-lite environment not found. See project README.'
}

# Use pythonw if launching without explicit console request
$targetExe = if ($pythonw -and (-not ($args -contains '--console'))) { $pythonw } else { $python }

Push-Location $lite
try {
    & $targetExe -m hermes_lite.chatdesktop @args
    if ($LASTEXITCODE -ne 0) {
        throw "chatdesktop exited with code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
