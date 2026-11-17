@echo off
setlocal
chcp 65001 >nul 2>&1
set PYTHONUTF8=1

set "SCRIPT_DIR=%~dp0"
set "BRIDGE_PY=%SCRIPT_DIR%github_bridge.py"

where python >nul 2>&1
if %ERRORLEVEL%==0 (
    python "%BRIDGE_PY%" %*
    goto :done
)

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 "%BRIDGE_PY%" %*
    goto :done
)

echo [ERROR] No se encontro Python 3 en PATH.
echo Instalalo desde https://www.python.org/downloads/ o con: winget install Python.Python.3.12
set "EXIT_CODE=2"
pause
exit /b %EXIT_CODE%

:done
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo El puente termino con codigo %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
