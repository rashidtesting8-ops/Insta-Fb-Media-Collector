param([string]$N8nImage = 'n8nio/n8n:2.48.7', [string]$WorkerImage = 'pgm-media-collector:latest', [string]$Scenario)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$mount = "type=bind,source=$repo,target=/repo,readonly"
$network = 'pgm-phase10-qa-' + [guid]::NewGuid().ToString('N').Substring(0,12)
$runId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
$containers = [System.Collections.Generic.List[string]]::new()

function Start-Fixture([string[]]$Arguments) {
    $id = docker run --detach --pull never --network $network --mount $mount @Arguments
    if ($LASTEXITCODE -ne 0 -or $id -notmatch '^[a-f0-9]{64}$') { throw 'Could not start an isolated QA fixture' }
    $containers.Add($id)
    return $id
}

docker network create --internal $network | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not create the internal QA network' }
$evidence = Join-Path $repo 'storage/qa-diagnostics'
if (-Not (Test-Path $evidence)) { New-Item -Path $evidence -ItemType Directory -Force | Out-Null }
$diagEnv = @(
    '--env', 'PGM_QA_DIAGNOSTICS_ROOT=/qa-diagnostics',
    '--env', ('PGM_QA_RUN_ID=' + $runId)
)
$diagMount = @('--mount', "type=bind,source=$evidence,target=/qa-diagnostics")
try {
    $namedSecret = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    $namedPwA = [guid]::NewGuid().ToString('N')
    $namedPwB = [guid]::NewGuid().ToString('N')
    $namedEnv = @(
        '--env', ('PGM_QA_NAMED_ASSERTION_SECRET=' + $namedSecret),
        '--env', ('PGM_QA_NAMED_PASSWORD_A=' + $namedPwA),
        '--env', ('PGM_QA_NAMED_PASSWORD_B=' + $namedPwB)
    )
    $worker = Start-Fixture (@('--network-alias','pgm-qa-worker','--env','PYTHONPATH=/repo/services/media-worker') + $namedEnv + $diagEnv + $diagMount +
        @('--entrypoint','python',$WorkerImage,'/repo/n8n-media/qa-fixtures/worker.py'))
    $drive = Start-Fixture (@('--network-alias','pgm-qa-adapter') + $diagEnv + $diagMount +
        @('--entrypoint','node',$N8nImage,'/repo/n8n-media/qa-fixtures/drive.mjs'))
    $automation = Start-Fixture (@('--env','N8N_DIAGNOSTICS_ENABLED=false','--env','N8N_VERSION_NOTIFICATIONS_ENABLED=false',
        '--env','N8N_TEMPLATES_ENABLED=false') + $diagEnv + $diagMount +
        @('--entrypoint','node',$N8nImage,'/repo/n8n-media/qa-fixtures/n8n.mjs'))
    $execArgs = @(
        '--env', ('PGM_QA_SCENARIO=' + $Scenario),
        '--env', 'PGM_QA_DIAGNOSTICS_ROOT=/qa-diagnostics',
        '--env', ('PGM_QA_RUN_ID=' + $runId),
        '--env', ('PGM_QA_NAMED_ASSERTION_SECRET=' + $namedSecret),
        '--env', ('PGM_QA_NAMED_PASSWORD_A=' + $namedPwA),
        '--env', ('PGM_QA_NAMED_PASSWORD_B=' + $namedPwB),
        $automation, 'node', '/repo/n8n-media/qa-fixtures/dashboard.mjs'
    )
    docker exec @execArgs
    if ($LASTEXITCODE -ne 0) {
        foreach ($id in $containers) { docker logs --tail 45 $id }
        throw 'Canonical mock E2E failed'
    }
    Write-Output ('QA evidence directory: ' + (Join-Path $evidence $runId))
} finally {
    foreach ($id in $containers) {
        docker rm --force --volumes $id | Out-Null
    }
    docker network rm $network | Out-Null
}
