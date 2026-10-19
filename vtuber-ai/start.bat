@echo off
cd /d "%~dp0"
where node >nul 2>nul || (echo Necesitas instalar Node.js 18 o superior: https://nodejs.org & pause & exit /b 1)
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start "" http://localhost:3939"
node server.js
pause
