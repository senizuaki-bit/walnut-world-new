"""Real launcher configuration resolution in fresh Windows PowerShell processes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2] / "scripts" / "voice-environment.ps1"
VOICE_NAMES = (
    "YAYA_VOICE_MODE",
    "YAYA_BOOK_TTS_API_KEY",
    "YAYA_BOOK_TTS_API_KEY_FILE",
    "YAYA_DOUBAO_VOICE_API_KEY",
    "YAYA_DOUBAO_VOICE_API_KEY_FILE",
)


def resolve(tmp_path, overrides=None):
    environment = {k: v for k, v in os.environ.items() if k not in VOICE_NAMES}
    environment.update(overrides or {})
    environment.update(
        TEST_VOICE_HELPER=str(HELPER),
        TEST_SECRETS=str(tmp_path / "private"),
        TEST_AGENT=str(tmp_path / "new checkout" / "agent"),
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; . $env:TEST_VOICE_HELPER; Initialize-WalnutVoiceEnvironment "
            "-SecretsDirectory $env:TEST_SECRETS -AgentRoot $env:TEST_AGENT; "
            "@{book=$env:YAYA_BOOK_TTS_API_KEY_FILE; voice=$env:YAYA_DOUBAO_VOICE_API_KEY_FILE; "
            "mode=$env:YAYA_VOICE_MODE} | ConvertTo-Json -Compress",
        ],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_fresh_shell_and_moved_checkout_use_persistent_book_file(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    book = private / "book-tts.key"
    book.write_text("test-only")
    for _ in range(2):
        result = resolve(tmp_path)
        assert result == {"book": str(book), "voice": None, "mode": "disabled"}


def test_explicit_source_is_never_replaced_by_a_default(tmp_path):
    result = resolve(tmp_path, {"YAYA_BOOK_TTS_API_KEY_FILE": "missing-explicit.key"})
    assert result["book"] == str(tmp_path / "missing-explicit.key")
    assert result["mode"] == "disabled"


def test_voice_file_enables_voice_but_explicit_disable_wins(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    voice = private / "doubao-voice.key"
    voice.write_text("test-only")
    assert resolve(tmp_path)["mode"] == "doubao"
    assert resolve(tmp_path, {"YAYA_VOICE_MODE": "disabled"})["mode"] == "disabled"


def test_check_mode_rejects_missing_book_file_before_runtime_start():
    environment = {k: v for k, v in os.environ.items() if k not in VOICE_NAMES}
    environment["YAYA_BOOK_TTS_API_KEY_FILE"] = "Z:\\missing-walnut-secret.key"
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(HELPER.with_name("start-persistent-play.ps1")),
            "-Action",
            "Check",
            "-PythonExe",
            sys.executable,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "BOOK_SPEECH_CONFIGURATION_INVALID" in result.stdout + result.stderr
    assert "PERSISTENT_PLAY_POSTGRES" not in result.stdout


@pytest.mark.parametrize("bad", ["", "one\ntwo", "\ufeff"])
def test_bad_explicit_file_cannot_fall_back(monkeypatch, tmp_path, bad):
    from walnut_backend.adapters.doubao_tts import BookSpeechError, _configured_key

    key_file = tmp_path / "bad.key"
    key_file.write_text(bad, encoding="utf-8")
    monkeypatch.setenv("YAYA_BOOK_TTS_API_KEY_FILE", str(key_file))
    monkeypatch.delenv("YAYA_BOOK_TTS_API_KEY", raising=False)
    monkeypatch.setenv("YAYA_DOUBAO_VOICE_API_KEY", "valid-test-fallback")
    with pytest.raises(BookSpeechError, match="CONFIGURATION_INVALID"):
        _configured_key()


def test_configuration_fingerprint_detects_new_process_settings_without_disclosing_keys(
    monkeypatch, capsys
):
    from walnut_backend.voice_preflight import main

    for name in VOICE_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("YAYA_VOICE_MODE", "disabled")
    fingerprints = []
    for key in ["private-test-one", "private-test-one", "private-test-two"]:
        monkeypatch.setenv("YAYA_BOOK_TTS_API_KEY", key)
        assert main() == 0
        output = capsys.readouterr().out
        assert key not in output
        fingerprints.append(json.loads(output)["configuration_sha256"])
    assert fingerprints[0] == fingerprints[1] != fingerprints[2]
