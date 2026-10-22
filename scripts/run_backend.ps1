.\\.venv\\Scripts\\Activate.ps1
Set-Location backend
$port = if ($env:BACKEND_PORT) { [int]$env:BACKEND_PORT } else { 8077 }
$hostName = if ($env:BACKEND_HOST) { $env:BACKEND_HOST } else { "0.0.0.0" }
python -m uvicorn app.main:app --host $hostName --port $port --reload
