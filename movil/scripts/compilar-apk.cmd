@echo off
REM Compila, firma e instala el APK nativo de Amadeus en el celular Android conectado
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0compilar-apk.ps1"
