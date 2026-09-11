[CmdletBinding()]
param([string]$GodotExe = $env:GODOT_EXE)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -ne 'Desktop') { throw 'Use Windows PowerShell 5.1 (powershell.exe).' }
$backendRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $backendRoot
if ($null -eq (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Install uv first: winget install --id astral-sh.uv --exact ; then reopen PowerShell.'
}
if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Install Docker Desktop with Linux containers and reopen PowerShell.'
}
& docker info --format '{{.OSType}}'
if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop before running setup.' }

# Only this checkout is installed. Never download a separately published Agent package.
& uv python install 3.12.13
if ($LASTEXITCODE -ne 0) { throw 'Python download failed; check network access and retry.' }
$python = Join-Path $backendRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & uv venv --python 3.12.13 (Join-Path $backendRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the backend virtual environment.' }
}
& $python -c 'import sys; assert sys.version_info[:3] == (3,12,13), "Use Python 3.12.13 in the backend .venv"'
if ($LASTEXITCODE -ne 0) { throw 'Existing .venv uses a different Python. Rename it as a backup and rerun setup.' }
& uv pip install --python $python --requirement (Join-Path $backendRoot 'requirements-play-win-py312.txt')
if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
& uv pip install --python $python --no-deps --no-build-isolation --editable (Join-Path $workspaceRoot 'agent') --editable $backendRoot
if ($LASTEXITCODE -ne 0) { throw 'Local Agent/backend installation failed.' }
& uv pip check --python $python
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies are inconsistent.' }

# Reuse the exact image defaults declared by the launcher.
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'start-persistent-play.ps1'), [ref]$tokens, [ref]$parseErrors)
foreach ($name in @('PostgresImage', 'SandboxImage')) {
    $parameter = $ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq $name }
    $image = $parameter.DefaultValue.SafeGetValue()
    & docker pull $image
    if ($LASTEXITCODE -ne 0) { throw "Docker image download failed: $image" }
}

if ([string]::IsNullOrWhiteSpace($GodotExe)) {
    $destination = Join-Path $workspaceRoot 'tools\godot-4.7.1'
    $GodotExe = Join-Path $destination 'Godot_v4.7.1-stable_win64.exe'
    if (-not (Test-Path -LiteralPath $GodotExe)) {
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        $archive = Join-Path $destination 'Godot_v4.7.1-stable_win64.exe.zip'
        $download = 'https://github.com/godotengine/godot-builds/releases/download/4.7.1-stable/Godot_v4.7.1-stable_win64.exe.zip'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -UseBasicParsing -Uri $download -OutFile $archive
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ine 'c7a289051eaefb460b0106b60e9cd5bee0ef55fd102dcb2bed1eb356cf3d90a1') {
            throw 'Godot archive checksum mismatch. Nothing was extracted; rerun setup.'
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $destination -Force
    }
}
. (Join-Path $PSScriptRoot 'runtime-preflight.ps1')
$images = @{}
foreach ($name in @('PostgresImage', 'SandboxImage')) {
    $parameter = $ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq $name }
    $images[$name] = $parameter.DefaultValue.SafeGetValue()
}
Test-WalnutRuntime -GodotExe $GodotExe @images
# Fresh clones have no .godot cache or global script class index.
$project = Join-Path $workspaceRoot 'walnut-world-frontend'
$logRoot = Join-Path $workspaceRoot 'tools\setup-logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
# PowerShell does not wait for a GUI .exe by default. Wait explicitly, then
# reopen the imported project to distinguish first-scan cache misses from errors.
foreach ($phase in @('import', 'verify')) {
    $arguments = @('--headless', '--editor', '--path', "`"$project`"")
    $arguments += $(if ($phase -eq 'import') { '--import' } else { '--quit' })
    $stdout = Join-Path $logRoot "$phase.stdout.log"
    $stderr = Join-Path $logRoot "$phase.stderr.log"
    $process = Start-Process -FilePath $GodotExe -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    if ($process.ExitCode -ne 0) { throw "Godot $phase failed; see $logRoot" }
    if ($phase -eq 'verify' -and (Select-String -LiteralPath $stdout, $stderr -Pattern '^(SCRIPT ERROR|ERROR):' -Quiet)) {
        throw "Godot still reports errors after importing assets; see $logRoot"
    }
}
Write-Output 'SETUP_PLAY_READY: configure private provider keys, then run start-persistent-play.ps1 -Action Check and -Action Start.'
