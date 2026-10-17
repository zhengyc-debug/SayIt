# 用 Windows 自带语音合成生成一段 16k 单声道英文测试音频
# 用法: powershell -ExecutionPolicy Bypass -File tools\make_test_audio.ps1
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$out = Join-Path $PSScriptRoot "..\tmp\hello.wav"
New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null

$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, `
      [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, `
      [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($out, $fmt)
$s.Speak("Hello! I just moved to a new city for work, and I am trying to make some friends here. What do you usually do on weekends?")
$s.Dispose()
Write-Host "wrote $out"
