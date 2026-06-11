#!/usr/bin/env bash
# Design2Make launcher — Linux / WSL / Docker (uses the bundled NWRFC SDK).
# Starts planning MCP :8001, prod-version MCP :8002, and the app :8000. Ctrl+C stops all three.
# Auto-installs uv if missing, then runs everything through `uv run` (RFC servers get their deps too).
# No pyrfc needed — the RFC servers call the NWRFC SDK directly via ctypes.
set -uo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "[setup] uv not found — installing…"
  curl -LsSf https://astral.sh/uv/install.sh | sh || python3 -m pip install --user uv || pip install --user uv
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { echo "[!] uv still not on PATH — install from https://docs.astral.sh/uv/ and re-run."; exit 1; }

echo "[setup] uv sync…"
uv sync

export SAPNWRFC_HOME="$PWD/mcp_remote/nwrfcsdk"
export LD_LIBRARY_PATH="$SAPNWRFC_HOME/lib:${LD_LIBRARY_PATH:-}"

pids=()
cleanup() { echo; echo "stopping…"; kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "▶ planning  MCP → http://127.0.0.1:8001/sse"
( cd mcp_remote && MCP_PORT=8001 exec uv run python sap_planning_mcp.py ) &  pids+=($!)
echo "▶ prodvers  MCP → http://127.0.0.1:8002/sse"
( cd mcp_remote && MCP_PORT=8002 exec uv run python sap_prodvers_mcp.py ) &  pids+=($!)

sleep 2
echo "▶ app           → http://localhost:8000   (Ctrl+C stops everything)"
uv run main.py
