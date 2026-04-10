param(
  [string]$HostName = "0.0.0.0",
  [int]$Port = 8000
)

python -m uvicorn app.main:app --host $HostName --port $Port --reload --app-dir .

