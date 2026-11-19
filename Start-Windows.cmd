@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0" || exit /b 1
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYLAUNCHER_ALLOW_INSTALL="
set "PYLAUNCHER_ALWAYS_INSTALL="
set "PYTHONDONTWRITEBYTECODE=1"
if exist ".venv\Scripts\python.exe" (
  call :probe ".venv\Scripts\python.exe" && goto launch
)
if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" (
  call :probe "%VIRTUAL_ENV%\Scripts\python.exe" && goto launch
)
where py.exe >nul 2>nul && (
  for %%V in (3.12 3.11 3.10) do (
    call :probe py.exe -%%V && goto launch
  )
)
for /f "delims=" %%P in ('where python.exe 2^>nul ^| findstr /i /v WindowsApps') do (
  call :probe "%%P" && goto launch
)
echo Python 3.10〜3.12 と openai-whisper が必要です。
echo 初回のみ、このフォルダで py -3.12 setup_models.py を実行してください。
pause
exit /b 1

:probe
set "runner=%~1"
set "tag=%~2"
"%runner%" %tag% -c "import sys; assert (3,10) <= sys.version_info[:2] <= (3,12); from whisper import load_model" 2>nul
exit /b %errorlevel%

:launch
"%runner%" %tag% app.py %*
if errorlevel 1 pause
exit /b
