param(
    [string]$DatabaseUrl = $env:WALNUT_DATABASE_URL,
    [string]$ContractPath = $env:WALNUT_CONTRACT_PATH,
    [string]$PythonExe,
    [string]$UvxExe,
    [string]$NodeExe,
    [string]$PytestReportPath
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$RuffPackage = "ruff==0.15.22"
$PyrightPackage = "pyright==1.1.411"

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = Join-Path $RepositoryRoot '.venv\Scripts\python.exe'
}
$PythonExe = [System.IO.Path]::GetFullPath($PythonExe)
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Repository Python 3.12 runtime was not found: $PythonExe"
}

if ([string]::IsNullOrWhiteSpace($UvxExe)) {
    $UvxCommand = Get-Command "uvx" -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $UvxCommand) {
        throw "uvx was not found; pinned offline Ruff and Pyright are required."
    }
    $UvxExe = $UvxCommand.Source
}
$UvxExe = [System.IO.Path]::GetFullPath($UvxExe)
if (-not (Test-Path -LiteralPath $UvxExe -PathType Leaf)) {
    throw "uvx executable was not found: $UvxExe"
}

if ([string]::IsNullOrWhiteSpace($NodeExe)) {
    $NodeCommand = Get-Command "node" -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $NodeCommand) {
        throw "Node.js was not found; pinned Pyright must not install nodeenv."
    }
    $NodeExe = $NodeCommand.Source
}
$NodeExe = [System.IO.Path]::GetFullPath($NodeExe)
if (-not (Test-Path -LiteralPath $NodeExe -PathType Leaf)) {
    throw "Node.js executable was not found: $NodeExe"
}

if ([string]::IsNullOrWhiteSpace($ContractPath)) {
    $ContractPath = Join-Path (Split-Path -Parent $RepositoryRoot) "agent"
}
$ContractPath = [System.IO.Path]::GetFullPath($ContractPath)

if ([string]::IsNullOrWhiteSpace($DatabaseUrl)) {
    throw "Set WALNUT_DATABASE_URL or pass -DatabaseUrl before verification."
}
if (-not (Test-Path -LiteralPath $ContractPath)) {
    throw "Locked Agent contract repository was not found: $ContractPath"
}

$RemovePytestReport = $false
if ([string]::IsNullOrWhiteSpace($PytestReportPath)) {
    $PytestReportPath = Join-Path (
        [System.IO.Path]::GetTempPath()
    ) ("walnut-pytest-{0}.xml" -f [System.Guid]::NewGuid().ToString("N"))
    $RemovePytestReport = $true
}
else {
    $PytestReportPath = [System.IO.Path]::GetFullPath($PytestReportPath)
    if (Test-Path -LiteralPath $PytestReportPath) {
        throw "Refusing to overwrite existing pytest JUnit report: $PytestReportPath"
    }
    $PytestReportDirectory = Split-Path -Parent $PytestReportPath
    if (-not (Test-Path -LiteralPath $PytestReportDirectory -PathType Container)) {
        throw "pytest JUnit report directory was not found: $PytestReportDirectory"
    }
}

$env:WALNUT_DATABASE_URL = $DatabaseUrl
$env:WALNUT_TEST_DATABASE_URL = $DatabaseUrl
$env:WALNUT_CONTRACT_PATH = $ContractPath

function Invoke-PythonChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $PythonExe @Arguments
    $NativeExitCode = $LASTEXITCODE
    if ($NativeExitCode -ne 0) {
        throw "$Label failed with native exit code $NativeExitCode."
    }
}

function Invoke-UvxChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [string]$PackageSpec,
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    # Build one explicit argv array.  Inline comma-separated native arguments are
    # interpreted differently by Windows PowerShell 5.1 and PowerShell 7.
    $NativeArguments = @("--offline", "--from", $PackageSpec, $Command)
    $NativeArguments += $Arguments
    & $UvxExe @NativeArguments
    $NativeExitCode = $LASTEXITCODE
    if ($NativeExitCode -ne 0) {
        throw "$Label failed with native exit code $NativeExitCode."
    }
}

function Confirm-NativeVersionChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedVersion,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $VersionOutput = & $Executable @Arguments
    $NativeExitCode = $LASTEXITCODE
    if ($NativeExitCode -ne 0) {
        throw "$Label failed with native exit code $NativeExitCode."
    }
    $VersionText = (@($VersionOutput) -join "`n").Trim()
    if ($VersionText -cne $ExpectedVersion) {
        throw "$Label reported unexpected version '$VersionText'; expected '$ExpectedVersion'."
    }
    Write-Output $VersionText
}

