@echo off
chcp 65001 >nul
title push to gitee
cd /d "%~dp0"
echo ============================================
echo   push this repo to Gitee
echo ============================================

for /f "delims=" %%i in ('git config --get user.name') do set GN=%%i
if "%GN%"=="" (
  git config --global user.name "rainprayer"
  git config --global user.email "rainprayer@users.noreply.gitee.com"
)

if not exist ".git" (
  git init
  git add .
  git commit -m "init"
)

for /f "delims=" %%i in ('git remote get-url origin 2^>nul') do set REMOTE=%%i
if "%REMOTE%"=="" (
  set /p REPO=Repo URL (https://gitee.com/yourname/REPO.git^): 
  git remote add origin %REPO%
)

git add .
git commit -m "update" 2>nul
git branch -M master
git push -u origin master
pause
