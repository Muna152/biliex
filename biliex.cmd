@echo off
REM biliex launcher - finds a usable Python and runs the biliex package.
REM
REM NOTE: keep this file pure ASCII. cmd.exe decodes .cmd files using the OEM
REM codepage (GBK on zh-CN), so non-ASCII text here gets mangled and breaks parsing.
REM
REM Resolution order (first hit wins):
REM   1. BILIEX_PYTHON in the current process environment
REM   2. BILIEX_PYTHON persisted in HKCU\Environment
REM      (needed because "setx" only affects NEW processes; without this, running
REM       the launcher in the same window right after setx reports "not found")
REM   3. the "py" launcher on PATH
REM   4. "python" on PATH
REM   5. common per-user install locations
setlocal
set "HERE=%~dp0"
set "PY="

REM --- 1) current process environment ---
if defined BILIEX_PYTHON set "PY=%BILIEX_PYTHON%"

REM --- 2) persisted user environment ---
if not defined PY (
  for /f "tokens=2,*" %%a in ('reg query "HKCU\Environment" /v BILIEX_PYTHON 2^>nul') do (
    if not defined PY if not "%%b"=="" set "PY=%%b"
  )
)

REM --- 3/4) launchers on PATH ---
if not defined PY for /f "delims=" %%i in ('where py 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"

REM --- 5) common install locations ---
if not defined PY (
  for /d %%d in ("%LOCALAPPDATA%\Python\pythoncore-*") do (
    if not defined PY if exist "%%d\python.exe" set "PY=%%d\python.exe"
  )
)
if not defined PY if exist "%LOCALAPPDATA%\Python\bin\python.exe" set "PY=%LOCALAPPDATA%\Python\bin\python.exe"
if not defined PY (
  for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
    if not defined PY if exist "%%d\python.exe" set "PY=%%d\python.exe"
  )
)

REM --- strip surrounding quotes if present ---
if defined PY for /f "delims=" %%i in ("%PY%") do set "PY=%%~i"

if not defined PY (
  echo [biliex] Python not found.
  echo [biliex] Install Python 3.10+, or point BILIEX_PYTHON at an interpreter:
  echo [biliex]   setx BILIEX_PYTHON "C:\Path\To\python.exe"
  echo [biliex]   then open a NEW terminal.
  exit /b 2
)

if not exist "%PY%" (
  echo [biliex] Configured interpreter does not exist:
  echo [biliex]   %PY%
  echo [biliex] Fix BILIEX_PYTHON or remove it to let autodetection run.
  exit /b 2
)

set "PYTHONPATH=%HERE%;%PYTHONPATH%"
"%PY%" -m biliex %*
exit /b %ERRORLEVEL%
