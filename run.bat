@echo off
REM Design2Make launcher — Windows.
REM Starts: planning MCP :8001, prod-version MCP :8002 (separate windows), and the app :8000.
REM
REM NOTE: the bundled NWRFC SDK is the LINUX build. On Windows the two RFC servers need the
REM       WINDOWS NWRFC SDK (.dll) in mcp_remote\nwrfcsdk\ (replace the folder). The app (:8000)
REM       runs fine without it — only MRP / demand / production-version need the RFC servers.
REM Prereqs: uv sync, pip install pyrfc, and .env files filled in (root + mcp_remote) — see README.

cd /d "%~dp0"
set "SAPNWRFC_HOME=%CD%\mcp_remote\nwrfcsdk"
set "PATH=%SAPNWRFC_HOME%\lib;%PATH%"

echo Starting planning MCP on :8001 ...
start "D2M planning :8001" cmd /k "pushd "%CD%\mcp_remote" && set MCP_PORT=8001 && python sap_planning_mcp.py"
echo Starting prod-version MCP on :8002 ...
start "D2M prodvers :8002" cmd /k "pushd "%CD%\mcp_remote" && set MCP_PORT=8002 && python sap_prodvers_mcp.py"

timeout /t 2 /nobreak >nul
echo Starting app on http://localhost:8000 ...
uv run main.py
