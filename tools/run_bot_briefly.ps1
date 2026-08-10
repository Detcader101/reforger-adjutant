# Run the bot for a fixed number of seconds, then stop it and its children.
#
# WSL-side `timeout` kills only the launcher, leaving the Windows python
# process orphaned — holding the sqlite lock and a live gateway session.
# This keeps the whole lifecycle on the Windows side so nothing survives.
#
#   powershell -ExecutionPolicy Bypass -File tools\run_bot_briefly.ps1 [-Seconds 30] [-LogPath run.log]

param(
    [int]$Seconds = 30,
    [string]$LogPath = "run.log",
    [string]$Module = "adjutant"
)

$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$proc = Start-Process -FilePath $python -ArgumentList "-m", $Module `
    -RedirectStandardOutput $LogPath -RedirectStandardError "$LogPath.err" `
    -NoNewWindow -PassThru

Write-Host "started pid=$($proc.Id), running for ${Seconds}s"
$exited = $proc.WaitForExit($Seconds * 1000)

if (-not $exited) {
    # /T so any child processes go with it.
    & taskkill /F /T /PID $proc.Id | Out-Null
    Write-Host "stopped after ${Seconds}s"
} else {
    Write-Host "exited on its own with code $($proc.ExitCode)"
}

if ((Test-Path "$LogPath.err") -and (Get-Item "$LogPath.err").Length -gt 0) {
    Get-Content "$LogPath.err" | Add-Content $LogPath
}
Remove-Item "$LogPath.err" -ErrorAction SilentlyContinue
