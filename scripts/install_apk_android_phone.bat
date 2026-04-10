@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM Usage:
REM   install_apk_android_phone.bat
REM   install_apk_android_phone.bat --rebuild
REM   install_apk_android_phone.bat 192.168.0.5 5555
REM   install_apk_android_phone.bat 192.168.0.5 39001 37123 123456
REM   install_apk_android_phone.bat --rebuild 192.168.0.5 39001 37123 123456

set "DEVICE_IP=192.168.0.5"
set "DEVICE_PORT=5555"
set "PAIR_PORT="
set "PAIR_CODE="
set "FORCE_REBUILD=0"
if /I "%~1"=="--rebuild" (
  set "FORCE_REBUILD=1"
  shift
)
if /I "%~1"=="/rebuild" (
  set "FORCE_REBUILD=1"
  shift
)
if not "%~1"=="" set "DEVICE_IP=%~1"
if not "%~2"=="" set "DEVICE_PORT=%~2"
if not "%~3"=="" set "PAIR_PORT=%~3"
if not "%~4"=="" set "PAIR_CODE=%~4"
set "DEVICE=%DEVICE_IP%:%DEVICE_PORT%"

REM Go to repository root (script is in /scripts)
cd /d "%~dp0.."
set "ROOT_DIR=%CD%"
set "APK_PATH=%ROOT_DIR%\android-app\app\build\outputs\apk\debug\app-debug.apk"

set "ADB_EXE="
set "GRADLE_CMD="
set "GRADLE_VERSION=8.10.2"
set "GRADLE_HOME=%ROOT_DIR%\tools\gradle\gradle-%GRADLE_VERSION%"
set "GRADLE_BAT=%GRADLE_HOME%\bin\gradle.bat"

echo [1/8] Resolvendo ADB...
call :resolve_adb
if errorlevel 1 exit /b 1
echo ADB encontrado em: %ADB_EXE%

echo [2/8] Preparando ambiente Android ^(SDK/JDK^)...
call :prepare_android_env
if errorlevel 1 exit /b 1
if defined ANDROID_SDK_ROOT echo ANDROID_SDK_ROOT: %ANDROID_SDK_ROOT%
if defined JAVA_HOME echo JAVA_HOME: %JAVA_HOME%

echo [3/8] Verificando APK debug...
if "%FORCE_REBUILD%"=="1" (
  echo Parametro --rebuild detectado. Forcando novo build.
  call :build_apk
  if errorlevel 1 exit /b 1
) else (
  if not exist "%APK_PATH%" (
    call :build_apk
    if errorlevel 1 exit /b 1
  )
)

if not exist "%APK_PATH%" (
  echo APK ainda nao encontrado em:
  echo %APK_PATH%
  exit /b 1
)

echo [5/8] Subindo servidor ADB...
"%ADB_EXE%" start-server >nul 2>&1

echo [6/8] Conectando no dispositivo %DEVICE%...
call :connect_or_pair
if errorlevel 1 exit /b 1

echo [7/8] Instalando APK...
"%ADB_EXE%" -s %DEVICE% install -r "%APK_PATH%"
if errorlevel 1 (
  echo Falha ao instalar APK.
  exit /b 1
)

echo [8/8] Abrindo app...
"%ADB_EXE%" -s %DEVICE% shell monkey -p com.leonardo.medialibrary -c android.intent.category.LAUNCHER 1 >nul 2>&1

echo.
echo Sucesso. APK instalado e app aberto no dispositivo %DEVICE%.
exit /b 0

:build_apk
if "%FORCE_REBUILD%"=="1" (
  if exist "%APK_PATH%" del /q "%APK_PATH%" >nul 2>&1
)

echo APK nao encontrado. Iniciando build automatico...
call :resolve_gradle
if errorlevel 1 exit /b 1

