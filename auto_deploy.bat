@echo off 
echo [1/3] Running Python Pipeline... 
"C:\Users\yliua\.workbuddy\binaries\python\envs\default\Scripts\python.exe" src\main.py 
echo [2/3] Git Push to GitHub... 
if not exist ".git" (git init & git branch -M main) 
git remote add origin https://github.com/independent-all/workbuddy--.git 2>nul 
git remote set-url origin https://github.com/independent-all/workbuddy--.git 2>nul 
git add . 
git commit -m "Auto deploy" 
git push -f -u origin main 
echo [3/3] Push successful. Done! 
pause 
