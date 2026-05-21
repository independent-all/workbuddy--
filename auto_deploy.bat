@echo off
:: Set current directory to where the bat file is
cd /d "%~dp0"

echo [1/3] Running Python Pipeline...
set "VENV_PYTHON=C:\Users\yliua\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" src\main.py
) else (
    echo [WARN] Python not found, skipping data update...
)

echo [2/3] Pushing to GitHub...
if not exist ".git" (
    git init
    git branch -M main
)
git remote add origin https://github.com/independent-all/workbuddy--.git 2>nul
git remote set-url origin https://github.com/independent-all/workbuddy--.git 2>nul

git add .
git commit -m "Auto deploy"
git push -f -u origin main

echo [3/3] Push Completed Successfully!
echo You can now go to GitHub to enable Pages.
pause