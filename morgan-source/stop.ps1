# Stop only the server this project started.
# stop refuses to act unless the PID, its creation time, its executable path and
# the listening socket all still belong to the process recorded at start time.
. (Join-Path $PSScriptRoot 'scripts\common.ps1')
Invoke-LocalModel -Command 'stop'
