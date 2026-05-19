@echo off
cd /d "%~dp0"
echo Cleaning up previous instances to prevent RAM crashes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object {($_.Name -eq 'node.exe' -and $_.CommandLine -match 'playwright') -or ($_.Name -match '^python' -and ($_.CommandLine -match 'Trading tool' -or $_.CommandLine -match 'scheduler.py' -or $_.CommandLine -match 'ibkr_live.py' -or $_.CommandLine -match 'qwen_analyzer.py' -or $_.CommandLine -match 'scraper.py' -or $_.CommandLine -match 'server.py' -or $_.CommandLine -match 'app.py'))} | Stop-Process -Force -ErrorAction SilentlyContinue"
start "" pythonw app.py
