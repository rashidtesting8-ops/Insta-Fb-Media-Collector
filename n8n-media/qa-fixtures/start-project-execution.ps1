param([switch]$DisposableVerification)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$envPath = (Resolve-Path -LiteralPath (Join-Path $repo 'storage/phase10-local-execution/fixture.env')).Path
$scripts = "type=bind,source=$(Join-Path $repo 'n8n-media'),target=/repo/n8n-media,readonly"
$worker = "type=bind,source=$(Join-Path $repo 'services/media-worker'),target=/repo/services/media-worker,readonly"
$fixture = "type=bind,source=$(Join-Path $repo 'storage/media-delivery-preflight-20261123/synthetic-pup-bath.jpg'),target=/repo/storage/media-delivery-preflight-20261123/synthetic-pup-bath.jpg,readonly"
$frontend = "type=bind,source=$(Join-Path $repo 'frontend/.next/standalone'),target=/repo/frontend-build,readonly"
$namespace = 'container:pgm-media-n8n-1'
$prefix = 'pgm-phase10-local'
if ($DisposableVerification) {
    $namespace = 'container:pgm-phase10-verify-n8n'
    $prefix = 'pgm-phase10-verify'
    if ((docker inspect pgm-phase10-verify-n8n --format '{{.HostConfig.NetworkMode}}') -ne 'none') {
        throw 'Verification n8n must have networking disabled'
    }
}
$mode = 'org.pgm.qa.execution=fake-only'
if ((docker port pgm-media-n8n-1 5678/tcp) -cne '127.0.0.1:5679') { throw 'Project n8n is not on loopback port 5679' }

function Start-LoopbackFixture([string]$Name, [string[]]$Arguments) {
    $exists = @(docker ps -a --filter "name=^/$Name$" --format '{{.Names}}')
    if ($exists.Count) {
        $fixture = @(docker ps -a --filter "name=^/$Name$" --filter "label=$mode" --format '{{.Names}}')
        if ($fixture.Count -ne 1) { throw "Name is already used by a different container: $Name" }
        if ((docker inspect $Name --format '{{.State.Running}}') -eq 'true') { return }
        throw "Fixture is not running: $Name"
    }
    docker run --rm --detach --pull never --name $Name --label $mode --network $namespace --env-file $envPath --env PGM_QA_LOOPBACK=1 --mount $scripts @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Fixture startup failed: $Name" }
}

Start-LoopbackFixture "$prefix-worker" @('--mount',$worker,'--mount',$fixture,'--env','PYTHONPATH=/repo/services/media-worker','--no-healthcheck',
    '--entrypoint','python','pgm-media-collector','/repo/n8n-media/qa-fixtures/worker.py')
Start-LoopbackFixture "$prefix-drive" @('--mount',$frontend,'--entrypoint','node','n8nio/n8n:2.48.7',
    '/repo/n8n-media/qa-fixtures/drive.mjs')
@'
const checks = [
  ["http://127.0.0.1:8877/ready", {}, 200],
  ["http://127.0.0.1:8899/health", {}, 401],
  ["http://127.0.0.1:8899/health", { "x-qa-drive-token": process.env.PGM_QA_DRIVE_TOKEN }, 200],
];
for (const [url, headers, expected] of checks) {
  let status;
  for (let attempt = 0; attempt < 30; attempt++) {
    status = await fetch(url, { headers, signal: AbortSignal.timeout(1000) }).then(r => r.status, () => null);
    if (status === expected) break;
    await new Promise(resolve => setTimeout(resolve, 300));
  }
  if (status !== expected) process.exit(1);
}
'@ | docker exec -i "$prefix-drive" node --input-type=module -
if ($LASTEXITCODE -ne 0) { throw 'Fixture readiness or authentication check failed' }
Write-Output 'Fake-only worker and Drive fixture ready on n8n-namespace loopback with no host port mappings.'
