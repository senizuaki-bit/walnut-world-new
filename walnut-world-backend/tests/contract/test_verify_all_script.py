"""The native verification driver must propagate every failure and every skip."""

from __future__ import annotations

from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_verify_all_checks_native_exits_and_rejects_pytest_skips() -> None:
    script = (BACKEND_ROOT / "scripts" / "verify_all.ps1").read_text(encoding="utf-8")
    assert script.count("Invoke-PythonChecked ") == 5
    assert script.count("Invoke-UvxChecked ") == 3
    assert script.count("Confirm-NativeVersionChecked ") == 4
    assert "[string]$PythonExe" in script
    assert "Join-Path $RepositoryRoot '.venv\\Scripts\\python.exe'" in script
    assert "Test-Path -LiteralPath $PythonExe -PathType Leaf" in script
    assert "& $PythonExe @Arguments" in script
    assert "if ($NativeExitCode -ne 0)" in script
    assert '"--junitxml=$PytestReportPath"' in script
    assert "if ($Skipped -ne 0)" in script
    assert "required verification forbids skips" in script
    assert 'Invoke-PythonChecked "compileall" @(' in script
    assert '"-m", "compileall", "-q", "src", "tests", "migrations"' in script
    assert "py -3.12 scripts/" not in script
    assert "py -3.12 -m" not in script
    assert "& py -3.12" not in script


def test_verify_all_runs_pinned_static_tools_without_network_resolution() -> None:
    script = (BACKEND_ROOT / "scripts" / "verify_all.ps1").read_text(encoding="utf-8")

    assert '[string]$UvxExe' in script
    assert '$RuffPackage = "ruff==0.15.22"' in script
    assert '$PyrightPackage = "pyright==1.1.411"' in script
    assert '"--offline", "--from", $PackageSpec, $Command' in script
    assert '$NativeArguments += $Arguments' in script
    assert '& $UvxExe @NativeArguments' in script
    assert 'Invoke-UvxChecked "Ruff" $RuffPackage "ruff" @(' in script
    assert 'Invoke-UvxChecked "Pyright" $PyrightPackage "pyright" @(' in script
    assert 'Invoke-PythonChecked "Ruff"' not in script
    assert 'Invoke-PythonChecked "Pyright"' not in script


def test_verify_all_executes_from_repository_root_and_restores_caller_location() -> None:
    script = (BACKEND_ROOT / "scripts" / "verify_all.ps1").read_text(encoding="utf-8")

    push_index = script.index("Push-Location $RepositoryRoot")
    verification_index = script.index('Invoke-PythonChecked "contract release verification"')
    pop_index = script.rindex("Pop-Location")
    assert push_index < verification_index < pop_index
    assert "Push-Location $RepositoryRoot\ntry {" in script
    assert "finally {\n    Pop-Location\n}" in script


def test_verify_all_can_retain_a_caller_owned_pytest_report_without_overwrite() -> None:
    script = (BACKEND_ROOT / "scripts" / "verify_all.ps1").read_text(encoding="utf-8")

    assert "[string]$PytestReportPath" in script
    assert "if ([string]::IsNullOrWhiteSpace($PytestReportPath))" in script
    assert "$RemovePytestReport = $true" in script
    assert "[System.IO.Path]::GetFullPath($PytestReportPath)" in script
    assert "Test-Path -LiteralPath $PytestReportPath" in script
    assert "Refusing to overwrite existing pytest JUnit report" in script
    assert '"--junitxml=$PytestReportPath"' in script
    assert "$RemovePytestReport -and (Test-Path -LiteralPath $PytestReportPath)" in script


def test_verify_all_rejects_unexpected_static_tool_version_output() -> None:
    script = (BACKEND_ROOT / "scripts" / "verify_all.ps1").read_text(encoding="utf-8")

    assert "function Confirm-NativeVersionChecked" in script
    assert '$VersionText = (@($VersionOutput) -join "`n").Trim()' in script
    assert "if ($VersionText -cne $ExpectedVersion)" in script
    assert "reported unexpected version" in script
    assert '"ruff 0.15.22"' in script
    assert '"pyright 1.1.411"' in script
    assert '"v24.16.0"' in script


def test_verify_all_overrides_polluted_pyright_environment_and_restores_it() -> None:
    script = (BACKEND_ROOT / "scripts" / "verify_all.ps1").read_text(encoding="utf-8")

    assert "[string]$NodeExe" in script
    assert 'Get-Command "node" -CommandType Application' in script
    expected_assignments = {
        '$env:PYRIGHT_PYTHON_IGNORE_WARNINGS = "1"',
        '$env:PYRIGHT_PYTHON_FORCE_VERSION = "1.1.411"',
        '$env:PYRIGHT_PYTHON_USE_BUNDLED_PYRIGHT = "1"',
        '$env:PYRIGHT_PYTHON_GLOBAL_NODE = "1"',
        '$env:PYRIGHT_PYTHON_NODEJS_WHEEL = "0"',
        "$env:PYRIGHT_PYTHON_NODE_VERSION = $null",
        "$env:PYRIGHT_PYTHON_PYLANCE_VERSION = $null",
        "$env:PYLANCE_VERSION = $null",
    }
    assert all(assignment in script for assignment in expected_assignments)
    assert "$PreviousPyrightEnvironment" in script
    assert '[EnvironmentVariableTarget]::Process' in script
    assert "$NodeDirectory" in script
    assert "finally {\n        foreach ($Name in $PyrightEnvironmentNames)" in script
