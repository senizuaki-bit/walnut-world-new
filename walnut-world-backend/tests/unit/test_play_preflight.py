"""Preflight uses the relay's real credential parser before any service starts."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_private_recoverable_llm_relay import _harden_test_secret_file


def test_missing_llm_key_is_reported_without_leaking_path_or_secret(tmp_path, capsys):
    from walnut_backend.play_preflight import main

    assert main(["--key-file", str(tmp_path / "private-missing.key")]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["code"] == "LLM_CONFIGURATION_INVALID"
    assert "private-missing" not in output


@pytest.mark.parametrize("contents", ["", "one\ntwo", "\ufeff"])
def test_unusable_key_cannot_pass_preflight(tmp_path, capsys, contents):
    from walnut_backend.play_preflight import main

    path = tmp_path / "key"
    path.write_text(contents, encoding="utf-8")
    _harden_test_secret_file(path)
    assert main(["--key-file", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "INVALID"


def test_voice_file_rotation_changes_fingerprint(monkeypatch, tmp_path, capsys):
    from walnut_backend.voice_preflight import main

    path = tmp_path / "book.key"
    monkeypatch.delenv("YAYA_BOOK_TTS_API_KEY", raising=False)
    monkeypatch.setenv("YAYA_BOOK_TTS_API_KEY_FILE", str(path))
    monkeypatch.setenv("YAYA_VOICE_MODE", "disabled")
    results = []
    for value in ("private-test-one", "private-test-two"):
        path.write_text(value)
        assert main() == 0
        output = capsys.readouterr().out
        assert value not in output
        results.append(json.loads(output)["configuration_sha256"])
    assert results[0] != results[1]


def test_llm_key_rotation_and_model_change_invalidate_runtime(tmp_path, capsys):
    from walnut_backend.play_preflight import main

    path = tmp_path / "model.key"
    path.write_text("test-only-key-one")
    _harden_test_secret_file(path)
    fingerprints = []
    for key, model in [
        ("test-only-key-one", "model-a"),
        ("test-only-key-one", "model-a"),
        ("test-only-key-two", "model-a"),
        ("test-only-key-two", "model-b"),
    ]:
        path.write_text(key)
        assert main(["--key-file", str(path), "--model", model]) == 0
        output = capsys.readouterr().out
        assert key not in output
        fingerprints.append(json.loads(output)["configuration_sha256"])
    assert fingerprints[0] == fingerprints[1]
    assert len(set(fingerprints)) == 3


@pytest.mark.parametrize("missing", ["none", "postgres", "sandbox"])
def test_runtime_checks_both_images_without_starting_a_container(tmp_path, missing):
    godot = tmp_path / "godot.cmd"
    godot.write_text("@echo off\n@echo 4.7.1.stable.test\n@exit /b 0\n")
    helper = Path(__file__).resolve().parents[2] / "scripts/runtime-preflight.ps1"
    env = {
        **os.environ,
        "TEST_GODOT": str(godot),
        "TEST_HELPER": str(helper),
        "TEST_MISSING": missing,
    }
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            r"""
$ErrorActionPreference = 'Stop'
. $env:TEST_HELPER
$script:calls = @()
function docker {
    $script:calls += ($args -join ' ')
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'info') { return 'linux' }
    if ($args[0] -ne 'image' -or $args[1] -ne 'inspect') { throw 'unexpected mutation' }
    if ($args[2] -eq $env:TEST_MISSING) { $global:LASTEXITCODE = 1 }
}
$failed = $false
try { Test-WalnutRuntime -GodotExe $env:TEST_GODOT -PostgresImage postgres -SandboxImage sandbox }
catch { $failed = $true }
if ($failed -ne ($env:TEST_MISSING -ne 'none')) { throw 'incorrect readiness verdict' }
if ($env:TEST_MISSING -eq 'none' -and $script:calls.Count -ne 3) { throw 'missing dependency check' }
""",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
