@echo off
echo Starting RuleForge Backend Setup...

cd %~dp0

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

echo Activating virtual environment...
call venv\Scripts\activate

echo Installing dependencies...
pip install -r requirements.txt

if not exist data\aml.db (
    echo Initializing DuckDB database...
    python data\setup_duckdb.py
)

echo Starting Flask Backend...
python flask_backend.py
pause
