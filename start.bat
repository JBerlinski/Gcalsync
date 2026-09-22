@echo off
rem Uruchamia GUI gcalsync w przegladarce (http://127.0.0.1:8765). Zamkniecie: Ctrl+C lub zamkniecie okna.
cd /d "%~dp0"
uv run gcalsync ui
if errorlevel 1 pause
