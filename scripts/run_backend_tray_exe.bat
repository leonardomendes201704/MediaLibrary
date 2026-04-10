@echo off
setlocal

cd /d "%~dp0.."
set EXE_PATH=%CD%\dist\MediaLibraryBackendTray.exe

if not exist "%EXE_PATH%" (
  echo Executavel nao encontrado em:
  echo   %EXE_PATH%
  echo Rode antes: scripts\build_backend_tray_exe.bat
  exit /b 1
)

start "" "%EXE_PATH%"
echo Tray iniciado: %EXE_PATH%

endlocal
