param([string]$Python = '', [switch]$WithSearch)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$root = $PSScriptRoot
$venv = Join-Path $root 'src\hermes-lite\.venv'
$target = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $target)) {
    if ($Python) { & $Python -m venv $venv }
    else { & py -3.14 -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment.' }
}
& $target -m pip install -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Desktop dependency installation failed.' }
if ($WithSearch) {
    & $target -m pip install -r (Join-Path $root 'src\hermes-lite\requirements.search.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Search dependency installation failed.' }
}
& $target -B (Join-Path $root 'scripts\init_local_config.py')
if ($LASTEXITCODE -ne 0) { throw 'Configuration initialization failed.' }
Write-Host 'Environment/configuration ready. Prepare model and llama.cpp files before desktop.cmd.'
