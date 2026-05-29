try {
    $dsList = Invoke-RestMethod 'http://127.0.0.1:8550/api/datasources' -Method Get -TimeoutSec 30
} catch {
    Write-Output "ERROR: Failed to list datasources: $($_.Exception.Message)"
    exit 2
}
$oracle = $dsList | Where-Object { ($_.db_type -as [string]).ToLower() -eq 'oracle' } | Select-Object -First 1
if (-not $oracle) {
    Write-Output "NO_ORACLE_DS"
    exit 1
}
Write-Output "USING_DS: $($oracle.id) - $($oracle.name) - $($oracle.host)"
$sqlPath = 'reports\\generated_insert_AVY_FACT_SIDE_20260519.sql'
if (-not (Test-Path $sqlPath)) {
    Write-Output "MISSING_SQL_FILE"
    exit 1
}
$sql = Get-Content -Raw -Encoding UTF8 $sqlPath
$body = @{ target_datasource_id = $oracle.id; sql = $sql; execute = $true } | ConvertTo-Json -Depth 20
try {
    $resp = Invoke-RestMethod 'http://127.0.0.1:8550/api/tests/control-table/check-insert' -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 600
    $resp | ConvertTo-Json -Depth 10
} catch {
    Write-Output "ERROR_EXECUTE"
    Write-Output $_.Exception.Message
    exit 2
}