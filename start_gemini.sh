#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
[ -d "venv" ] && source venv/bin/activate
python3 gemini/gemini_web2api.py --config config.json --cookie-file cookie.txt --proxy http://127.0.0.1:12334
