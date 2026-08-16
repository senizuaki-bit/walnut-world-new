[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$repositoryRoot = Split-Path -Parent $workspaceRoot
$godotExe = $env:YAYA_GODOT_EXE
$projectRoot = Join-Path $workspaceRoot "clients\godot"

if ([string]::IsNullOrWhiteSpace($godotExe)) {
    $godotExe = Join-Path $repositoryRoot "tools\godot-4.5.2\Godot_v4.5.2-stable_win64_console.exe"
}

if (-not (Test-Path -LiteralPath $godotExe -PathType Leaf)) {
    $candidate = Get-Command godot4_console.exe,godot4.exe,godot.exe,godot4,godot -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $candidate) {
        throw "Godot 4.5 executable is missing. Set YAYA_GODOT_EXE or add Godot to PATH."
    }
    $godotExe = $candidate.Source
}

function Invoke-GodotTestRunner {
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string]$SuccessMarker
    )

    $output = @(& $godotExe --headless --path $projectRoot --rendering-method gl_compatibility --script $Script 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "$Script exited with code $LASTEXITCODE.`n$($output -join [Environment]::NewLine)"
    }

    $text = $output -join [Environment]::NewLine
    if ($text -match "(?m)^(?:SCRIPT ERROR|ERROR:|WARNING:)") {
        throw "$Script emitted a diagnostic.`n$text"
    }
    if (-not $text.Contains($SuccessMarker)) {
        throw "$Script success marker $SuccessMarker is missing.`n$text"
    }
    Write-Output $SuccessMarker
}

Invoke-GodotTestRunner -Script "res://contract_test_runner.gd" -SuccessMarker "AGENT_GODOT_CONTRACT_TEST_OK"
Invoke-GodotTestRunner -Script "res://http_transport_test_runner.gd" -SuccessMarker "AGENT_GODOT_HTTP_TRANSPORT_TEST_OK"
