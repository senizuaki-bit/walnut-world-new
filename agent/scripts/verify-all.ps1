[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$agentRoot = Split-Path -Parent $PSScriptRoot
$repositoryRoot = Split-Path -Parent $agentRoot
$nodeExe = $env:YAYA_NODE_EXE
$pythonExe = $env:YAYA_PYTHON_EXE
$previousPythonExe = $env:YAYA_PYTHON_EXE

function Invoke-NativeGate {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

if ([string]::IsNullOrWhiteSpace($nodeExe)) {
    $nodeExe = Join-Path $repositoryRoot "tools\node\node.exe"
}

if (-not (Test-Path -LiteralPath $nodeExe -PathType Leaf)) {
    $nodeCommand = Get-Command node.exe,node -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $nodeCommand) {
        throw "Node.js 20+ is missing. Set YAYA_NODE_EXE or add node to PATH."
    }
    $nodeExe = $nodeCommand.Source
}

if ([string]::IsNullOrWhiteSpace($pythonExe)) {
    $pythonCommand = Get-Command python.exe,python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
} else {
    $pythonCommand = Get-Command $pythonExe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
}
if (-not $pythonCommand) {
    throw "Python 3.12 is missing. Set YAYA_PYTHON_EXE or add python to PATH."
}
$pythonExe = $pythonCommand.Source

$packageLock = Join-Path $agentRoot "package-lock.json"
$typescriptEntry = Join-Path $agentRoot "node_modules/typescript/bin/tsc"
$pyrightEntry = Join-Path $agentRoot "node_modules/pyright/index.js"

if (-not (Test-Path -LiteralPath $packageLock -PathType Leaf)) {
    throw "package-lock.json is missing; the TypeScript/Pyright toolchain is not reproducible."
}
if (-not (Test-Path -LiteralPath $typescriptEntry -PathType Leaf) -or
    -not (Test-Path -LiteralPath $pyrightEntry -PathType Leaf)) {
    throw "Locked Node development dependencies are missing. Run 'npm ci --ignore-scripts' first."
}

Push-Location $agentRoot
try {
    # Node's cross-language tests must use the same locked Python environment.
    $env:YAYA_PYTHON_EXE = $pythonExe
    Invoke-NativeGate "Node.js version check" $nodeExe @(
        "-e",
        "process.exit(Number(process.versions.node.split('.')[0]) >= 20 ? 0 : 1)"
    )
    Invoke-NativeGate "Python version check" $pythonExe @(
        "-c",
        "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
    )

    # Single-quoted JavaScript strings survive native argument marshalling on both
    # Windows PowerShell and PowerShell Core on Linux.
    $lockedDependencyCheck = @'
const fs = require('node:fs');
const lock = JSON.parse(fs.readFileSync('package-lock.json', 'utf8'));
for (const name of ['typescript', 'pyright']) {
  const expected = lock.packages?.[`node_modules/${name}`]?.version;
  const actual = JSON.parse(
    fs.readFileSync(`node_modules/${name}/package.json`, 'utf8'),
  ).version;
  if (!expected || actual !== expected) {
    console.error(`${name} does not match package-lock.json: expected=${expected}, actual=${actual}`);
    process.exitCode = 1;
  }
}
'@
    Invoke-NativeGate "Locked Node dependency verification" $nodeExe @("-e", $lockedDependencyCheck)

    Invoke-NativeGate "Contract validation" $nodeExe @("scripts/validate-contracts.mjs")

    Invoke-NativeGate "Port signature lock" $nodeExe @("scripts/port-surface.mjs", "--check")

    Invoke-NativeGate "TypeScript type check" $nodeExe @(
        $typescriptEntry,
        "--noEmit",
        "-p",
        "tsconfig.json"
    )

    Invoke-NativeGate "Node contract tests" $nodeExe @("scripts/run-node-tests.mjs")

    Invoke-NativeGate "Pyright type check" $nodeExe @(
        $pyrightEntry,
        "--pythonpath",
        $pythonExe,
        # The backend ships an intentional Win32 Job Object implementation.
        # Pinning the release target keeps Pyright deterministic when this gate
        # runs on an Ubuntu host; Linux runtime paths are exercised below.
        "--pythonplatform",
        "Windows",
        "python/yaya_agent_contracts",
        "python/yaya_agent_build",
        "python/yaya_agent_sandbox",
        "python/yaya_agent_runtime",
        "python/yaya_agent_backend"
    )

    Invoke-NativeGate "Ruff lint" $pythonExe @("-m", "ruff", "check", "python", "tests")

    Invoke-NativeGate "Ruff format check" $pythonExe @(
        "-m",
        "ruff",
        "format",
        "--check",
        "python",
        "tests"
    )

    Invoke-NativeGate "Python contract tests" $pythonExe @(
        "scripts/run-non-live-python-tests.py"
    )

    Invoke-NativeGate "Python bytecode compilation" $pythonExe @(
        "-m",
        "compileall",
        "-q",
        "python",
        "tests"
    )

    & (Join-Path $PSScriptRoot "test-python-package.ps1")

    & (Join-Path $PSScriptRoot "test-godot-contracts.ps1")
} finally {
    $env:YAYA_PYTHON_EXE = $previousPythonExe
    Pop-Location
}

Write-Output "AGENT_INTERFACE_ALL_TESTS_OK"
