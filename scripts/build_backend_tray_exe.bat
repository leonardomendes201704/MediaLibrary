@echo off
setlocal

cd /d "%~dp0.."
set ROOT_DIR=%CD%
set BACKEND_DIR=%ROOT_DIR%\backend

set PY_CMD=
set PY_MAJOR=
set PY_MINOR=
set TRAY_VENV=%ROOT_DIR%\.venv_tray

echo [1/6] Selecionando Python...
where py >nul 2>&1
if not errorlevel 1 (
  py -3.13 -c "import sys" >nul 2>&1
  if not errorlevel 1 set PY_CMD=py -3.13
  if not defined PY_CMD (
    py -3.12 -c "import sys" >nul 2>&1
    if not errorlevel 1 set PY_CMD=py -3.12
  )
  if not defined PY_CMD (
    py -3.11 -c "import sys" >nul 2>&1
    if not errorlevel 1 set PY_CMD=py -3.11
  )
)

if not defined PY_CMD (
  where python >nul 2>&1
  if errorlevel 1 (
    if exist "%TRAY_VENV%\Scripts\python.exe" (
      set PY_CMD=%TRAY_VENV%\Scripts\python.exe
    ) else (
      echo Python nao encontrado no PATH.
      echo Instale Python 3.11, 3.12 ou 3.13 e tente novamente.
      exit /b 1
    )
  ) else (
    set PY_CMD=python
  )
)

for /f "tokens=1,2" %%A in ('%PY_CMD% -c "import sys; print(sys.version_info.major, sys.version_info.minor)"') do (
  set PY_MAJOR=%%A
  set PY_MINOR=%%B
)

if not "%PY_MAJOR%"=="3" (
  echo Versao de Python nao suportada: %PY_MAJOR%.%PY_MINOR%
  echo Use Python 3.11, 3.12 ou 3.13.
  exit /b 1
)

if %PY_MINOR% LSS 11 (
  echo Python %PY_MAJOR%.%PY_MINOR% nao suportado.
  echo Use Python 3.11, 3.12 ou 3.13.
  exit /b 1
)

if %PY_MINOR% GEQ 14 (
  echo Python %PY_MAJOR%.%PY_MINOR% detectado.
  echo Este projeto usa pydantic-core sem wheel estavel para 3.14 neste ambiente.
  echo Instale Python 3.13 e rode novamente.
  exit /b 1
)

echo Python selecionado: %PY_CMD%  ^(%PY_MAJOR%.%PY_MINOR%^)

echo [2/6] Preparando virtualenv...
if not exist "%TRAY_VENV%\Scripts\python.exe" (
  %PY_CMD% -m venv "%TRAY_VENV%"
  if errorlevel 1 (
    echo Falha ao criar virtualenv.
    exit /b 1
  )
)

set VENV_PY=%TRAY_VENV%\Scripts\python.exe
"%VENV_PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo Virtualenv do tray esta inconsistente. Recriando...
  rmdir /s /q "%TRAY_VENV%"
  %PY_CMD% -m venv "%TRAY_VENV%"
  if errorlevel 1 (
    echo Falha ao recriar virtualenv.
    exit /b 1
  )
)

echo [3/6] Instalando dependencias do tray/exe...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r "%BACKEND_DIR%\requirements-tray.txt"
if errorlevel 1 (
  echo Falha ao instalar dependencias.
  exit /b 1
)

echo [4/6] Limpando builds anteriores...
set EXE_PATH=%ROOT_DIR%\dist\MediaLibraryBackendTray.exe

rem Evita WinError 5 quando o exe anterior esta em execucao.
taskkill /F /IM MediaLibraryBackendTray.exe >nul 2>&1
if exist "%EXE_PATH%" (
  del /q "%EXE_PATH%" >nul 2>&1
)
if exist "%EXE_PATH%" (
  echo Aguardando liberacao do executavel antigo...
  timeout /t 2 /nobreak >nul
  del /q "%EXE_PATH%" >nul 2>&1
)
if exist "%EXE_PATH%" (
  echo Executavel em uso: %EXE_PATH%
  echo Feche o tray e tente novamente.
  exit /b 1
)

if exist "%ROOT_DIR%\build" rmdir /s /q "%ROOT_DIR%\build"

echo [5/6] Gerando executavel...
"%VENV_PY%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --icon "%BACKEND_DIR%\media_library_tray.ico" ^
  --name MediaLibraryBackendTray ^
  --paths "%BACKEND_DIR%" ^
  --collect-all uvicorn ^
  --collect-all fastapi ^
  --collect-all starlette ^
  --collect-all anyio ^
  --collect-all jinja2 ^
  --collect-all watchdog ^
  --add-data "%BACKEND_DIR%\media_library_tray.ico;." ^
  --add-data "%BACKEND_DIR%\app\web\templates;app\web\templates" ^
  --add-data "%BACKEND_DIR%\app\web\static;app\web\static" ^
  "%BACKEND_DIR%\tray_app.py"
if errorlevel 1 (
  echo Falha ao gerar executavel.
  exit /b 1
)

if not exist "%EXE_PATH%" (
  echo Executavel nao encontrado em: %EXE_PATH%
  exit /b 1
)

echo [6/6] Concluido.
echo Executavel: %EXE_PATH%
echo.
echo Para iniciar:
echo   "%EXE_PATH%"

endlocal
