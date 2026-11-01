@echo off
setlocal
chcp 65001 >nul
if not exist "%~dp0runtime\node.exe" (
  echo Bundled Node runtime missing. Please extract the full release ZIP.
  exit /b 1
)
"%~dp0runtime\node.exe" "%~dp0src\cli.mjs" %*
set "AX_ASSISTANT_EXIT=%ERRORLEVEL%"
if "%~1"=="" pause
endlocal & exit /b %AX_ASSISTANT_EXIT%
