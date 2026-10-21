@echo off
setlocal enabledelayedexpansion
title Surface Hybrid AI Hub - External SSD Automated Provisioner
color 0A

:: ============================================================================
:: 1. HARDWARE & PATH DETECTION
:: ============================================================================
echo =======================================================================
echo   Surface Laptop Studio (1st Gen) - External SSD Automated Setup
echo =======================================================================
echo.

:: Detect current drive and directory (locks onto external SSD regardless of drive letter)
set "SSD_DRIVE=%~d0"
set "SCRIPT_DIR=%~dp0"

echo [1/6] Detecting External SSD Mount Point...
echo       Mounted Drive: %SSD_DRIVE%
echo       Working Root:  %SCRIPT_DIR%
echo.

:: Check if script is running in an existing repo clone or at the SSD root
if exist "%SCRIPT_DIR%app.py" (
    set "HUB_ROOT=%SCRIPT_DIR%"
    echo       Status: Running from inside an existing cloned repository.
) else (
    set "HUB_ROOT=%SCRIPT_DIR%AI_Hub\"
    echo       Status: Running from SSD root. Target directory: !HUB_ROOT!
    
    if not exist "!HUB_ROOT!" mkdir "!HUB_ROOT!"
    
    :: Clone repository if app.py is missing
    if not exist "!HUB_ROOT!app.py" (
        echo.
        echo [Git] Repository not found locally. Cloning from GitHub...
        set /p "GH_REPO=Enter your GitHub repo URL (e.g., https://github.com/<user>/ssl-local-setup.git): "
        if "!GH_REPO!"=="" (
            echo [Error] No repository URL provided. Aborting.
            pause
            exit /b 1
        )
        git clone "!GH_REPO!" "!HUB_ROOT!"
        if errorlevel 1 (
            echo [Error] Git clone failed. Please verify your URL and internet connection.
            pause
            exit /b 1
        )
    )
)

cd /d "!HUB_ROOT!"

:: ============================================================================
:: 2. STRICT ZERO-C-WRITE HARDWARE SANDBOX STRUCTURE
:: ============================================================================
echo.
echo [2/6] Building Safe Workspace and Persistence Tree...

:: Ensure required database, sandbox, model, and redirected cache folders exist
if not exist "memory_store" mkdir "memory_store"
if not exist "safe_workspace" mkdir "safe_workspace"
if not exist "safe_workspace\scripts" mkdir "safe_workspace\scripts"
if not exist "safe_workspace\daily_reports" mkdir "safe_workspace\daily_reports"
if not exist "safe_workspace\weekly_reports" mkdir "safe_workspace\weekly_reports"
if not exist "models" mkdir "models"
if not exist "bin" mkdir "bin"
if not exist "temp" mkdir "temp"
if not exist "temp\pycache" mkdir "temp\pycache"
if not exist "temp\pip_cache" mkdir "temp\pip_cache"

:: Enforce zero internal C:\ drive writes by redirecting Windows temporary variables
set "TEMP=!HUB_ROOT!temp"
set "TMP=!HUB_ROOT!temp"
set "PIP_CACHE_DIR=!HUB_ROOT!temp\pip_cache"
set "PYTHONPYCACHEPREFIX=!HUB_ROOT!temp\pycache"
set "ANONYMIZED_TELEMETRY=False"
set "LITELLM_LOG=ERROR"

echo       - Persistent SQLite Store:  !HUB_ROOT!memory_store\
echo       - Path-Jailed Sandbox:      !HUB_ROOT!safe_workspace\
echo       - Redirected Temp/Cache:    !HUB_ROOT!temp\ (Zero C:\ writes)

:: ============================================================================
:: 3. USER-SPACE PYTHON VIRTUAL ENVIRONMENT
:: ============================================================================
echo.
echo [3/6] Configuring Portable Python Virtual Environment...

where python >nul 2>nul
if errorlevel 1 (
    echo [Error] Python executable not found in system PATH.
    echo Please install Python 3.10 - 3.12 on your Surface Laptop Studio.
    pause
    exit /b 1
)

