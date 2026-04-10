param(
  [string]$DeviceIp = "192.168.0.50",
  [int]$DevicePort = 5555
)

$apk = "android-app\\app\\build\\outputs\\apk\\debug\\app-debug.apk"
if (-not (Test-Path $apk)) {
  Write-Error "APK não encontrado em $apk. Rode scripts/build_android_debug.ps1 primeiro."
  exit 1
}

adb connect "$DeviceIp`:$DevicePort"
adb install -r $apk
adb shell monkey -p com.leonardo.medialibrary -c android.intent.category.LAUNCHER 1

