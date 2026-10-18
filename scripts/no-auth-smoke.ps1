# No-login smoke test (ASCII only: PowerShell 5.1 reads .ps1 as ANSI).
$ErrorActionPreference = 'Continue'
$FE = 'http://127.0.0.1:3000'
$BE = 'http://127.0.0.1:8001'
$tmp = $env:TEMP

function Code($url, $out, $timeout = 90) {
    return (& curl.exe -s -o $out -w "%{http_code}" --max-time $timeout $url)
}

function Snip($path, $n = 90) {
    if (-not (Test-Path $path)) { return '' }
    $b = Get-Content $path -Raw
    $s = if ($b.Length -gt $n) { $b.Substring(0, $n) } else { $b }
    return ($s -replace "`r?`n", ' ')
}

Write-Output "== 1) backend auth flag =="
& curl.exe -s --max-time 20 "$BE/health" -o "$tmp\h.json"
$h = Get-Content "$tmp\h.json" -Raw | ConvertFrom-Json
Write-Output ("  auth_disabled = {0}   status = {1}" -f $h.auth_disabled, $h.status)

Write-Output ""
Write-Output "== 2) direct backend, no identity headers (should be 200) =="
foreach ($p in @('/api/v1/alpha-agent/markets', '/api/v1/admin/training-data/catalog')) {
    $c = Code "$BE$p" "$tmp\d.json" 30
    Write-Output ("  {0}  {1,-42} {2}" -f $c, $p, (Snip "$tmp\d.json"))
}

Write-Output ""
Write-Output "== 3) frontend env carries VITE_DISABLE_AUTH =="
$c = Code "$FE/src/features/auth/services/authService.ts" "$tmp\auth.js" 60
Write-Output ("  authService.ts http={0}" -f $c)
$line = Get-Content "$tmp\auth.js" -Raw -ErrorAction SilentlyContinue
if ($line -match 'VITE_DISABLE_AUTH":\s*"([^"]+)"') {
    Write-Output ("  VITE_DISABLE_AUTH = " + $Matches[1])
} else {
    Write-Output "  VITE_DISABLE_AUTH not found in module env snapshot"
}

Write-Output ""
Write-Output "== 4) frontend shell =="
Write-Output ("  / http={0}" -f (Code "$FE/" "$tmp\fe.html" 30))

Write-Output ""
Write-Output "== 5) via frontend proxy =="
foreach ($p in @('/api/v1/alpha-agent/tasks/history', '/api/v1/alpha-agent/data-summary')) {
    $c = Code "$FE$p" "$tmp\p.json" 60
    Write-Output ("  {0}  {1,-42} {2}" -f $c, $p, (Snip "$tmp\p.json"))
}
