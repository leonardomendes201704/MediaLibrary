python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r backend\\requirements.txt
Copy-Item backend\\.env.example backend\\.env -ErrorAction SilentlyContinue

