# Smoke test for the slim factor-mining frontend (ASCII only:
# PowerShell 5.1 reads .ps1 as ANSI, non-ASCII breaks parsing).
$ErrorActionPreference = 'Continue'
$FE = 'http://127.0.0.1:3000'
$tmp = $env:TEMP

function Probe($url, $out, $timeout = 60) {
    $c = & curl.exe -s -o $out -w "%{http_code}" --max-time $timeout $url
    $s = if (Test-Path $out) { (Get-Item $out).Length } else { 0 }
    return "$c / $s bytes"
}

Write-Output "== slim frontend modules =="
Write-Output ("  {0,-62} {1}" -f "/", (Probe "$FE/" "$tmp\s1.html" 30))
foreach ($p in @(
    '/src/main.tsx',
    '/src/App.tsx',
    '/src/workspace/HomePage.tsx',
    '/src/workspace/MiningPage.tsx',
    '/src/workspace/MiningForm.tsx',
    '/src/workspace/TasksPage.tsx',
    '/src/workspace/FactorsPage.tsx',
    '/src/workspace/PoolPage.tsx',
    '/src/workspace/CombosPage.tsx',
    '/src/workspace/BacktestPage.tsx',
    '/src/workspace/ResearchPage.tsx',
    '/src/workspace/DataPage.tsx',
    '/src/workspace/SettingsPage.tsx',
    '/src/workspace/WorkspaceContext.tsx',
    '/src/workspace/api.ts',
    '/src/workspace/workspace.css'
)) {
    Write-Output ("  {0,-62} {1}" -f $p, (Probe "$FE$p" "$tmp\s2.js" 90))
}

Write-Output ""
Write-Output "== health + /api proxy (backend auth disabled) =="
foreach ($p in @(
    '/health',
    '/api/v1/alpha-agent/tasks/history',
    '/api/v1/alpha-agent/data-summary',
    '/api/v1/alpha-agent/factors?limit=1',
    '/api/v1/factor-research/snapshot-status'
)) {
    $r = Probe "$FE$p" "$tmp\s3.json" 60
    $body = if (Test-Path "$tmp\s3.json") { Get-Content "$tmp\s3.json" -Raw } else { '' }
    $snip = if ($body.Length -gt 80) { $body.Substring(0, 80) } else { $body }
    Write-Output ("  {0}  {1,-38} {2}" -f $r, $p, ($snip -replace "`r?`n", ' '))
}

Write-Output ""
Write-Output "== size =="
$w = (Get-ChildItem D:\git\autoalpha\web\src -Recurse -File | Measure-Object).Count
$mb = [math]::Round(((Get-ChildItem D:\git\autoalpha\web -Recurse -File -ErrorAction SilentlyContinue |
    Measure-Object -Property Length -Sum).Sum / 1MB), 0)
Write-Output "  web/src : $w files"
Write-Output "  web/    : $mb MB (incl. node_modules)"
