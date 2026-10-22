@echo off
setlocal EnableDelayedExpansion

REM Go to repository root (script is in /scripts)
cd /d "%~dp0.."
set ROOT_DIR=%CD%
set BACKEND_DIR=%ROOT_DIR%\backend

if not defined BACKEND_PORT set BACKEND_PORT=8077
if not defined BACKEND_HOST set BACKEND_HOST=0.0.0.0

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
set "VENV_PY=%ROOT_DIR%\.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
  set VENV_MAJOR=
  set VENV_MINOR=
  for /f "tokens=1,2" %%A in ('"%VENV_PY%" -c "import sys; print(sys.version_info.major, sys.version_info.minor)"') do (
    set VENV_MAJOR=%%A
    set VENV_MINOR=%%B
  )
  if not defined VENV_MAJOR (
    echo Virtualenv parece corrompido ^(nao foi possivel ler a versao do Python^).
    echo Recriando .venv ...
    call :stop_backend_on_port
    rmdir /s /q "%ROOT_DIR%\.venv" 2>nul
  ) else if not "!VENV_MAJOR!.!VENV_MINOR!"=="%PY_MAJOR%.%PY_MINOR%" (
    echo Virtualenv atual usa Python !VENV_MAJOR!.!VENV_MINOR!.
    echo Recriando .venv com Python %PY_MAJOR%.%PY_MINOR%...
    call :stop_backend_on_port
    rmdir /s /q "%ROOT_DIR%\.venv" 2>nul
  )
)

if not exist "%VENV_PY%" (
  echo Criando virtualenv em .venv ...
  %PY_CMD% -m venv "%ROOT_DIR%\.venv"
  if errorlevel 1 (
    echo Falha ao criar virtualenv.
    exit /b 1
  )
)

goto :after_venv_setup

:stop_backend_on_port
echo Encerrando instancias antigas do Media Library na porta %BACKEND_PORT%...
for /f "tokens=5" %%P in ('netstat -aon ^| findstr ":%BACKEND_PORT%" ^| findstr "LISTENING"') do (
  taskkill /PID %%P /F >nul 2>&1
)
timeout /t 2 /nobreak >nul
exit /b 0

:after_venv_setup

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
echo API:   http://127.0.0.1:%BACKEND_PORT%/docs
echo Admin: http://127.0.0.1:%BACKEND_PORT%/admin
start "" powershell -NoProfile -Command "Start-Sleep -Seconds 5; Start-Process 'http://127.0.0.1:%BACKEND_PORT%/admin'"

echo [6/6] Iniciando backend na porta %BACKEND_PORT%...
if not exist "%BACKEND_DIR%" (
  echo Pasta backend nao encontrada em: %BACKEND_DIR%
  exit /b 1
)
call :stop_backend_on_port
cd /d "%BACKEND_DIR%"
"%VENV_PY%" -m uvicorn app.main:app --host %BACKEND_HOST% --port %BACKEND_PORT% --reload

endlocal
