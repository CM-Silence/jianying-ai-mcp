# One-time Windows x64 installation. The editor environment and client configs live in this checkout.
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$uvVersion = '0.12.13'
$uv = Get-Command uv.exe -ErrorAction SilentlyContinue
$useSystemUv = $uv -and ((& $uv.Source --version) -split ' ')[1] -eq $uvVersion
if (-not $useSystemUv) {
    $bootstrap = Join-Path $repo ".build/bootstrap/uv-$uvVersion"
    $uvPath = Join-Path $bootstrap 'uv.exe'
    if (-not (Test-Path -LiteralPath $uvPath -PathType Leaf)) {
        $release = Invoke-RestMethod "https://pypi.org/pypi/uv/$uvVersion/json"
        $wheel = $release.urls | Where-Object { $_.filename -like '*-win_amd64.whl' } | Select-Object -First 1
        if (-not $wheel) { throw 'The Windows x64 uv wheel is missing.' }
        New-Item -ItemType Directory -Force -Path $bootstrap | Out-Null
        $stage = Join-Path $bootstrap ([guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $stage | Out-Null
        $archive = Join-Path $stage 'uv.zip'
        Invoke-WebRequest -UseBasicParsing $wheel.url -OutFile $archive
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash -ne $wheel.digests.sha256) {
            throw 'uv download checksum mismatch.'
        }
        Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $stage 'wheel')
        $binary = Get-ChildItem -LiteralPath (Join-Path $stage 'wheel') -Recurse -Filter uv.exe | Select-Object -First 1
        Copy-Item -LiteralPath $binary.FullName -Destination $uvPath
    }
    $env:PATH = "$bootstrap;$env:PATH"
} else {
    $uvPath = $uv.Source
}
if (-not $env:UV_PROJECT_ENVIRONMENT) { $env:UV_PROJECT_ENVIRONMENT = Join-Path $repo 'runtime/python' }
if (-not $env:JIANYING_RUNTIME_HOME) { $env:JIANYING_RUNTIME_HOME = Join-Path $repo 'runtime/installed' }
& $uvPath run --project $repo --locked --python 3.12 --no-dev jianying-ai-mcp setup
exit $LASTEXITCODE
