$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$envPath = (Resolve-Path -LiteralPath (Join-Path $repo 'storage/phase10-local-execution/fixture.env')).Path
$scripts = "type=bind,source=$(Join-Path $repo 'n8n-media'),target=/repo/n8n-media,readonly"
$name = 'pgm-phase10-verify-n8n'
$mode = 'org.pgm.qa.execution=fake-only'
$ownedNames = @($name, 'pgm-phase10-verify-worker', 'pgm-phase10-verify-drive')
foreach ($fixture in $ownedNames) {
    if (@(docker ps -a --filter "name=^/$fixture$" --format '{{.Names}}').Count) { throw "Existing fixture name: $fixture" }
}
try {
    docker run --rm --detach --pull never --name $name --label $mode --network none --env-file $envPath --mount $scripts --env PGM_QA_LOOPBACK=1 --env N8N_ENDPOINT_WEBHOOK=webhook-test --env N8N_ENDPOINT_WEBHOOK_TEST=fixture-editor-only --env N8N_LISTEN_ADDRESS=127.0.0.1 --env N8N_DIAGNOSTICS_ENABLED=false --env N8N_VERSION_NOTIFICATIONS_ENABLED=false --env N8N_TEMPLATES_ENABLED=false --env N8N_RUNNERS_GRANT_TOKEN_TTL=120 --entrypoint node n8nio/n8n:2.48.7 /repo/n8n-media/qa-fixtures/n8n.mjs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable n8n' }
    & (Join-Path $PSScriptRoot 'start-project-execution.ps1') -DisposableVerification
    docker exec pgm-phase10-verify-drive node /repo/n8n-media/qa-fixtures/project-dashboard.mjs
    if ($LASTEXITCODE -ne 0) {
        foreach ($fixture in $ownedNames) { docker logs --tail 20 $fixture }
        throw 'Local execution verification failed'
    }
} finally {
    foreach ($fixture in @('pgm-phase10-verify-drive', 'pgm-phase10-verify-worker', $name)) {
        if (@(docker ps --filter "name=^/$fixture$" --filter "label=$mode" --format '{{.Names}}').Count) {
            docker stop $fixture | Out-Null
        }
    }
}
