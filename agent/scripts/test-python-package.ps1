[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$agentRoot = Split-Path -Parent $PSScriptRoot
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$workRoot = Join-Path $tempRoot ("yaya-agent-package-" + [guid]::NewGuid().ToString("N"))
$stageRoot = Join-Path $workRoot "source"
$wheelRoot = Join-Path $workRoot "wheel"
$venvRoot = Join-Path $workRoot "venv"
$pythonExe = $env:YAYA_PYTHON_EXE

if ([string]::IsNullOrWhiteSpace($pythonExe)) {
    $pythonCommand = Get-Command python.exe,python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
} else {
    $pythonCommand = Get-Command $pythonExe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
}
if (-not $pythonCommand) {
    throw "Python 3.12 is missing. Set YAYA_PYTHON_EXE or add python to PATH."
}
$pythonExe = $pythonCommand.Source

try {
    New-Item -ItemType Directory -Path $stageRoot, $wheelRoot -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $agentRoot "pyproject.toml") -Destination $stageRoot
    Copy-Item -LiteralPath (Join-Path $agentRoot "README.md") -Destination $stageRoot
    Copy-Item -LiteralPath (Join-Path $agentRoot "python") -Destination $stageRoot -Recurse

    & $pythonExe -m build --wheel --outdir $wheelRoot $stageRoot
    if ($LASTEXITCODE -ne 0) { throw "Python wheel build failed." }
    $wheels = @(Get-ChildItem -LiteralPath $wheelRoot -Filter "*.whl" -File)
    if ($wheels.Count -ne 1) { throw "Expected exactly one Python wheel, found $($wheels.Count)." }

    & $pythonExe -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw "Isolated Python virtual environment creation failed." }
    $venvPython = if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        Join-Path $venvRoot "Scripts\python.exe"
    } else {
        Join-Path $venvRoot "bin/python"
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Isolated Python virtual environment is missing its interpreter."
    }

    $previousPythonPath = $env:PYTHONPATH
    $previousPythonHome = $env:PYTHONHOME
    try {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
        & $venvPython -I -m pip install --disable-pip-version-check --no-cache-dir $wheels[0].FullName
        if ($LASTEXITCODE -ne 0) { throw "Python wheel installation failed." }
        & $venvPython -I -m pip check
        if ($LASTEXITCODE -ne 0) { throw "Installed Python wheel has unsatisfied dependencies." }
        & $venvPython -I -c "import sys; import yaya_agent_build; import yaya_agent_sandbox; from importlib.resources import files; assert yaya_agent_build.DigestPinnedDockerCppBuilder; assert yaya_agent_sandbox.DockerCppSandbox; assert files('yaya_agent_build').joinpath('py.typed').is_file(); assert files('yaya_agent_sandbox').joinpath('py.typed').is_file(); assert not any(name == 'yaya_agent_backend' or name.startswith('yaya_agent_backend.') or name == 'psycopg' or name.startswith('psycopg.') for name in sys.modules)"
        if ($LASTEXITCODE -ne 0) { throw "Installed provider-neutral Build/Sandbox package import failed." }
        & $venvPython -I -c "import yaya_agent_contracts; from importlib.resources import files; from yaya_agent_backend.composition import create_production_composition; from yaya_agent_backend.repositories import PostgresAgentTurnRepository; from yaya_agent_backend.world_uow import PostgresWorldUnitOfWork; from yaya_agent_runtime import PackagedRoleConfigProvider; assert create_production_composition and PostgresAgentTurnRepository and PostgresWorldUnitOfWork; assert 'WorldUnitOfWorkPort' in yaya_agent_contracts.__all__; assert files('yaya_agent_contracts').joinpath('py.typed').is_file(); assert files('yaya_agent_runtime').joinpath('py.typed').is_file(); configs = PackagedRoleConfigProvider.load(); assert configs.get('world_agent').id == 'world_agent'; migrations = files('yaya_agent_backend.migrations'); assert all(migrations.joinpath(name).is_file() for name in ('0001_agent_turn.sql', '0002_learner_projection.sql', '0003_student_skill_chain.sql', '0003_student_skill_chain.down.sql'))"
        if ($LASTEXITCODE -ne 0) { throw "Installed Python package import failed." }
    } finally {
        $env:PYTHONPATH = $previousPythonPath
        $env:PYTHONHOME = $previousPythonHome
    }
} finally {
    $resolvedWork = [IO.Path]::GetFullPath($workRoot)
    if (-not $resolvedWork.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove unexpected package-test path: $resolvedWork"
    }
    if (Test-Path -LiteralPath $resolvedWork) {
        Remove-Item -LiteralPath $resolvedWork -Recurse -Force
    }
}

Write-Output "AGENT_PYTHON_PACKAGE_TEST_OK"
