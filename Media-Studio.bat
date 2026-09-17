@echo off
title SOL Media Studio
echo ========================================================
echo               SOL MEDIA STUDIO
echo ========================================================
echo.
echo Media Studio is running at http://127.0.0.1:8080/
echo.
echo * Opening browser to Media Studio...
echo * Keeping workstation media worker awake while this window is open.
echo * Close this window anytime to let WSL sleep and release RAM.
echo.
start http://127.0.0.1:8080/
wsl.exe -d Ubuntu-24.04 -- sleep infinity
