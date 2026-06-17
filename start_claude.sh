#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
[ -d "venv" ] && source venv/bin/activate
python3 claude/claude_web2api.py
