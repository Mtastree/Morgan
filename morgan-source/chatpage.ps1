# Start the local CHAT page (the product entry for round 16).
#
#   .\chatpage.cmd                 -> http://127.0.0.1:8771/
#   .\chatpage.cmd --port 8800
#   .\chatpage.cmd --open-browser
#
# This is a SEPARATE surface from .\page.cmd (the archived Agent experiment
# page): different port, different code, different data. Chat loads no tool, no
# Skill and no Agent environment guidance, and it does not need the Hermes core
# to be present at all.
#
# It talks to the same local model server (.\start.cmd -> 127.0.0.1:18085).
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# chatpage.ps1 lives IN the project root, so $PSScriptRoot IS the project root.
# (page.ps1 can use a parent lookup because it delegates to scripts\common.ps1,
# whose own $PSScriptRoot is the scripts directory.)
$projectRoot = $PSScriptRoot
$lite = Join-Path $projectRoot 'src\hermes-lite'

$python = $null
foreach ($c in @((Join-Path $lite '.venv\Scripts\python.exe'),
                 (Join-Path $lite '.python\python.exe'))) {
    if (Test-Path $c) { $python = $c; break }
}
if (-not $python) {
    throw 'hermes-lite environment not found. See the project README.'
}

Push-Location $lite
try {
    & $python -m hermes_lite.chatweb @args
    if ($LASTEXITCODE -ne 0) {
        throw "chat page exited with code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
