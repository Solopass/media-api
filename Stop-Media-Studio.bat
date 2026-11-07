@echo off
title Stop SOL Media Studio
echo ========================================================
echo               STOPPING SOL MEDIA STUDIO
echo ========================================================
echo.
echo [1/2] Sending clean shutdown command to media-api (127.0.0.1:8080)...
powershell -NoProfile -Command "try { $res = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/v1/shutdown' -Method Post -TimeoutSec 3; Write-Host 'Server response: ' $res.message } catch { Write-Host 'Server is already stopped or idle.' }"

echo [2/2] Releasing WSL background keepalive...
wsl.exe -d Ubuntu-24.04 -- pkill -f "^sleep infinity$" 2>nul

echo.
echo ========================================================
echo Media Studio has been cleanly stopped.
echo All host CPU threads and RAM have been released.
echo ========================================================
timeout /t 3 >nul
