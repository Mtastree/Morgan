# Start the local Qwen2.5-1.5B-Instruct Q4_K_M server (background, hidden window).
# Checks model integrity, launches llama-server, then waits for /health and
# prints the runtime's own offload report.
. (Join-Path $PSScriptRoot 'scripts\common.ps1')
Invoke-LocalModel -Command 'start'
