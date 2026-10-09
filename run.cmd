@echo off
rem zcode-toolkit one-click entry point for Windows.
rem
rem Finds a usable Python and forwards all arguments to autopilot.py.
rem
rem Usage:
rem   run.cmd                 full automated run
rem   run.cmd --no-deploy     build + test only, do not touch the client
rem   run.cmd --unattended    unattended mode (for CI)
rem
rem NOTE: this file is deliberately pure ASCII. cmd.exe parses .cmd/.bat files
rem using the OEM code page (936/GBK on Chinese Windows), so UTF-8 comments with
rem non-ASCII characters corrupt the parser and every subsequent line errors out
rem with "is not recognized as an internal or external command".
rem Keep it ASCII-only.

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PY="
rem Try the py launcher first, then python / python3. Use the first one that runs.
for %%C in (py python python3) do (
    if not defined PY (
        %%C -c "import sys" >nul 2>&1 && set "PY=%%C"
    )
)

if not defined PY (
    echo [x] No usable Python found.
    echo     Windows: winget install -e --id Python.Python.3.12
    echo     Or download from https://www.python.org/downloads/windows/
    echo     and make sure to tick "Add python.exe to PATH".
    rem Do not let the window vanish when double-clicked
    pause
    exit /b 2
)

set "PYTHONIOENCODING=utf-8"
%PY% "%SCRIPT_DIR%autopilot.py" %*
set "RC=%ERRORLEVEL%"

rem Pause only when double-clicked (no arguments and parent cmd line contains this script name),
rem not when invoked from an existing console or scripted with arguments.
if "%~1"=="" (
    echo %cmdcmdline% | find /i "%~nx0" >nul
    if not errorlevel 1 pause
)

exit /b %RC%
