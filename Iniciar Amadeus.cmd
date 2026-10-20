@echo off
rem ============================================================================
rem  Iniciar Amadeus (Windows): doble clic y arranca las tres piezas.
rem   1) VTuber AI Studio (avatar 3D, :3939)   -> ventana minimizada
rem   2) R.A.M.S. (el cerebro, HUD en :8765)    -> ventana de consola (puedes escribirle ahi)
rem   3) Amadeus Desktop (bandeja + overlay)    -> ventana minimizada
rem  Si alguna ya esta corriendo, no la duplica. Para apagar: escribe /salir en la
rem  consola de R.A.M.S. y cierra las otras dos ventanas (o "Salir" en la bandeja).
rem ============================================================================
chcp 65001 >nul
title Amadeus - lanzador
cd /d "%~dp0"

set "RAIZ=%~dp0"
set "VTAI=%~dp0..\vtuber-ai"

where node >nul 2>nul || (echo [!] Falta Node.js: instalalo desde https://nodejs.org & pause & exit /b 1)
if not exist "%RAIZ%.venv\Scripts\python.exe" (echo [!] Falta el entorno .venv de R.A.M.S. Mira README.md, seccion Instalacion. & pause & exit /b 1)

rem --- 1) VTuber AI Studio --------------------------------------------------------
netstat -ano | findstr "127.0.0.1:3939" | findstr "LISTENING" >nul
if errorlevel 1 (
    if exist "%VTAI%\server.js" (
        echo Iniciando VTuber AI Studio...
        start "VTuber AI Studio (:3939)" /min cmd /c "cd /d "%VTAI%" && node server.js"
    ) else (
        echo [!] No encuentro %VTAI%\server.js: Amadeus funcionara sin avatar 3D.
    )
) else (
    echo VTuber AI Studio ya estaba corriendo.
)

rem --- 2) R.A.M.S. --------------------------------------------------------------
netstat -ano | findstr "127.0.0.1:8765" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo Iniciando R.A.M.S. ^(Amadeus^)...
    start "Amadeus - R.A.M.S." cmd /k "cd /d "%RAIZ%" && set "PYTHONIOENCODING=utf-8" && .venv\Scripts\python.exe main.py"
) else (
    echo R.A.M.S. ya estaba corriendo.
)

rem --- 3) Amadeus Desktop (una sola instancia: si ya esta abierta, solo la llama) ---
echo Iniciando Amadeus Desktop...
start "Amadeus Desktop" /min cmd /c "cd /d "%RAIZ%desktop" && npm.cmd start"

echo.
echo Listo. Amadeus aparecera en unos segundos (icono en la bandeja junto al reloj).
timeout /t 5 >nul
exit /b 0
