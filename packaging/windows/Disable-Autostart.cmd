@echo off
setlocal DisableDelayedExpansion

if defined APPDATA goto :appdata_found

echo [ERROR] APPDATA is not defined; the per-user Startup folder is unavailable.
exit /b 1

:appdata_found
set "SHIKI_AUTOSTART_LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ShikiUpdatesBot.lnk"
echo Shortcut: "%SHIKI_AUTOSTART_LINK%"
if not exist "%SHIKI_AUTOSTART_LINK%" goto :already_absent

del /f /q "%SHIKI_AUTOSTART_LINK%"
if errorlevel 1 goto :failed

echo [OK] Autostart shortcut disabled for the current user.
exit /b 0

:already_absent
echo [OK] Autostart shortcut is already absent.
exit /b 0

:failed
echo [ERROR] Failed to remove the autostart shortcut.
exit /b 1