echo [4/8] Gerando APK ^(assembleDebug^)...
pushd "%ROOT_DIR%\android-app"
call "!GRADLE_CMD!" assembleDebug
set "BUILD_RESULT=%ERRORLEVEL%"
popd
if not "!BUILD_RESULT!"=="0" (
  echo Primeira tentativa de build falhou. Tentando instalar componentes SDK...
  call :ensure_sdk_packages
  if errorlevel 1 (
    echo Falha ao preparar SDK automaticamente.
    if defined ANDROID_SDK_ROOT echo Verifique SDK em: !ANDROID_SDK_ROOT!
    exit /b 1
  )

  echo Repetindo build apos instalar SDK...
  pushd "%ROOT_DIR%\android-app"
  call "!GRADLE_CMD!" assembleDebug
  set "BUILD_RESULT=%ERRORLEVEL%"
  popd
  if not "!BUILD_RESULT!"=="0" (
    echo Falha ao gerar APK com Gradle apos segunda tentativa.
    if defined ANDROID_SDK_ROOT echo Verifique SDK em: !ANDROID_SDK_ROOT!
    exit /b 1
  )
)
exit /b 0

:connect_or_pair
"%ADB_EXE%" disconnect !DEVICE! >nul 2>&1
for /f "delims=" %%L in ('"%ADB_EXE%" connect !DEVICE! 2^>^&1') do echo %%L
call :check_device_ready
if not errorlevel 1 exit /b 0

echo Falha no connect direto. Tentando fluxo de ADB pair...
if not defined PAIR_PORT (
  set /p PAIR_PORT=Informe a porta de pareamento ^(pair port^) exibida no celular: 
)
if not defined PAIR_CODE (
  set /p PAIR_CODE=Informe o codigo de pareamento: 
)
if not defined PAIR_PORT (
  echo Pair port nao informado.
  exit /b 1
)
if not defined PAIR_CODE (
  echo Pair code nao informado.
  exit /b 1
)

set "PAIR_ENDPOINT=%DEVICE_IP%:%PAIR_PORT%"
echo Pareando com %PAIR_ENDPOINT%...
for /f "delims=" %%L in ('"%ADB_EXE%" pair %PAIR_ENDPOINT% %PAIR_CODE% 2^>^&1') do echo %%L

if "!DEVICE_PORT!"=="5555" (
  echo.
  echo Em Android recente, a porta de debug geralmente nao e 5555.
  set /p DEVICE_PORT=Informe a porta de debug ^(ip:porta mostrado em "Wireless debugging"^): 
  if not defined DEVICE_PORT (
    echo Porta de debug nao informada.
    exit /b 1
  )
  set "DEVICE=!DEVICE_IP!:!DEVICE_PORT!"
)

echo Conectando apos pair em !DEVICE!...
"%ADB_EXE%" disconnect !DEVICE! >nul 2>&1
for /f "delims=" %%L in ('"%ADB_EXE%" connect !DEVICE! 2^>^&1') do echo %%L
call :check_device_ready
if not errorlevel 1 exit /b 0

echo Dispositivo nao apareceu como "device" no ADB.
echo Rode manualmente para validar:
echo   adb pair %DEVICE_IP%:^<porta_pair^>
echo   adb connect %DEVICE_IP%:^<porta_debug^>
echo   adb devices
exit /b 1

:check_device_ready
set "DEVICE_READY="
for /f "tokens=1,2" %%A in ('"%ADB_EXE%" devices') do (
  if "%%A"=="!DEVICE!" (
    if "%%B"=="device" set "DEVICE_READY=1"
  )
)
if defined DEVICE_READY exit /b 0
exit /b 1

:resolve_adb
where adb >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%I in ('where adb ^| findstr /i "adb.exe"') do (
    set "ADB_EXE=%%I"
    goto :resolve_adb_done
  )
)

if exist "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" (
  set "ADB_EXE=%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"
  goto :resolve_adb_done
)

if defined ANDROID_SDK_ROOT (
  if exist "!ANDROID_SDK_ROOT!\platform-tools\adb.exe" (
    set "ADB_EXE=!ANDROID_SDK_ROOT!\platform-tools\adb.exe"
    goto :resolve_adb_done
  )
)

