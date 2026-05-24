@echo off
setlocal

:: ── RemoClaw management script for Windows ──────────────────────
:: Usage: remoclaw {start|stop|restart|status|log}

set DIR=%~dp0
set PIDFILE=%DIR%.remoclaw.pid
set LOGFILE=%DIR%remoclaw.log
set SCRIPT=%DIR%remoclaw.py

:: Detect Python
set PYTHON=
if exist "%DIR%.venv\Scripts\python.exe" (
    set PYTHON=%DIR%.venv\Scripts\python.exe
) else (
    where python3 >nul 2>&1 && set PYTHON=python3
    where python >nul 2>&1 && set PYTHON=python
)

if "%PYTHON%"=="" (
    echo [ERROR] Python not found. Run setup.bat first.
    exit /b 1
)

:: ── Route command ───────────────────────────────────────────────

if "%1"=="start"   goto :start
if "%1"=="stop"    goto :stop
if "%1"=="restart" goto :restart
if "%1"=="status"  goto :status
if "%1"=="log"     goto :log
goto :usage

:: ── Start ───────────────────────────────────────────────────────

:start
if exist "%PIDFILE%" (
    for /f %%P in (%PIDFILE%) do (
        tasklist /fi "PID eq %%P" 2>nul | find "%%P" >nul
        if !errorlevel! equ 0 (
            echo [*] RemoClaw already running (PID %%P)
            exit /b 0
        )
    )
)

echo [*] Starting RemoClaw...
start /b "" "%PYTHON%" "%SCRIPT%" >> "%LOGFILE%" 2>&1

:: Get PID of the last started python process
timeout /t 2 /nobreak >nul
for /f "tokens=2" %%P in ('tasklist /fi "imagename eq python.exe" /fo list 2^>nul ^| findstr "PID"') do (
    echo %%P> "%PIDFILE%"
)

if exist "%PIDFILE%" (
    for /f %%P in (%PIDFILE%) do echo [OK] RemoClaw started (PID %%P)
    echo      Log: type %LOGFILE%
) else (
    echo [ERROR] RemoClaw failed to start. Check log:
    if exist "%LOGFILE%" type "%LOGFILE%" | more
)
goto :eof

:: ── Stop ────────────────────────────────────────────────────────

:stop
if not exist "%PIDFILE%" (
    echo [*] RemoClaw is not running
    goto :eof
)

for /f %%P in (%PIDFILE%) do (
    echo [*] Stopping RemoClaw (PID %%P)...
    taskkill /pid %%P /f >nul 2>&1
)
del "%PIDFILE%" 2>nul
echo [OK] RemoClaw stopped
goto :eof

:: ── Restart ─────────────────────────────────────────────────────

:restart
call :stop
timeout /t 3 /nobreak >nul
call :start
goto :eof

:: ── Status ──────────────────────────────────────────────────────

:status
if not exist "%PIDFILE%" (
    echo [*] RemoClaw is not running
    goto :eof
)
for /f %%P in (%PIDFILE%) do (
    tasklist /fi "PID eq %%P" 2>nul | find "%%P" >nul
    if !errorlevel! equ 0 (
        echo [OK] RemoClaw is running (PID %%P)
    ) else (
        echo [*] RemoClaw is not running (stale PID file)
        del "%PIDFILE%" 2>nul
    )
)
goto :eof

:: ── Log ─────────────────────────────────────────────────────────

:log
if exist "%LOGFILE%" (
    type "%LOGFILE%" | more
) else (
    echo No log file found
)
goto :eof

:: ── Usage ───────────────────────────────────────────────────────

:usage
echo Usage: remoclaw {start^|stop^|restart^|status^|log}
exit /b 1
