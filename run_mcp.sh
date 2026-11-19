#!/bin/bash
# AgenticSRE MCP Server Launcher
set -e

cd "$(dirname "$0")"
export PYTHONPATH="$(pwd):$PYTHONPATH"

echo "Starting AgenticSRE MCP Server (stdio)..." >&2
exec python mcp_server.py
