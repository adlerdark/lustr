@echo off
setlocal

:: CHECK ADMIN
NET SESSION >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

:: SETUP FOLDER
set "INSTALL_DIR=C:\Scripts"
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

:: GENERATE HANDLER
set "HANDLER_PATH=%INSTALL_DIR%\vlc-handler.bat"
(
    echo @echo off
    echo set "url=%%~1"
    echo.
    echo :: --- VLC PATH DETECTION ---
    echo set "vlcPath=C:\Program Files\VideoLAN\VLC\vlc.exe"
    echo if not exist "%%vlcPath%%" set "vlcPath=C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"
    echo.
    echo :: --- EXECUTION ---
    echo powershell -NoProfile -Command "$u='%%url%%'; $u=$u.Trim(); $c=$u -replace '^vlc:///','' -replace '^vlc://','\\'; $c=$c -replace '/','\'; $c=[Uri]::UnescapeDataString($c); $args='--one-instance \"' + $c + '\"'; Start-Process -FilePath '%%vlcPath%%' -ArgumentList $args"
) > "%HANDLER_PATH%"

:: REGISTRY
reg add "HKCR\vlc" /ve /d "URL:VLC Protocol" /f >nul
reg add "HKCR\vlc" /v "URL Protocol" /d "" /f >nul
reg add "HKCR\vlc\shell\open\command" /ve /d "\"%HANDLER_PATH%\" \"%%1\"" /f >nul

echo INSTALLATION COMPLETE.
pause