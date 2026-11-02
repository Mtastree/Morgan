# Local Qwen2.5-1.5B-Instruct deployment - shared bootstrap for the entry scripts.
# Dot-source-free: each entry script calls Resolve-LocalPython then Invoke-LocalModel.

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONLEGACYWINDOWSSTDIO = '0'

# $PSScriptRoot is this file's own directory (scripts\), so the project root is
# one level up. $MyInvocation would resolve to the dot-sourced file instead.
$script:ProjectRoot = Split-Path -Parent $PSScriptRoot

function Resolve-LocalPython {
    <#
      Pick an interpreter for the control script. The project venv wins so the
      deployment does not depend on whatever "python" happens to be first on
      PATH (on this machine that is an unrelated Hermes agent venv).
      The control script is standard-library only, so no packages are needed.
    #>
    $candidates = @(
        (Join-Path $script:ProjectRoot '.venv\Scripts\python.exe'),
        (Join-Path $script:ProjectRoot 'runtime\python\python.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw 'No Python interpreter found. Recreate .venv (see README).'
}

function Invoke-LocalModel {
    param([Parameter(Mandatory)][string]$Command, [string[]]$Extra = @())
    $python = Resolve-LocalPython
    $script = Join-Path $script:ProjectRoot 'scripts\local_model.py'
    & $python $script $Command @Extra
    if ($LASTEXITCODE -ne 0) {
        throw "local_model.py $Command failed with exit code $LASTEXITCODE. See logs\ in this project."
    }
}

function Resolve-HermesLitePython {
    <#
      hermes-lite has its OWN environment: src\hermes-lite\.venv (Python 3.14).
      It is separate from the deployment venv on purpose -- the slim Hermes core
      needs a newer interpreter than the deployment's control scripts do.
    #>
    $lite = Join-Path $script:ProjectRoot 'src\hermes-lite'
    $candidates = @(
        (Join-Path $lite '.venv\Scripts\python.exe'),
        (Join-Path $lite '.python\python.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    throw 'hermes-lite environment not found. See the project README for how to create it.'
}

function Invoke-HermesLite {
    param([Parameter(Mandatory)][string]$Command, [string[]]$Extra = @())
    $python = Resolve-HermesLitePython
    $lite = Join-Path $script:ProjectRoot 'src\hermes-lite'
    Push-Location $lite
    try {
        & $python -m hermes_lite.cli $Command @Extra
        if ($LASTEXITCODE -ne 0) {
            throw "hermes-lite $Command failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}
