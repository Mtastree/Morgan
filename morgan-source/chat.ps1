# Interactive Chinese chat against the local server (streaming).
# /exit quits, /clear resets the conversation context.
. (Join-Path $PSScriptRoot 'scripts\common.ps1')
Invoke-LocalModel -Command 'chat'
