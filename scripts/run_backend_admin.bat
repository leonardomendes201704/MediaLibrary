@echo off
setlocal

REM Go to repository root (script is in /scripts)
cd /d "%~dp0.."
set ROOT_DIR=%CD%
set BACKEND_DIR=%ROOT_DIR%\backend

set PY_CMD=
set PY_MAJOR=
set PY_MINOR=

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
    echo Python nao encontrado no PATH.
    echo Instale Python 3.11, 3.12 ou 3.13 e tente novamente.
    exit /b 1
  )
  set PY_CMD=python
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

echo [2/6] Verificando virtualenv...
if exist ".venv\Scripts\python.exe" (
  set VENV_MAJOR=
  set VENV_MINOR=
  for /f "tokens=1,2" %%A in ('".venv\Scripts\python.exe" -c "import sys; print(sys.version_info.major, sys.version_info.minor)"') do (
    set VENV_MAJOR=%%A
    set VENV_MINOR=%%B
  )
  if not "%VENV_MAJOR%.%VENV_MINOR%"=="%PY_MAJOR%.%PY_MINOR%" (
    echo Virtualenv atual usa Python %VENV_MAJOR%.%VENV_MINOR%.
    echo Recriando .venv com Python %PY_MAJOR%.%PY_MINOR%...
    rmdir /s /q ".venv"
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo Criando virtualenv em .venv ...
  %PY_CMD% -m venv .venv
  if errorlevel 1 (
    echo Falha ao criar virtualenv.
    exit /b 1
  )
)

set VENV_PY=%ROOT_DIR%\.venv\Scripts\python.exe

echo [3/6] Verificando dependencias...
"%VENV_PY%" -c "import fastapi, uvicorn, sqlalchemy, watchdog, pydantic" >nul 2>&1
if errorlevel 1 (
  echo Instalando dependencias do backend...
  "%VENV_PY%" -m pip install --upgrade pip
  "%VENV_PY%" -m pip install -r "%BACKEND_DIR%\requirements.txt"
  if errorlevel 1 (
    echo Falha ao instalar dependencias.
    exit /b 1
  )
)

echo [4/6] Verificando arquivo .env...
if not exist "%BACKEND_DIR%\.env" (
  if exist "%BACKEND_DIR%\.env.example" (
    copy /y "%BACKEND_DIR%\.env.example" "%BACKEND_DIR%\.env" >nul
  )
)

echo [5/6] Abrindo admin...
echo API:   http://127.0.0.1:8000/docs
echo Admin: http://127.0.0.1:8000/admin
start "" powershell -NoProfile -Command "Start-Sleep -Seconds 5; Start-Process 'http://127.0.0.1:8000/admin'"

echo [6/6] Iniciando backend...
if not exist "%BACKEND_DIR%" (
  echo Pasta backend nao encontrada em: %BACKEND_DIR%
  exit /b 1
)
cd /d "%BACKEND_DIR%"
"%VENV_PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

endlocal
