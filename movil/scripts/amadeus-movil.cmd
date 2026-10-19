@echo off
REM Lanzador de Amadeus Movil Android (R.A.M.S. BIO-CORE + VTuber AI Studio)
REM Si adb esta disponible, configura automaticamente el tunel USB adb reverse tcp:8767 tcp:8767.
setlocal
cd /d "%~dp0..\.."

netstat -ano | findstr /R /C:":3939 .*LISTENING" >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    if exist "vtuber-ai\server.js" (
        start "VTuber AI Studio (:3939)" /min node vtuber-ai\server.js
    )
)

where adb >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    adb reverse tcp:8767 tcp:8767 >nul 2>nul
) else if exist "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" (
    "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" reverse tcp:8767 tcp:8767 >nul 2>nul
)

".\.venv\Scripts\python.exe" -m movil.servidor_movil %*
