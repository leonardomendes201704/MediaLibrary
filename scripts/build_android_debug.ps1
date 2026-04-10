Set-Location android-app
if (Test-Path .\\gradlew.bat) {
  .\\gradlew.bat assembleDebug
} else {
  if (Get-Command gradle -ErrorAction SilentlyContinue) {
    gradle assembleDebug
  } else {
    Write-Error "Gradle não encontrado. Gere o wrapper no Android Studio (Sync Project) ou instale Gradle no PATH."
    exit 1
  }
}
