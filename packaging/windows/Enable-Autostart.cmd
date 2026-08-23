@echo off
setlocal DisableDelayedExpansion

set "SHIKI_AUTOSTART_EXE=%~dp0ShikiUpdatesBot.exe"
if exist "%SHIKI_AUTOSTART_EXE%" goto :exe_found

echo [ERROR] ShikiUpdatesBot.exe was not found beside this helper.
echo Executable: "%SHIKI_AUTOSTART_EXE%"
exit /b 1

:exe_found
if defined APPDATA goto :appdata_found

echo [ERROR] APPDATA is not defined; the per-user Startup folder is unavailable.
exit /b 1

:appdata_found
set "SHIKI_AUTOSTART_LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ShikiUpdatesBot.lnk"
echo Shortcut: "%SHIKI_AUTOSTART_LINK%"
echo Executable: "%SHIKI_AUTOSTART_EXE%"

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$ErrorActionPreference = 'Stop'; $linkDirectory = [System.IO.Path]::GetDirectoryName($env:SHIKI_AUTOSTART_LINK); [System.IO.Directory]::CreateDirectory($linkDirectory) | Out-Null; $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SHIKI_AUTOSTART_LINK); $shortcut.TargetPath = $env:SHIKI_AUTOSTART_EXE; $shortcut.WorkingDirectory = [System.IO.Path]::GetDirectoryName($env:SHIKI_AUTOSTART_EXE); $shortcut.Save()"
if errorlevel 1 goto :failed

echo [OK] Autostart shortcut enabled for the current user.
exit /b 0

:failed
echo [ERROR] Failed to create the autostart shortcut.
exit /b 1