Push-Location $RepositoryRoot
try {
    Invoke-PythonChecked "contract release verification" @(
        "scripts/verify_contract_release.py", "--agent-repo", $ContractPath
    )
    Invoke-PythonChecked "Alembic upgrade" @("-m", "alembic", "upgrade", "head")
    Confirm-NativeVersionChecked "Ruff version" $UvxExe "ruff 0.15.22" @(
        "--offline", "--from", $RuffPackage, "ruff", "--version"
    )
    Invoke-UvxChecked "Ruff" $RuffPackage "ruff" @(
        "check", "src", "tests", "migrations"
    )
    $PyrightEnvironmentNames = @(
        "PATH",
        "PYLANCE_VERSION",
        "PYRIGHT_PYTHON_FORCE_VERSION",
        "PYRIGHT_PYTHON_GLOBAL_NODE",
        "PYRIGHT_PYTHON_IGNORE_WARNINGS",
        "PYRIGHT_PYTHON_NODEJS_WHEEL",
        "PYRIGHT_PYTHON_NODE_VERSION",
        "PYRIGHT_PYTHON_PYLANCE_VERSION",
        "PYRIGHT_PYTHON_USE_BUNDLED_PYRIGHT"
    )
    $PreviousPyrightEnvironment = @{}
    foreach ($Name in $PyrightEnvironmentNames) {
        $PreviousPyrightEnvironment[$Name] = [Environment]::GetEnvironmentVariable(
            $Name,
            [EnvironmentVariableTarget]::Process
        )
    }
    try {
        $NodeDirectory = Split-Path -Parent $NodeExe
        $env:PATH = "$NodeDirectory$([System.IO.Path]::PathSeparator)$env:PATH"
        $env:PYRIGHT_PYTHON_IGNORE_WARNINGS = "1"
        $env:PYRIGHT_PYTHON_FORCE_VERSION = "1.1.411"
        $env:PYRIGHT_PYTHON_USE_BUNDLED_PYRIGHT = "1"
        $env:PYRIGHT_PYTHON_GLOBAL_NODE = "1"
        $env:PYRIGHT_PYTHON_NODEJS_WHEEL = "0"
        $env:PYRIGHT_PYTHON_NODE_VERSION = $null
        $env:PYRIGHT_PYTHON_PYLANCE_VERSION = $null
        $env:PYLANCE_VERSION = $null

        Confirm-NativeVersionChecked "Node.js version" $NodeExe "v24.16.0" @("--version")
        Confirm-NativeVersionChecked "Pyright version" $UvxExe "pyright 1.1.411" @(
            "--offline", "--from", $PyrightPackage, "pyright", "--version"
        )
        Invoke-UvxChecked "Pyright" $PyrightPackage "pyright" @(
            "--pythonpath", $PythonExe, "src"
        )
    }
    finally {
        foreach ($Name in $PyrightEnvironmentNames) {
            [Environment]::SetEnvironmentVariable(
                $Name,
                $PreviousPyrightEnvironment[$Name],
                [EnvironmentVariableTarget]::Process
            )
        }
    }
    Invoke-PythonChecked "compileall" @(
        "-m", "compileall", "-q", "src", "tests", "migrations"
    )

    try {
        Invoke-PythonChecked "pytest" @(
            "-m", "pytest", "tests", "-q", "--junitxml=$PytestReportPath"
        )
        if (-not (Test-Path -LiteralPath $PytestReportPath)) {
            throw "pytest did not write its required JUnit report."
        }
        [xml]$PytestXml = Get-Content -LiteralPath $PytestReportPath -Raw
        $Skipped = 0
        foreach ($Suite in @($PytestXml.testsuites.testsuite)) {
            if ($null -ne $Suite.skipped) {
                $Skipped += [int]$Suite.skipped
            }
        }
        if ($Skipped -ne 0) {
            throw "pytest reported $Skipped skipped test(s); required verification forbids skips."
        }
    }
    finally {
        if ($RemovePytestReport -and (Test-Path -LiteralPath $PytestReportPath)) {
            Remove-Item -LiteralPath $PytestReportPath -Force
        }
    }
}
finally {
    Pop-Location
}