if defined ANDROID_HOME (
  if exist "!ANDROID_HOME!\platform-tools\adb.exe" (
    set "ADB_EXE=!ANDROID_HOME!\platform-tools\adb.exe"
    goto :resolve_adb_done
  )
)

where winget >nul 2>&1
if not errorlevel 1 (
  echo ADB nao encontrado. Tentando instalar Platform Tools via winget...
  winget install --id Google.PlatformTools --exact --accept-source-agreements --accept-package-agreements --silent
  if exist "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" (
    set "ADB_EXE=%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"
    goto :resolve_adb_done
  )
)

echo Nao foi possivel localizar o ADB automaticamente.
echo Instale Android Platform Tools e tente novamente.
exit /b 1

:resolve_adb_done
exit /b 0

:prepare_android_env
set "USER_SDK_ROOT=%LOCALAPPDATA%\Android\Sdk"
set "FORCE_USER_SDK="

if not defined ANDROID_SDK_ROOT set "FORCE_USER_SDK=1"

if defined ANDROID_SDK_ROOT (
  echo(!ANDROID_SDK_ROOT!| findstr /I /C:"Program Files" >nul
  if not errorlevel 1 set "FORCE_USER_SDK=1"
  call :check_sdk_writable "!ANDROID_SDK_ROOT!"
  if errorlevel 1 set "FORCE_USER_SDK=1"
)

if defined FORCE_USER_SDK (
  set "ANDROID_SDK_ROOT=%USER_SDK_ROOT%"
  if not exist "!ANDROID_SDK_ROOT!" mkdir "!ANDROID_SDK_ROOT!"
)

set "ANDROID_HOME=!ANDROID_SDK_ROOT!"
call :write_local_properties

if not defined JAVA_HOME (
  if exist "%ProgramFiles%\Android\Android Studio\jbr" (
    set "JAVA_HOME=%ProgramFiles%\Android\Android Studio\jbr"
  )
)

if not defined JAVA_HOME (
  if exist "%ProgramFiles%\Android\Android Studio\jre" (
    set "JAVA_HOME=%ProgramFiles%\Android\Android Studio\jre"
  )
)

if defined JAVA_HOME set "PATH=%JAVA_HOME%\bin;%PATH%"

where java >nul 2>&1
if errorlevel 1 (
  echo Java nao encontrado. Instale JDK 17+ ou Android Studio.
  exit /b 1
)

exit /b 0

:check_sdk_writable
set "SDK_PATH=%~1"
if "%SDK_PATH%"=="" exit /b 1
if not exist "%SDK_PATH%" exit /b 1

set "TEST_FILE=%SDK_PATH%\.ml_write_test_%RANDOM%_%RANDOM%.tmp"
echo test > "%TEST_FILE%" 2>nul
if errorlevel 1 exit /b 1
del /q "%TEST_FILE%" >nul 2>&1
exit /b 0

:write_local_properties
set "LOCAL_PROPERTIES=%ROOT_DIR%\android-app\local.properties"
set "SDK_ESCAPED=!ANDROID_SDK_ROOT:\=\\!"
> "%LOCAL_PROPERTIES%" echo sdk.dir=!SDK_ESCAPED!
exit /b 0

:ensure_sdk_packages
set "SDKMANAGER="
call :resolve_sdkmanager
if not defined SDKMANAGER (
  echo sdkmanager.bat nao encontrado.
  exit /b 1
)
echo Usando sdkmanager: !SDKMANAGER!
echo y | "!SDKMANAGER!" --sdk_root="!ANDROID_SDK_ROOT!" "platform-tools" "platforms;android-35" "build-tools;35.0.0"
if errorlevel 1 exit /b 1
exit /b 0

:resolve_sdkmanager
where sdkmanager.bat >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%S in ('where sdkmanager.bat') do (
    set "SDKMANAGER=%%S"
    goto :resolve_sdkmanager_done
  )
)

