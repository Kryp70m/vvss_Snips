@echo off
cd /d %~dp0\backend
if not exist .venv (
  py -3.12 -m venv .venv
)
call .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
