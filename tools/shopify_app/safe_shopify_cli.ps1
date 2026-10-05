param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('Link','Validate','Deploy')]
    [string]$Action,
    [switch]$ConfirmProductionDeploy
)

$ErrorActionPreference = 'Stop'
$appPath = (Resolve-Path $PSScriptRoot).Path
$repoPath = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$cli = Get-Command shopify -ErrorAction SilentlyContinue
if (-not $cli) { throw 'Shopify CLI가 없습니다. 공식 Shopify CLI 설치 후 다시 실행하세요.' }

if ($Action -eq 'Link') {
    Write-Host '기존 Shopify Production App을 연결합니다. 새 앱 생성은 선택하지 마세요.'
    Push-Location $appPath
    try { & $cli.Source app config link; if ($LASTEXITCODE -ne 0) { throw "Shopify CLI link failed ($LASTEXITCODE)" } }
    finally { Pop-Location }
    exit 0
}

$configs = @(Get-ChildItem -LiteralPath $appPath -Filter 'shopify.app*.toml' -File)
if ($configs.Count -eq 0) { throw '연결된 app config가 없습니다. 먼저 Action Link를 실행하세요.' }
foreach ($config in $configs) {
    $text = Get-Content -LiteralPath $config.FullName -Raw -Encoding utf8
    if ($text -match '(?im)^\s*client_secret\s*=') { throw "Secret 필드는 CLI app config에 저장하면 안 됩니다: $($config.Name)" }
    if ($text -notmatch '(?im)^\s*client_id\s*=\s*"[^"]+"') { throw "Client ID를 확인할 수 없습니다: $($config.Name)" }
    $match = [regex]::Match($text, '(?im)^\s*client_id\s*=\s*"([^"]+)"')
    $safeId = if ($match.Success -and $match.Groups[1].Value.Length -ge 6) { '…' + $match.Groups[1].Value.Substring($match.Groups[1].Value.Length - 6) } else { '(hidden)' }
    Write-Host "Config: $($config.Name) · Client ID ending $safeId"
}
if ($Action -eq 'Validate') { Write-Host '정적 config 검사 완료. Dashboard app identity/scope와 별도 대조가 필요합니다.'; exit 0 }
if (-not $ConfirmProductionDeploy) { throw '실제 앱 설정 변경은 -ConfirmProductionDeploy 명시가 필요합니다.' }
$typed = Read-Host '이 Production App의 설정/version을 배포하려면 DEPLOY 입력'
if ($typed -cne 'DEPLOY') { throw '배포 취소됨' }
Push-Location $appPath
try { & $cli.Source app deploy; if ($LASTEXITCODE -ne 0) { throw "Shopify CLI deploy failed ($LASTEXITCODE)" } }
finally { Pop-Location }
