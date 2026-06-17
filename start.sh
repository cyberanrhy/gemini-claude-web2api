#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Activate venv if exists
[ -d "venv" ] && source venv/bin/activate

echo "Starting panel on http://127.0.0.1:8083 ..."
nohup python3 panel.py --port 8083 > panel.log 2>&1 &
sleep 2
echo "Panel PID: $!"
echo "Open http://localhost:8083"
