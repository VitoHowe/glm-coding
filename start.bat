@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d %~dp0

set "APP_HOST=127.0.0.1"
set "APP_PORT=8787"
set "FALLBACK_PROXY_URL=http://127.0.0.1:17286"

if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if /i "%%A"=="APP_HOST" set "APP_HOST=%%B"
        if /i "%%A"=="APP_PORT" set "APP_PORT=%%B"
        if /i "%%A"=="FALLBACK_PROXY_URL" set "FALLBACK_PROXY_URL=%%B"
    )
)

for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%APP_PORT% .*LISTENING"') do (
    if not "%%P"=="" (
        echo [AegisFlow] Stopping existing process on port %APP_PORT% - PID %%P ...
        taskkill /F /T /PID %%P >nul 2>&1
    )
)

if not exist .venv\Scripts\python.exe (
    echo [AegisFlow] Creating virtual environment...
    py -3 -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
)

echo [AegisFlow] Syncing Python dependencies...
.venv\Scripts\python.exe -m pip install -r requirements.txt

if exist web\package.json (
    where npm >nul 2>&1
    if errorlevel 1 (
        echo [AegisFlow] npm not found; skipping Vue frontend build and using legacy page fallback.
    ) else (
        if not exist web\node_modules (
            echo [AegisFlow] Installing Vue frontend dependencies...
            pushd web
            call npm install
            popd
        )
        echo [AegisFlow] Building Vue frontend...
        pushd web
        call npm run build
        popd
    )
)

rem Built-in Python proxy pool starts on demand: when the Web switches to proxy_pool mode, or on startup if the persisted mode is proxy_pool.
set "SHOULD_START_PROXY_POOL=0"
if not "%FALLBACK_PROXY_URL%"=="" (
    set "PROXY_CHECK=%FALLBACK_PROXY_URL:127.0.0.1:1728=%"
    if not "!PROXY_CHECK!"=="%FALLBACK_PROXY_URL%" set "SHOULD_START_PROXY_POOL=1"
    set "PROXY_CHECK=%FALLBACK_PROXY_URL:localhost:1728=%"
    if not "!PROXY_CHECK!"=="%FALLBACK_PROXY_URL%" set "SHOULD_START_PROXY_POOL=1"
)

if "%SHOULD_START_PROXY_POOL%"=="1" (
    echo [AegisFlow] FALLBACK_PROXY_URL points to localhost; proxy pool starts on demand when switching to proxy_pool mode.
    for %%R in (17283 17284 17285 17286) do (
        for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%%R .*LISTENING"') do (
            if not "%%P"=="" (
                echo [AegisFlow] Stopping existing local proxy listener on port %%R - PID %%P ...
                taskkill /F /T /PID %%P >nul 2>&1
            )
        )
    )
) else (
    if "%FALLBACK_PROXY_URL%"=="" (
        echo [AegisFlow] FALLBACK_PROXY_URL is empty; proxy pool mode is disabled.
    ) else (
        echo [AegisFlow] FALLBACK_PROXY_URL=%FALLBACK_PROXY_URL% is external; FastAPI will not start a local proxy pool.
    )
)

echo [AegisFlow] Starting FastAPI server...
.venv\Scripts\python.exe -m uvicorn app.main:app --host %APP_HOST% --port %APP_PORT% --reload

endlocal
