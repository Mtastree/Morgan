#requires -Version 5.1
<#
  Resume + verify the single pinned Q4_K_M artifact for this project.

  Provenance is re-read from the Hub API for the pinned revision on every run.
  Nothing is renamed to its final name until size, SHA-256 and the GGUF header
  all match the pinned expectations. Failures keep the .partial file and write
  an evidence JSON instead of proceeding.
#>
[CmdletBinding()]
param(
    [string]$Root,
    [switch]$SkipVerify
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if (-not $Root) { $Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }
if (-not (Test-Path (Join-Path $Root 'configs\model_source.json'))) {
    throw "Root resolution failed: $Root does not contain configs\model_source.json"
}

$source  = Get-Content (Join-Path $Root 'configs\model_source.json') -Raw | ConvertFrom-Json
$repo    = $source.repo
$rev     = $source.revision
$name    = $source.file.rfilename
$expSize = [int64]$source.file.size
$expSha  = $source.file.lfs.sha256.ToLower()

$modelDir = Join-Path $Root 'models'
$partial  = Join-Path $modelDir "$name.partial"
$final    = Join-Path $modelDir $name
$logDir   = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $modelDir, $logDir | Out-Null

$evidence = [ordered]@{
    pinned = [ordered]@{ repo = $repo; revision = $rev; file = $name; size = $expSize; sha256 = $expSha }
    observed_at = (Get-Date).ToString('o')
}

# ---- 1. Re-read provenance from the Hub for the pinned revision ----------
$api = "https://huggingface.co/api/models/$repo/revision/$rev`?blobs=true"
$meta = Invoke-RestMethod -Uri $api -TimeoutSec 60 -MaximumRedirection 5
$entry = $meta.siblings | Where-Object { $_.rfilename -eq $name }
if (-not $entry) { throw "Pinned file $name is absent from revision $rev. Source metadata changed." }

$observed = [ordered]@{
    repo_sha   = $meta.sha
    blobId     = $entry.blobId
    size       = [int64]$entry.size
    lfs_size   = [int64]$entry.lfs.size
    sha256     = $entry.lfs.sha256.ToLower()
}
$evidence.observed = $observed

$drift = @()
if ($meta.sha -ne $rev) { $drift += "revision: observed $($meta.sha) != pinned $rev" }
if ($observed.size -ne $expSize) { $drift += "size: observed $($observed.size) != pinned $expSize" }
if ($observed.sha256 -ne $expSha) { $drift += "sha256: observed $($observed.sha256) != pinned $expSha" }
if ($drift.Count -gt 0) {
    $evidence.drift = $drift
    $evidence.status = 'SOURCE_DRIFT'
    $evidence | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $logDir 'model_fetch.json') -Encoding utf8
    throw ("Source drift detected; refusing to download.`n  " + ($drift -join "`n  "))
}
$evidence.drift = @()
Write-Host "provenance ok: $repo@$rev  $name  $expSize bytes  $expSha"

# ---- 2. Resume the partial file -----------------------------------------
$resumeFrom = 0
if (Test-Path $partial) {
    $resumeFrom = (Get-Item $partial).Length
    if ($resumeFrom -gt $expSize) {
        throw "Partial file is larger than the pinned size ($resumeFrom > $expSize); refusing to resume."
    }
    Write-Host "resuming at byte $resumeFrom"
} elseif (Test-Path $final) {
    Write-Host 'final file already present; skipping transfer'
    $resumeFrom = $expSize
} else {
    Write-Host 'starting fresh download'
}

if ($resumeFrom -lt $expSize) {
    # Sanity-check the bytes we already hold before appending to them.
    if ($resumeFrom -gt 0) {
        $head = [System.IO.File]::ReadAllBytes($partial)[0..3]
        if (-not ($head[0] -eq 0x47 -and $head[1] -eq 0x47 -and $head[2] -eq 0x55 -and $head[3] -eq 0x46)) {
            throw 'Existing partial file does not start with GGUF magic; refusing to append.'
        }
        Write-Host 'existing partial has valid GGUF magic'
    }

    $url = "https://huggingface.co/$repo/resolve/$rev/$name"
    $curlArgs = @('--fail', '--location', '--silent', '--show-error',
                  '--retry', '8', '--retry-delay', '5', '--retry-all-errors',
                  '--connect-timeout', '30', '--speed-time', '60', '--speed-limit', '10240',
                  '--output', $partial)
    if ($resumeFrom -gt 0) { $curlArgs += @('--continue-at', [string]$resumeFrom) }
    $curlArgs += $url

    & curl.exe @curlArgs
    if ($LASTEXITCODE -ne 0) {
        $evidence.status = 'TRANSFER_FAILED'
        $evidence.curl_exit = $LASTEXITCODE
        $evidence.bytes_on_disk = if (Test-Path $partial) { (Get-Item $partial).Length } else { 0 }
        $evidence | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $logDir 'model_fetch.json') -Encoding utf8
        throw "curl failed with exit code $LASTEXITCODE; partial file kept at $partial"
    }
}

# ---- 3. Verify size, SHA-256 and GGUF header ----------------------------
if (-not $SkipVerify) {
    $bytes = (Get-Item $partial).Length
    if ($bytes -ne $expSize) {
        $evidence.status = 'SIZE_MISMATCH'
        $evidence.bytes_on_disk = $bytes
        $evidence | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $logDir 'model_fetch.json') -Encoding utf8
        throw "size mismatch: got $bytes, expected $expSize (partial file kept)"
    }

    $sha = (Get-FileHash -Path $partial -Algorithm SHA256).Hash.ToLower()
    if ($sha -ne $expSha) {
        $evidence.status = 'SHA256_MISMATCH'
        $evidence.bytes_on_disk = $bytes
        $evidence.observed_sha256 = $sha
        $evidence | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $logDir 'model_fetch.json') -Encoding utf8
        throw "sha256 mismatch: got $sha, expected $expSha (partial file kept, NOT renamed)"
    }

    $stream = [System.IO.File]::OpenRead($partial)
    try {
        $buf = New-Object byte[] 16
        [void]$stream.Read($buf, 0, 16)
    } finally { $stream.Dispose() }
    $magic  = -join ($buf[0..3] | ForEach-Object { [char]$_ })
    $ver    = [BitConverter]::ToUInt32($buf, 4)
    $nTens  = [BitConverter]::ToUInt64($buf, 8)
    if ($magic -ne 'GGUF') { throw "GGUF magic mismatch: got '$magic'" }
    if ($ver -ne 3) { throw "Unexpected GGUF version: $ver" }

    $evidence.verification = [ordered]@{
        bytes        = $bytes
        sha256       = $sha
        gguf_magic   = $magic
        gguf_version = $ver
        tensor_count = $nTens
        pass         = $true
    }
    Write-Host "verify ok: $bytes bytes, sha256 $sha, GGUF v$ver, $nTens tensors"
}

# ---- 4. Promote to the final name only after everything passed ---------
if ((Test-Path $partial) -and -not (Test-Path $final)) {
    Move-Item -Path $partial -Destination $final
    Write-Host "promoted to $final"
}

$evidence.status = 'OK'
$evidence.final_path = $final
$evidence.final_bytes = (Get-Item $final).Length
$evidence | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $logDir 'model_fetch.json') -Encoding utf8
Write-Host 'done'
