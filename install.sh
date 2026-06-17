#!/usr/bin/env bash
set -e
echo "============================================"
echo "  gemini-claude-web2api — install"
echo "============================================"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python 3 not found. Install python3."
    exit 1
fi

# Create venv
if [ ! -d "venv" ]; then
    echo "[1/3] Creating virtual environment..."
    python3 -m venv venv
else
    echo "[1/3] Virtual environment already exists"
fi

# Activate and install
echo "[2/3] Installing dependencies..."
source venv/bin/activate
pip install -r requirements.txt
pip install -r gemini/requirements.txt 2>/dev/null || true
pip install -r claude/requirements.txt 2>/dev/null || true

# Gemini config
if [ ! -f "config.json" ]; then
    echo "[3/3] Creating config.json from example..."
    cp gemini/config.json.example config.json
else
    echo "[3/3] config.json already exists"
fi

# Done
echo ""
echo "============================================"
echo "  INSTALLATION COMPLETE"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Export cookies from Firefox (cookies.txt extension):"
echo "     - Visit gemini.google.com, export -> cookie.txt"
echo "     - Visit claude.ai, export -> cookie_claude.txt"
echo "     Place both in the project folder."
echo ""
echo "  2. Run panel:  bash start.sh"
echo "     or direct:  bash start_gemini.sh / bash start_claude.sh"
echo ""
echo "KNOWN ISSUES:"
echo "  - curl_cffi on Debian/Ubuntu: pip install --break-system-packages curl_cffi"
echo "  - macOS: not tested, YMMV with curl_cffi"
echo ""
