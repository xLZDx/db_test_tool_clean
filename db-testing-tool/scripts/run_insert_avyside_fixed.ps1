$sqlPath = 'reports\\generated_insert_AVY_FACT_SIDE_20260519.sql'
if (-not (Test-Path $sqlPath)) { Write-Output "MISSING_SQL_FILE"; exit 1 }
$sql = Get-Content -Raw -Encoding UTF8 $sqlPath
$body = @{ target_datasource_id = 3; sql = $sql; execute = $true }
$json = $body | ConvertTo-Json -Depth 20
try {
    $resp = Invoke-RestMethod -Uri 'http://127.0.0.1:8550/api/tests/control-table/check-insert' -Method Post -Body $json -ContentType 'application/json' -TimeoutSec 600
    $resp | ConvertTo-Json -Depth 10
} catch {
    Write-Output "ERROR_EXECUTE"
    Write-Output $_.Exception.Message
    exit 2
}