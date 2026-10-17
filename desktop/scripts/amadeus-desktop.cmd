@echo off
rem Lanzador de Amadeus Desktop para Windows.
rem Uso:  amadeus-desktop.cmd [--hidden] [--amadeus=toggle^|show^|hide^|chat^|quit^|basta^|vision_pausa^|clics]
rem Con una instancia ya abierta, la orden se le entrega a esa instancia y este proceso sale.
setlocal
set "DIR=%~dp0.."
set "ELECTRON=%DIR%\node_modules\electron\dist\electron.exe"
if not exist "%ELECTRON%" (
  echo amadeus-desktop: no encuentro Electron en "%ELECTRON%". Ejecuta "npm i" y "npx install-electron" en "%DIR%". 1>&2
  exit /b 1
)
start "" "%ELECTRON%" "%DIR%" %*
