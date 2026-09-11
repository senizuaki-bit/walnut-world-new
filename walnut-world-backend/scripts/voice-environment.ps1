# Resolve file locations only. Credentials never enter script output or state.json.
function Initialize-WalnutVoiceEnvironment {
    param(
        [Parameter(Mandatory)][string]$AgentRoot,
        [string]$SecretsDirectory = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.walnut-secrets')
    )
    if ([string]::IsNullOrWhiteSpace($env:YAYA_BOOK_TTS_API_KEY) -and
        [string]::IsNullOrWhiteSpace($env:YAYA_BOOK_TTS_API_KEY_FILE)) {
        $bookFile = Join-Path $SecretsDirectory 'book-tts.key'
        if (Test-Path -LiteralPath $bookFile -PathType Leaf) {
            $env:YAYA_BOOK_TTS_API_KEY_FILE = [IO.Path]::GetFullPath($bookFile)
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:YAYA_DOUBAO_VOICE_API_KEY) -and
        [string]::IsNullOrWhiteSpace($env:YAYA_DOUBAO_VOICE_API_KEY_FILE)) {
        foreach ($voiceFile in @(
            (Join-Path $SecretsDirectory 'doubao-voice.key'),
            (Join-Path $AgentRoot 'doubao-voice-api.key')
        )) {
            if (Test-Path -LiteralPath $voiceFile -PathType Leaf) {
                $env:YAYA_DOUBAO_VOICE_API_KEY_FILE = [IO.Path]::GetFullPath($voiceFile)
                break
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:YAYA_VOICE_MODE)) {
        $env:YAYA_VOICE_MODE = if (
            -not [string]::IsNullOrWhiteSpace($env:YAYA_DOUBAO_VOICE_API_KEY) -or
            -not [string]::IsNullOrWhiteSpace($env:YAYA_DOUBAO_VOICE_API_KEY_FILE)
        ) { 'doubao' } else { 'disabled' }
    }
    # Child processes use the backend directory, not the caller's working directory.
    foreach ($fileVariable in @('YAYA_BOOK_TTS_API_KEY_FILE', 'YAYA_DOUBAO_VOICE_API_KEY_FILE')) {
        $fileValue = [Environment]::GetEnvironmentVariable($fileVariable, 'Process')
        if (-not [string]::IsNullOrWhiteSpace($fileValue)) {
            $absoluteFile = if ([IO.Path]::IsPathRooted($fileValue)) {
                # Preserve missing drives for the redacted Python validation error.
                [IO.Path]::GetFullPath($fileValue)
            } else {
                $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($fileValue)
            }
            [Environment]::SetEnvironmentVariable($fileVariable, $absoluteFile, 'Process')
        }
    }
}
