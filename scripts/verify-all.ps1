$ErrorActionPreference = 'Continue'
Write-Output "=== listeners ==="
foreach ($port in 3000, 8001) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        foreach ($c in $conns | Select-Object -Unique OwningProcess) {
            $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
            Write-Output ("  :{0}  pid={1}  {2}" -f $port, $c.OwningProcess, $p.ProcessName)
        }
    } else {
        Write-Output ("  :{0}  (no listener; note: 8001 is inside WSL)" -f $port)
    }
}

Write-Output ""
Write-Output "=== backend (WSL) ==="
$h = & wsl.exe bash -c "curl -s -m 5 http://127.0.0.1:8001/health"
if ($h) {
    $j = $h | ConvertFrom-Json
    Write-Output ("  status={0}  mounted={1}  failed={2}  auth_disabled={3}  startup_ddl={4}" -f `
        $j.status, $j.mounted.Count, $j.failed.Count, $j.auth_disabled, $j.startup_ddl)
} else {
    Write-Output "  backend unreachable"
}

Write-Output ""
Write-Output "=== PG ==="
$q = & wsl.exe bash /mnt/d/git/autoalpha/scripts/wsl/pg-count.sh
Write-Output ("  " + $q)

Write-Output ""
Write-Output "=== frontend + proxy ==="
foreach ($u in @(
    'http://127.0.0.1:3000/',
    'http://127.0.0.1:3000/api/v1/alpha-agent/markets',
    'http://127.0.0.1:3000/api/v1/alpha-agent/tasks/history',
    'http://127.0.0.1:3000/api/v1/alpha-agent/data-summary',
    'http://127.0.0.1:3000/api/v1/factor-report/datasets'
)) {
    $r = & curl.exe -s -o "$env:TEMP\v.out" -w "%{http_code} / %{size_download}B" --max-time 60 $u
    Write-Output ("  {0,-12} {1}" -f $r, ($u -replace 'http://127.0.0.1:3000', ''))
}