if exist "!ANDROID_SDK_ROOT!\cmdline-tools\latest\bin\sdkmanager.bat" (
  set "SDKMANAGER=!ANDROID_SDK_ROOT!\cmdline-tools\latest\bin\sdkmanager.bat"
  goto :resolve_sdkmanager_done
)
if exist "!ANDROID_SDK_ROOT!\cmdline-tools\bin\sdkmanager.bat" (
  set "SDKMANAGER=!ANDROID_SDK_ROOT!\cmdline-tools\bin\sdkmanager.bat"
  goto :resolve_sdkmanager_done
)

if exist "%LOCALAPPDATA%\Android\Sdk\cmdline-tools\latest\bin\sdkmanager.bat" (
  set "SDKMANAGER=%LOCALAPPDATA%\Android\Sdk\cmdline-tools\latest\bin\sdkmanager.bat"
  goto :resolve_sdkmanager_done
)
if exist "%LOCALAPPDATA%\Android\Sdk\cmdline-tools\bin\sdkmanager.bat" (
  set "SDKMANAGER=%LOCALAPPDATA%\Android\Sdk\cmdline-tools\bin\sdkmanager.bat"
  goto :resolve_sdkmanager_done
)

if exist "%ProgramFiles(x86)%\Android\android-sdk\cmdline-tools\latest\bin\sdkmanager.bat" (
  set "SDKMANAGER=%ProgramFiles(x86)%\Android\android-sdk\cmdline-tools\latest\bin\sdkmanager.bat"
  goto :resolve_sdkmanager_done
)
if exist "%ProgramFiles(x86)%\Android\android-sdk\cmdline-tools\bin\sdkmanager.bat" (
  set "SDKMANAGER=%ProgramFiles(x86)%\Android\android-sdk\cmdline-tools\bin\sdkmanager.bat"
  goto :resolve_sdkmanager_done
)

:resolve_sdkmanager_done
exit /b 0

:resolve_gradle
if exist "%ROOT_DIR%\android-app\gradlew.bat" (
  set "GRADLE_CMD=%ROOT_DIR%\android-app\gradlew.bat"
  goto :resolve_gradle_done
)

where gradle >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%G in ('where gradle ^| findstr /i "gradle.bat"') do (
    set "GRADLE_CMD=%%G"
    goto :resolve_gradle_done
  )
  set "GRADLE_CMD=gradle"
  goto :resolve_gradle_done
)

if not exist "%GRADLE_BAT%" (
  echo Gradle nao encontrado. Baixando Gradle %GRADLE_VERSION%...
  call :bootstrap_gradle
  if errorlevel 1 exit /b 1
)

if not exist "%GRADLE_BAT%" (
  echo Falha ao preparar Gradle automatico.
  exit /b 1
)
set "GRADLE_CMD=%GRADLE_BAT%"

:resolve_gradle_done
echo Gradle: %GRADLE_CMD%
exit /b 0

:bootstrap_gradle
if not exist "%ROOT_DIR%\tools\gradle" mkdir "%ROOT_DIR%\tools\gradle"
set "GRADLE_ZIP=%TEMP%\gradle-%GRADLE_VERSION%-bin.zip"
set "GRADLE_URL=https://services.gradle.org/distributions/gradle-%GRADLE_VERSION%-bin.zip"

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri '%GRADLE_URL%' -OutFile '%GRADLE_ZIP%' -UseBasicParsing } catch { exit 1 }"
if errorlevel 1 (
  echo Falha ao baixar Gradle.
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Expand-Archive -Path '%GRADLE_ZIP%' -DestinationPath '%ROOT_DIR%\tools\gradle' -Force } catch { exit 1 }"
if errorlevel 1 (
  echo Falha ao extrair Gradle.
  exit /b 1
)

del /q "%GRADLE_ZIP%" >nul 2>&1
exit /b 0
