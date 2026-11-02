# Report server state: managed process identity, /health, /v1/models, GPU usage.
. (Join-Path $PSScriptRoot 'scripts\common.ps1')
Invoke-LocalModel -Command 'status'
