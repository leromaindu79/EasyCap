@echo off
cd /d "%~dp0"
py -3 -m pip install -r requirements.txt
if errorlevel 1 (
  echo L'installation des dependances a echoue.
  pause
  exit /b 1
)
py -3 EasyCap.py
if errorlevel 1 pause
