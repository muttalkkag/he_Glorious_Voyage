@echo off
setlocal
cd /d "%~dp0"
title Sea Trade Planner Setup
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
