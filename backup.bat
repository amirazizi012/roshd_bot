@echo off
cd /d %~dp0
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set D=%%i
robocopy data backups\%D% /E /NFL /NDL /NJH /NJS >nul
echo Backup done: backups\%D%
