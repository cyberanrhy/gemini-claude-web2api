#!/usr/bin/env bash
echo "Stopping all proxy processes..."
pkill -f "python3.*panel.py" 2>/dev/null || true
pkill -f "python3.*gemini_web2api" 2>/dev/null || true
pkill -f "python3.*claude_web2api" 2>/dev/null || true
echo "Done."
