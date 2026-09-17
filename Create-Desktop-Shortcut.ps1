# ==============================================================================
# Create Desktop Shortcut for SOL Media Studio (Silent Launcher)
# ==============================================================================
$ErrorActionPreference = "Stop"

$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "SOL Media Studio.lnk"
$targetScript = "D:\Workspace\media-api\Media-Studio-Silent.vbs"
$workingDir = "D:\Workspace\media-api"

$wscriptPath = Join-Path $env:SystemRoot "System32\wscript.exe"

$wshShell = New-Object -ComObject WScript.Shell
$shortcut = $wshShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $wscriptPath
$shortcut.Arguments = "`"$targetScript`""
$shortcut.WorkingDirectory = $workingDir
$shortcut.Description = "SOL Media Studio - Local Speech-To-Text & Media Pipeline"
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,115"
$shortcut.Save()

Write-Host "Created Desktop Shortcut: $shortcutPath" -ForegroundColor Green
