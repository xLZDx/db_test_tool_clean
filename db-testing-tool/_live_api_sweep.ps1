$ErrorActionPreference = 'Stop'
$base = 'http://127.0.0.1:8550'

function Call-Api {
  param(
    [string]$Method,
    [string]$Path,
    [object]$Body = $null,
    [int[]]$Accept = @(200)
  )
  $uri = "$base$Path"
  try {
    if ($null -ne $Body) {
      $json = $Body | ConvertTo-Json -Depth 10
      $resp = Invoke-WebRequest -Uri $uri -Method $Method -Body $json -ContentType 'application/json' -UseBasicParsing
    } else {
      $resp = Invoke-WebRequest -Uri $uri -Method $Method -UseBasicParsing
    }
    $ok = $Accept -contains [int]$resp.StatusCode
    [pscustomobject]@{ path=$Path; method=$Method; status=[int]$resp.StatusCode; ok=$ok; body=$resp.Content }
  } catch {
    $status = 0
    $body = $_.Exception.Message
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
      $status = [int]$_.Exception.Response.StatusCode
      try {
        $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
        $body = $sr.ReadToEnd()
      } catch {}
    }
    $ok = $Accept -contains $status
    [pscustomobject]@{ path=$Path; method=$Method; status=$status; ok=$ok; body=$body }
  }
}

$results = @()

# 1) Health/openapi + all GET endpoints without path params from openapi.json
$results += Call-Api -Method GET -Path '/'
$results += Call-Api -Method GET -Path '/openapi.json'
$open = (Invoke-RestMethod -Uri "$base/openapi.json" -Method GET)
$paths = $open.paths.PSObject.Properties
foreach ($p in $paths) {
  $path = $p.Name
  if ($path -match '\{.+\}') { continue }
  $ops = $p.Value.PSObject.Properties
  foreach ($op in $ops) {
    $m = $op.Name.ToUpper()
    if ($m -eq 'GET') {
      $results += Call-Api -Method GET -Path $path -Accept @(200,204)
    }
  }
}

# 2) Real schemas flow: start PDM background + operation status polling
$pdmReq = @{ datasource_id = 2; save_to_kb = $true; background = $true }
$pdm = Call-Api -Method POST -Path '/api/schemas/pdm' -Body $pdmReq -Accept @(200)
$results += $pdm
$opId = $null
if ($pdm.ok) {
  try { $opId = (ConvertFrom-Json $pdm.body).operation_id } catch {}
}
if ($opId) {
  Start-Sleep -Milliseconds 600
  $results += Call-Api -Method GET -Path "/api/schemas/operation/$opId" -Accept @(200)
}

# 3) Real tests flow: create -> list -> run -> delete
$newTest = @{
  name = "E2E API flow test $(Get-Date -Format 'yyyyMMddHHmmss')"
  test_type = "data"
  source_query = "SELECT 1 AS v"
  target_query = "SELECT 1 AS v"
  expected_result = "match"
  severity = "low"
  is_active = $true
}
$create = Call-Api -Method POST -Path '/api/tests' -Body $newTest -Accept @(200)
$results += $create
$tid = $null
if ($create.ok) {
  try { $tid = (ConvertFrom-Json $create.body).id } catch {}
}
$results += Call-Api -Method GET -Path '/api/tests' -Accept @(200)
if ($tid) {
  $results += Call-Api -Method POST -Path "/api/tests/run/$tid" -Accept @(200)
  $results += Call-Api -Method DELETE -Path "/api/tests/$tid" -Accept @(200)
}

# summary
$pass = ($results | Where-Object { $_.ok }).Count
$total = $results.Count
$fail = $total - $pass
"LIVE_API_SUMMARY pass=$pass total=$total fail=$fail"
$results | Select-Object method,path,status,ok | Format-Table -AutoSize | Out-String
