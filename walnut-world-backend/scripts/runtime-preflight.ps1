# Read-only checks shared by Check and Start. No containers or files are created.
function Test-WalnutRuntime {
    param([string]$GodotExe, [string]$PostgresImage, [string]$SandboxImage)
    if (-not (Test-Path -LiteralPath $GodotExe -PathType Leaf)) {
        throw 'Godot is missing. Run setup-play.ps1 first, or pass -GodotExe.'
    }
    $version = (& $GodotExe --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^4\.7\.1\.stable') {
        throw 'Godot 4.7.1 stable is required. Run setup-play.ps1.'
    }
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker CLI missing. Install Docker Desktop and reopen Windows PowerShell.'
    }
    $engine = (& docker info --format '{{.OSType}}' 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $engine -ne 'linux') {
        throw 'Start Docker Desktop with Linux containers, then retry.'
    }
    foreach ($image in @($PostgresImage, $SandboxImage)) {
        & docker image inspect $image *> $null
        if ($LASTEXITCODE -ne 0) { throw "Required Docker image missing: $image . Run setup-play.ps1." }
    }
    Write-Output 'PERSISTENT_PLAY_DEPENDENCIES_READY'
}
