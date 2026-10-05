@echo off
REM Double-click this file to start the Data Collector.
cd /d "%~dp0"
if not exist .venv (
  echo First run: installing... this takes a minute.
  python -m venv .venv
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
start "" http://localhost:5000
.venv\Scripts\python app.py
pause