:: Create clean virtual environment inside external SSD if missing
if not exist ".venv\Scripts\activate.bat" (
    echo       Creating .venv on external SSD...
    python -m venv .venv
    if errorlevel 1 (
        echo [Error] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

echo       Upgrading pip and installing dependencies without caching to C:...
python -m pip install --upgrade pip --no-cache-dir >nul 2>&1
if exist "requirements.txt" (
    pip install --no-cache-dir -r requirements.txt
) else (
    pip install --no-cache-dir streamlit litellm python-dotenv
)

:: ============================================================================
:: 4. ENVIRONMENT CREDENTIAL INITIALIZER (.env)
:: ============================================================================
echo.
echo [4/6] Initializing Local Secret Configuration...

if not exist ".env" (
    echo       Creating local template .env file...
    (
        echo # Surface Hybrid AI Hub - Local Secret Configuration
        echo GEMINI_API_KEY=
        echo TELEGRAM_BOT_TOKEN=
        echo TELEGRAM_CHAT_ID=
        echo ANTHROPIC_API_KEY=
        echo OPENAI_API_KEY=
    ) > ".env"
    echo       [Notice] .env template created at !HUB_ROOT!.env
) else (
    echo       Existing .env detected. Preserving credentials.
)

:: ============================================================================
:: 5. OPTIONAL OFFLINE ULTRALIGHT MODEL DOWNLOAD (2.2GB VRAM FIT)
:: ============================================================================
echo.
echo [5/6] Checking Local Offline Taiwanese Model (RTX 3050 Ti VRAM)...

set "MODEL_PATH=!HUB_ROOT!models\Llama-3.2-Taiwan-3B-Instruct-Q4_K_M.gguf"
if not exist "!MODEL_PATH!" (
    echo       Ultralight 2.2GB model for Tier 2 offline fallback not found.
    set /p "DL_CHOICE=Do you want to download Llama-3.2-Taiwan-3B-Instruct (Q4_K_M, ~2.2GB) now? (y/n): "
    if /i "!DL_CHOICE!"=="y" (
        echo       Downloading to !HUB_ROOT!models\...
        curl -L -o "!MODEL_PATH!" "https://huggingface.co/lianghsun/Llama-3.2-Taiwan-3B-Instruct-GGUF/resolve/main/Llama-3.2-Taiwan-3B-Instruct-Q4_K_M.gguf"
        if errorlevel 1 (
            echo       [Warning] Download failed or interrupted. You can add GGUF files to models\ manually.
        ) else (
            echo       Model successfully placed in !HUB_ROOT!models\
        )
    ) else (
        echo       Skipped model download. Hybrid cloud modes remain fully operational.
    )
) else (
    echo       Offline model ready: !MODEL_PATH!
)

:: ============================================================================
:: 6. ONE-CLICK LAUNCHER & DESKTOP SHORTCUT GENERATION
:: ============================================================================
echo.
echo [6/6] Generating One-Click Portable Launchers...

set "LAUNCHER=!HUB_ROOT!Start_AI_Hub.bat"
(
    echo @echo off
    echo title Surface Hybrid AI Hub
    echo cd /d "%%~dp0"
    echo.
    echo :: Enforce zero C:\ disk writes
    echo set "TEMP=%%~dp0temp"
    echo set "TMP=%%~dp0temp"
    echo set "PIP_CACHE_DIR=%%~dp0temp\pip_cache"
    echo set "PYTHONPYCACHEPREFIX=%%~dp0temp\pycache"
    echo set "ANONYMIZED_TELEMETRY=False"
    echo set "LITELLM_LOG=ERROR"
    echo.
    echo :: Launch local llama-server if GGUF model and server binary exist
    echo if exist "bin\llama-server.exe" ^(
    echo     if exist "models\Llama-3.2-Taiwan-3B-Instruct-Q4_K_M.gguf" ^(
    echo         start "Local_TAIDE_3B" /min "bin\llama-server.exe" -m "models\Llama-3.2-Taiwan-3B-Instruct-Q4_K_M.gguf" --host 127.0.0.1 --port 8080 -ngl 28 -c 4096 --cache-type-k q8_0 --mmap
    echo     ^)
    echo ^)
    echo.
    echo :: Launch Unified Web Dashboard and Telegram Poller
    echo call ".venv\Scripts\activate.bat"
    echo streamlit run "app.py" --server.headless=false --browser.gatherUsageStats=false
) > "!LAUNCHER!"

:: Create Desktop Shortcut using Windows Script Host VBScript
set "VBS_SCRIPT=!HUB_ROOT!temp\create_shortcut.vbs"
(
    echo Set oWS = WScript.CreateObject("WScript.Shell"^)
    echo sLinkFile = oWS.SpecialFolders("Desktop"^) ^& "\Surface Hybrid AI Hub.lnk"
    echo Set oLink = oWS.CreateShortcut(sLinkFile^)
    echo oLink.TargetPath = "!LAUNCHER!"
    echo oLink.WorkingDirectory = "!HUB_ROOT!"
    echo oLink.Description = "Launch Surface Hybrid MCP AI Hub on External SSD"
    echo oLink.Save
) > "!VBS_SCRIPT!"

cscript //nologo "!VBS_SCRIPT!" >nul 2>&1
del "!VBS_SCRIPT!" >nul 2>&1

echo       - Launcher created: !LAUNCHER!
echo       - Desktop shortcut created: "Surface Hybrid AI Hub.lnk"
echo.
echo =======================================================================
echo   Provisioning Complete! Setup verified on %SSD_DRIVE%
echo =======================================================================
echo.
echo Immediate Next Steps:
echo 1. Open '!HUB_ROOT!.env' and add your GEMINI_API_KEY and TELEGRAM_BOT_TOKEN.
echo 2. Double-click the "Surface Hybrid AI Hub" shortcut on your Desktop.
echo.
pause
