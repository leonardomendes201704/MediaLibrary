.\\.venv\\Scripts\\Activate.ps1
Set-Location backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

