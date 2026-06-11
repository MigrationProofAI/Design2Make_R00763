#!/usr/bin/env bash
# Design2Make launcher — Linux / WSL / Docker (uses the bundled NWRFC SDK).
# Starts: planning MCP :8001, prod-version MCP :8002, and the app :8000.
# Ctrl+C stops all three. Prereqs: `uv sync` + `pip install pyrfc`, and .env files filled in
# (.env at root, mcp_remote/.env for the RFC creds) — see README.
set -uo pipefail
cd "$(dirname "$0")"

# bundled SAP NWRFC SDK (Linux .so) for pyrfc
export SAPNWRFC_HOME="$PWD/mcp_remote/nwrfcsdk"
export LD_LIBRARY_PATH="$SAPNWRFC_HOME/lib:${LD_LIBRARY_PATH:-}"

pids=()
cleanup() { echo; echo "stopping…"; kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "▶ planning  MCP → http://127.0.0.1:8001/sse"
( cd mcp_remote && MCP_PORT=8001 exec python sap_planning_mcp.py ) &  pids+=($!)
echo "▶ prodvers  MCP → http://127.0.0.1:8002/sse"
( cd mcp_remote && MCP_PORT=8002 exec python sap_prodvers_mcp.py ) &  pids+=($!)

sleep 2  # let the RFC servers bind before the app connects
echo "▶ app           → http://localhost:8000   (Ctrl+C stops everything)"
uv run main.py
