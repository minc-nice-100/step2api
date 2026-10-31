@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PORT=%~1
if "%PORT%"=="" set PORT=8790
set KEY=%~2
if "%KEY%"=="" set KEY=sf-%RANDOM%%RANDOM%%RANDOM%
python -V >nul 2>&1
if errorlevel 1 (
  echo 找不到可用的 Python，请先安装 Python 3.9+ 并加入 PATH。
  pause
  exit /b 1
)
echo.
echo   局域网模式：监听 0.0.0.0:%PORT%
echo   API Key: %KEY%
echo   其他设备 Base URL: http://^<本机IP^>:%PORT%/v1
echo.
python step2api.py --host 0.0.0.0 --port %PORT% --api-key %KEY% --data-dir accounts
if errorlevel 1 pause
endlocal
