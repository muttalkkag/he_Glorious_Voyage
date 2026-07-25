@echo off
setlocal
cd /d "%~dp0"
title Sea Trade Planner Diagnostics
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnose.ps1"
