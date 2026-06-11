@echo off
REM Design2Make launcher - Windows. Starts planning MCP :8001, prod-version MCP :8002, and app :8000.
REM Auto-installs uv if missing, then runs EVERYTHING through `uv run` (RFC servers get their deps too).
REM No pyrfc needed - the RFC servers call the NWRFC SDK directly via ctypes.
REM NOTE: the bundled NWRFC SDK is the LINUX build. On Windows the RFC servers need the WINDOWS
REM       NWRFC SDK (.dll) in mcp_remote\nwrfcsdk\. The app (:8000) runs without it.

cd /d "%~dp0"

REM --- ensure uv ---
where uv >/dev/null 2>&1
if errorlevel 1 (
  echo [setup] uv not found - installing it...
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
where uv >/dev/null 2>&1
if errorlevel 1 ( python -m pip install --quiet uv 2>/dev/null || py -3 -m pip install --quiet uv )
where uv >/dev/null 2>&1
if errorlevel 1 (
  echo. & echo [!] Could not install uv automatically. Install it once then re-run:
  echo       powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
  pause & exit /b 1
)

echo [setup] uv sync ...
call uv sync || ( echo [!] uv sync failed & pause & exit /b 1 )

set "SAPNWRFC_HOME=%CD%\mcp_remote\nwrfcsdk"
set "PATH=%SAPNWRFC_HOME%\lib;%PATH%"

echo Starting planning MCP on :8001 ...
start "D2M planning :8001" cmd /k "cd /d %CD%\mcp_remote ^&^& set MCP_PORT=8001 ^&^& uv run python sap_planning_mcp.py"
echo Starting prod-version MCP on :8002 ...
start "D2M prodvers :8002" cmd /k "cd /d %CD%\mcp_remote ^&^& set MCP_PORT=8002 ^&^& uv run python sap_prodvers_mcp.py"

timeout /t 3 /nobreak >/dev/null
echo Starting app on http://localhost:8000 ...
uv run main.py
