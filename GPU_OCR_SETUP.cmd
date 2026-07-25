@echo off
setlocal
cd /d "%~dp0"
title Sea Trade Planner RTX OCR Setup
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0gpu_ocr_setup.ps1"
