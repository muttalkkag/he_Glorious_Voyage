@echo off
setlocal
cd /d "%~dp0"
title Sea Trade Planner
if not exist ".venv\Scripts\python.exe" (
  echo Setup is required. Run SETUP.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "launcher.py"
if errorlevel 1 pause
