#!/usr/bin/env python3
"""
Proxy Control Panel — Web UI for Gemini & Claude local proxies.
Zero external dependencies (stdlib only).
"""

import http.server
import json
import subprocess
import time
import os
import re
import socket
import signal
import sys
import urllib.request
import urllib.error
import threading
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

ON_WINDOWS = sys.platform == "win32"

# ── Config ──────────────────────────────────────────────────────────────
PANEL_PORT = 8083
GEMINI_PORT = 8081
CLAUDE_PORT = 8082

HOME = os.path.expanduser("~")
GEMINI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini")
CLAUDE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude")

if ON_WINDOWS:
    GEMINI_LOG = os.path.join(tempfile.gettempdir(), "gemini_proxy.log")
    CLAUDE_LOG = os.path.join(tempfile.gettempdir(), "claude_proxy.log")
else:
    GEMINI_LOG = "/tmp/gemini_proxy.log"
    CLAUDE_LOG = "/tmp/claude_proxy.log"

if not ON_WINDOWS:
    GEMINI_COOKIE_SCRIPT = os.path.join(HOME, ".local", "bin", "gemini-cookie-update")
    CLAUDE_COOKIE_SCRIPT = os.path.join(HOME, ".local", "bin", "claude-cookie-update")
else:
    GEMINI_COOKIE_SCRIPT = None
    CLAUDE_COOKIE_SCRIPT = None

GEMINI_COOKIE_FILE = os.path.join(GEMINI_DIR, "cookie.txt")
CLAUDE_COOKIE_FILE = os.path.join(CLAUDE_DIR, "cookie_claude.txt")

GEMINI_SCRIPT = os.path.join(GEMINI_DIR, "gemini_web2api.py")
CLAUDE_SCRIPT = os.path.join(CLAUDE_DIR, "claude_web2api.py")

GEMINI_REPO = "https://github.com/cyberanrhy/gemini-web2api.git"
CLAUDE_REPO = "https://github.com/cyberanrhy/claude-web2api.git"

VPN_HOST = "127.0.0.1"
VPN_PORT = 12334

# ── Helpers ─────────────────────────────────────────────────────────────

def log(msg):
    print(f"[panel] {msg}", flush=True)


def check_port(port):
    """Check if a TCP port is open."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        result = s.connect_ex(("127.0.0.1", port))
        s.close()
        return result == 0
    except Exception:
        return False


def check_vpn():
    """Check if Hiddify proxy port is open."""
    return check_port(VPN_PORT)


def http_get(url, timeout=5):
    """Make HTTP GET, return (status_code, body_or_error)."""
    try:
        req = urllib.request.Request(url)
        with _direct_opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, str(e)
    except Exception as e:
        return 0, str(e)


def http_post_json(url, data, timeout=15):
    """Make HTTP POST with JSON body, return (status_code, body_or_error)."""
    try:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with _direct_opener.open(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            return resp.status, resp_body
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        return e.code, err_body
    except Exception as e:
        return 0, str(e)


_direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_log_file(name):
    return GEMINI_LOG if name == "gemini" else CLAUDE_LOG


def read_log_file(filepath, lines=50):
    """Read the last N lines from a log file."""
    try:
        with open(filepath, "r", errors="replace") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:])
    except FileNotFoundError:
        return None
    except Exception as e:
        return f"[error reading log: {e}]"


def count_proxy_process(port):
    """Count processes listening on a port."""
    try:
        if ON_WINDOWS:
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=3
            )
            for line in result.stdout.split("\n"):
                if f":{port}" in line and "LISTENING" in line:
                    return 1
            return 0
        else:
            result = subprocess.run(
                ["ss", "-tlnp"], capture_output=True, text=True, timeout=3
            )
            for line in result.stdout.split("\n"):
                if f":{port}" in line and "LISTEN" in line:
                    return 1
            return 0
    except Exception:
        return -1


def is_installed(name):
    script = GEMINI_SCRIPT if name == "gemini" else CLAUDE_SCRIPT
    return os.path.exists(script)


def install_proxy(name, force=False):
    """Git clone the proxy repo and do initial setup.
    If force=True, backup cookies+config, delete, re-clone, restore."""
    repo_url = GEMINI_REPO if name == "gemini" else CLAUDE_REPO
    dest_dir = GEMINI_DIR if name == "gemini" else CLAUDE_DIR
    script = GEMINI_SCRIPT if name == "gemini" else CLAUDE_SCRIPT

    if is_installed(name) and not force:
        return {"success": True, "message": f"{name} is already installed at {dest_dir}"}

    # Backup user data before deleting
    backup = {}
    if force and os.path.exists(dest_dir):
        for fname in ["config.json", "cookie.txt", "cookie_claude.txt"]:
            fpath = os.path.join(dest_dir, fname)
            if os.path.exists(fpath):
                try:
                    with open(fpath) as f:
                        backup[fname] = f.read()
                except:
                    pass

    # Try with no proxy first, fallback to Hiddify
    env = os.environ.copy()
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)

    try:
        # If force, remove existing dir before clone
        if force and os.path.exists(dest_dir):
            import shutil
            shutil.rmtree(dest_dir, ignore_errors=True)

        # Create parent dir if needed
        os.makedirs(dest_dir, exist_ok=True)

        proc = subprocess.run(
            ["git", "clone", repo_url, dest_dir],
            capture_output=True, text=True, timeout=60, env=env,
        )
        if proc.returncode != 0:
            # Retry with Hiddify proxy
            proc = subprocess.run(
                ["git", "clone", repo_url, dest_dir],
                capture_output=True, text=True, timeout=60,
                env={**env, "http_proxy": f"http://{VPN_HOST}:{VPN_PORT}",
                     "https_proxy": f"http://{VPN_HOST}:{VPN_PORT}"},
            )
            if proc.returncode != 0:
                return {"success": False,
                        "message": f"git clone failed: {proc.stderr[:300]}".strip()}

        # Copy config from example if not exists
        example = os.path.join(dest_dir, "config.json.example")
        config = os.path.join(dest_dir, "config.json")
        if os.path.exists(example) and not os.path.exists(config):
            import shutil
            shutil.copy(example, config)

        # Restore user data (config, cookies) from backup
        for fname, content in backup.items():
            fpath = os.path.join(dest_dir, fname)
            try:
                with open(fpath, "w") as f:
                    f.write(content)
            except:
                pass

        # Restart proxy to pick up new code
        restart_proxy(name)

        return {"success": True, "message": f"Installed {name} from {repo_url}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def restart_proxy(name):
    """Kill and restart a proxy, redirecting output to log file."""
    port = GEMINI_PORT if name == "gemini" else CLAUDE_PORT
    proxy_dir = GEMINI_DIR if name == "gemini" else CLAUDE_DIR
    log_file = get_log_file(name)
    script_name = "gemini_web2api.py" if name == "gemini" else "claude_web2api.py"
    cookie_flag = "--cookie-file"
    cookie_file = GEMINI_COOKIE_FILE if name == "gemini" else CLAUDE_COOKIE_FILE
    config_file = os.path.join(proxy_dir, "config.json")

    if ON_WINDOWS:
        try:
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=5
            )
            pids = set()
            for line in result.stdout.split("\n"):
                parts = line.strip().split()
                if f":{port}" in line and "LISTENING" in line and len(parts) >= 5:
                    pids.add(parts[-1])
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=5)
        except Exception:
            pass
    else:
        try:
            subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                capture_output=True, timeout=5
            )
        except Exception:
            pass

    time.sleep(0.5)

    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    cmd = [
        pythonw,
        os.path.join(proxy_dir, script_name),
        "--config", config_file,
        cookie_flag, cookie_file,
    ]

    try:
        with open(log_file, "a") as lf:
            lf.write(f"\n--- restart at {datetime.now().isoformat()} ---\n")
            proc = subprocess.Popen(
                cmd, stdout=lf, stderr=lf,
                cwd=proxy_dir,
                stdin=subprocess.DEVNULL
            )
        return {"success": True, "message": f"PID {proc.pid}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def run_health_test(name):
    """Send a real chat completion request to the proxy."""
    port = GEMINI_PORT if name == "gemini" else CLAUDE_PORT
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    payload = {
        "model": "gemini-3.5-flash" if name == "gemini" else "claude-haiku-4-5-20251001",
        "messages": [{"role": "user", "content": "say hi in 3 words or less"}],
        "max_tokens": 10,
    }
    t0 = time.time()
    status, body = http_post_json(url, payload, timeout=120)
    elapsed = round(time.time() - t0, 3)

    if status == 200:
        try:
            data = json.loads(body)
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {"success": True, "response": content.strip(), "time": elapsed}
        except Exception:
            return {"success": False, "response": body[:200], "time": elapsed}
    return {"success": False, "response": body[:200], "time": elapsed}


def parse_cookie_expiry(filepath):
    """Extearct cookie expiry dates from Netscape cookie file. Return days until nearest expiry."""
    if not os.path.exists(filepath):
        return None
    try:
        now = time.time()
        min_days = None
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("http"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 5:
                    try:
                        expiry = int(parts[4])
                        if expiry > now:
                            days = (expiry - now) / 86400
                            if min_days is None or days < min_days:
                                min_days = days
                    except (ValueError, IndexError):
                        pass
        return round(min_days, 1) if min_days is not None else None
    except Exception:
        return None


def read_config(name):
    """Read proxy config.json and return proxy value."""
    config_file = os.path.join(GEMINI_DIR if name == "gemini" else CLAUDE_DIR, "config.json")
    if not os.path.exists(config_file):
        return None
    try:
        with open(config_file) as f:
            cfg = json.load(f)
        return cfg.get("proxy")
    except Exception:
        return None


def write_config(name, proxy_val):
    """Write proxy value to config.json and restart the proxy."""
    config_file = os.path.join(GEMINI_DIR if name == "gemini" else CLAUDE_DIR, "config.json")
    if not os.path.exists(config_file):
        return {"success": False, "message": "config.json not found"}
    try:
        with open(config_file) as f:
            cfg = json.load(f)
        if proxy_val is None:
            cfg.pop("proxy", None)
        else:
            cfg["proxy"] = proxy_val
        with open(config_file, "w") as f:
            json.dump(cfg, f, indent=2)
        restart_proxy(name)
        return {"success": True, "message": "config saved, proxy restarted"}
    except Exception as e:
        return {"success": False, "message": str(e)}


_upstream_cache = {}
def check_upstream_access():
    """Check if upstream (Google/Claude) is reachable via TCP 443.
    Returns dict with direct/proxy status per target, 60s cache."""
    now = time.time()
    cached = _upstream_cache.get("result")
    cached_at = _upstream_cache.get("at", 0)
    if cached and now - cached_at < 60:
        return cached

    vpn_alive = check_vpn()

    def tcp_test(host, via_proxy=False):
        try:
            if via_proxy and vpn_alive:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3)
                s.connect(("127.0.0.1", VPN_PORT))
                s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
                resp = s.recv(4096, socket.MSG_PEEK)
                s.close()
                return resp.startswith(b"HTTP/") or b"200" in resp
            else:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3)
                s.connect((host, 443))
                s.close()
                return True
        except:
            return False

    targets = [("gemini.google.com", "gemini"), ("claude.ai", "claude")]
    results = {}
    for host, name in targets:
        direct = tcp_test(host)
        via_proxy = tcp_test(host, via_proxy=True) if vpn_alive else False
        results[name] = {"direct": direct, "proxy": via_proxy}

    _upstream_cache["result"] = results
    _upstream_cache["at"] = now
    return results


# ── API & Web Handler ────────────────────────────────────────────────────

class PanelHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        msg = fmt % args
        if "GET /api" in msg or "POST /api" in msg:
            return
        log(msg)

    def do_GET(self):
        try:
            self._do_GET()
        except Exception as e:
            log(f"do_GET error: {e}")
            try:
                self._json({"error": f"internal error: {e}"}, 500)
            except:
                pass

    def _do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/" or path == "":
            self._serve_html()
        elif path == "/api/status":
            self._json(self._get_status())
        elif path.startswith("/api/logs/"):
            name = path.split("/")[-1]
            if name in ("gemini", "claude"):
                qs = parse_qs(parsed.query)
                lines = int(qs.get("lines", [50])[0])
                text = read_log_file(get_log_file(name), lines)
                self._json({"name": name, "log": text, "exists": text is not None})
            else:
                self._json({"error": "unknown proxy"}, 404)
        elif path.startswith("/api/test/"):
            name = path.split("/")[-1]
            if name in ("gemini", "claude"):
                result = run_health_test(name)
                self._json(result)
            else:
                self._json({"error": "unknown proxy"}, 404)
        elif path.startswith("/api/config/"):
            name = path.split("/")[-1]
            if name in ("gemini", "claude"):
                proxy_val = read_config(name)
                self._json({"name": name, "proxy": proxy_val})
            else:
                self._json({"error": "unknown proxy"}, 404)
        elif path == "/api/upstream":
            self._json(check_upstream_access())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:
            log(f"do_POST error: {e}")
            try:
                self._json({"error": f"internal error: {e}"}, 500)
            except:
                pass

    def _do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/action/"):
            parts = path.split("/")
            if len(parts) >= 5:
                name = parts[3]
                action = parts[4]
                if name in ("gemini", "claude"):
                    if action == "restart":
                        if not is_installed(name):
                            self._json({"success": False, "message": "not installed"}, 400)
                            return
                        result = restart_proxy(name)
                        self._json(result)
                        return
                    elif action == "cookies":
                        if not is_installed(name):
                            self._json({"success": False, "message": "not installed"}, 400)
                            return
                        result = self._handle_cookies(name)
                        self._json(result)
                        return
                    elif action == "install":
                        result = install_proxy(name)
                        self._json(result)
                        return
                    elif action == "reinstall":
                        result = install_proxy(name, force=True)
                        self._json(result)
                        return
            self._json({"error": "invalid action"}, 400)
        elif path == "/api/cookies/paste/gemini" or path == "/api/cookies/paste/claude":
            name = path.split("/")[-1]
            self._handle_paste_cookies(name)
            return
        elif path.startswith("/api/config/"):
            name = path.split("/")[-1]
            if name in ("gemini", "claude"):
                try:
                    content_len = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(content_len))
                    proxy_val = body.get("proxy")  # None or string URL
                    result = write_config(name, proxy_val)
                    self._json(result)
                except Exception as e:
                    self._json({"success": False, "message": str(e)}, 400)
            else:
                self._json({"error": "unknown proxy"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def _handle_paste_cookies(self, name):
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_len).decode("utf-8", errors="replace")
        except Exception:
            self._json({"success": False, "message": "Failed to read request body"}, 400)
            return

        if not raw or len(raw) < 20:
            self._json({"success": False, "message": "Empty or too short — paste the full cookie export."}, 400)
            return

        # Validate: must contain Netscape header or tab-separated lines
        lines = raw.strip().split("\n")
        valid_lines = 0
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 5 and parts[4].isdigit():
                valid_lines += 1

        if valid_lines < 2:
            self._json({"success": False,
                        "message": f"Only {valid_lines} valid cookies found. Need at least 2. "
                                   f"Export using cookies.txt extension (Netscape format)."}, 400)
            return

        dst = GEMINI_COOKIE_FILE if name == "gemini" else CLAUDE_COOKIE_FILE
        try:
            with open(dst, "w") as f:
                f.write(raw)
            self._json({"success": True, "message": f"Saved {valid_lines} cookies to {dst}"})
        except Exception as e:
            self._json({"success": False, "message": f"Write error: {e}"}, 500)

    def _handle_cookies(self, name):
        if ON_WINDOWS:
            return {
                "success": False,
                "message": "On Windows: export cookies from Firefox using cookies.txt extension, "
                           f"then use PASTE button to save to {GEMINI_COOKIE_FILE}"
            }
        script = GEMINI_COOKIE_SCRIPT if name == "gemini" else CLAUDE_COOKIE_SCRIPT
        if script and os.path.exists(script):
            try:
                subprocess.Popen(
                    ["gnome-terminal", "--hold", "--", "bash", "-c", script],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                return {"success": True, "message": f"Launched {script}"}
            except Exception as e:
                return {"success": False, "message": str(e)}
        else:
            return {
                "success": False,
                "message": f"Script not found: {script}\n"
                           f"Export cookies manually and save to "
                           f"{GEMINI_COOKIE_FILE if name == 'gemini' else CLAUDE_COOKIE_FILE}"
            }

    def _get_status(self):
        gemini_installed = is_installed("gemini")
        claude_installed = is_installed("claude")
        vpn_alive = check_vpn()

        gemini_alive = check_port(GEMINI_PORT) if gemini_installed else False
        claude_alive = check_port(CLAUDE_PORT) if claude_installed else False

        gemini_rt = None
        claude_rt = None
        if gemini_alive:
            t0 = time.time()
            code, _ = http_get(f"http://127.0.0.1:{GEMINI_PORT}/v1/models")
            gemini_rt = round(time.time() - t0, 3)
            gemini_alive = code == 200
        if claude_alive:
            t0 = time.time()
            code, _ = http_get(f"http://127.0.0.1:{CLAUDE_PORT}/v1/models")
            claude_rt = round(time.time() - t0, 3)
            claude_alive = code == 200

        gemini_cookie_expiry = parse_cookie_expiry(GEMINI_COOKIE_FILE)
        claude_cookie_expiry = parse_cookie_expiry(CLAUDE_COOKIE_FILE)
        gemini_proxy = read_config("gemini")
        claude_proxy = read_config("claude")

        return {
            "gemini": {
                "alive": gemini_alive,
                "installed": gemini_installed,
                "port": GEMINI_PORT,
                "response_time": gemini_rt,
                "cookie_expiry_days": gemini_cookie_expiry,
                "log_exists": os.path.exists(GEMINI_LOG),
                "proxy": gemini_proxy,
            },
            "claude": {
                "alive": claude_alive,
                "installed": claude_installed,
                "port": CLAUDE_PORT,
                "response_time": claude_rt,
                "cookie_expiry_days": claude_cookie_expiry,
                "log_exists": os.path.exists(CLAUDE_LOG),
                "proxy": claude_proxy,
            },
            "vpn": {"alive": vpn_alive, "port": VPN_PORT},
        }

    def _serve_html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode("utf-8"))

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        log(f"{self.client_address[0]} - {args[0]}")

    do_PUT = do_POST
    do_DELETE = do_POST


# ── HTML Page ────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Proxy Control Panel</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#0f0;font-family:'Courier New',monospace;padding:20px;max-width:1200px;margin:0 auto;min-height:100vh}
h1{font-size:1.3em;margin-bottom:4px;color:#0f0;text-transform:uppercase;letter-spacing:3px}
.sub{color:#080;font-size:0.75em;margin-bottom:20px}
.panel-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}

.card{border:1px solid #0f0;padding:16px;background:#050505;position:relative}
.card.gemini{border-color:#0f0}
.card.claude{border-color:#fa0}
.card-header{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.indicator{width:12px;height:12px;border-radius:50%;display:inline-block}
.indicator.alive{background:#0f0;box-shadow:0 0 8px #0f0;animation:pulse 2s infinite}
.indicator.dead{background:#500;box-shadow:0 0 4px #500}
.indicator.unknown{background:#440}
@keyframes pulse{0%{opacity:1}50%{opacity:0.5}100%{opacity:1}}
.card-title{font-size:1em;font-weight:bold;text-transform:uppercase}
.card-title .gemini{color:#0f0}
.card-title .claude{color:#fa0}
.card-body{font-size:0.8em;color:#0a0;line-height:1.6}
.card-body .row{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid #0a0a0a}
.card-body .row:last-child{border:none}
.card-body .label{color:#060}
.card-body .value{color:#0f0}
.card-body .value.warn{color:#fa0}
.card-body .value.critical{color:#f00}
.actions{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
.installed-actions{display:flex;gap:8px;flex-wrap:wrap}
.missing-actions{display:none;gap:8px;flex-wrap:wrap}
.btn[title]{position:relative}
.btn[title]:hover::after{content:attr(title);position:absolute;bottom:calc(100% + 4px);left:50%;transform:translateX(-50%);background:#000;color:#0f0;border:1px solid #0f0;padding:4px 8px;font-size:0.65em;white-space:nowrap;z-index:10;pointer-events:none}
.btn{background:transparent;border:1px solid #0f0;color:#0f0;padding:6px 14px;cursor:pointer;font-family:inherit;font-size:0.78em;transition:all 0.2s}
.btn:hover{background:#0f0;color:#000;box-shadow:0 0 6px #0f0}
.btn.danger{border-color:#f00;color:#f00}
.btn.danger:hover{background:#f00;color:#000}
.btn.warning{border-color:#fa0;color:#fa0}
.btn.warning:hover{background:#fa0;color:#000}
.btn:disabled{opacity:0.3;cursor:not-allowed}
.btn.small{padding:3px 8px;font-size:0.7em}

.section-title{color:#0f0;font-size:0.9em;text-transform:uppercase;letter-spacing:2px;margin:20px 0 10px;border-bottom:1px solid #0f0;padding-bottom:4px}

.log-box{background:#000;border:1px solid #0a0;padding:8px;font-size:0.68em;line-height:1.3;height:240px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;color:#080}
.log-box .bright{color:#0f0}

.test-result{background:#000;border:1px solid #0f0;padding:10px;font-size:0.75em;margin-top:8px;min-height:20px}
.test-result.success{border-color:#0f0}
.test-result.fail{border-color:#f00}

footer{margin-top:30px;padding-top:10px;border-top:1px solid #0a0a0a;font-size:0.7em;color:#060;text-align:center}
footer a{color:#080;text-decoration:none}
footer a:hover{color:#0f0}

.matrix-rain{position:fixed;top:0;left:0;width:100%;height:100%;z-index:-1;opacity:0.06;pointer-events:none}

.loading{opacity:0.5;pointer-events:none}

@media(max-width:700px){.panel-grid{grid-template-columns:1fr}}

.status-bar{display:flex;gap:16px;font-size:0.72em;color:#060;margin-bottom:16px;flex-wrap:wrap}
.status-bar span{display:flex;align-items:center;gap:4px}

.toast{position:fixed;bottom:20px;right:20px;background:#000;border:1px solid #0f0;padding:10px 16px;font-size:0.75em;z-index:999;max-width:400px;opacity:0;transition:opacity 0.3s}
.toast.show{opacity:1}

.confirm-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.85);z-index:1000;justify-content:center;align-items:center}
.confirm-overlay.show{display:flex}
.confirm-dialog{background:#050505;border:2px solid #0f0;padding:24px;max-width:400px;text-align:center}
.confirm-dialog p{margin-bottom:16px;font-size:0.85em;color:#0a0}
.confirm-dialog .btn-group{display:flex;gap:12px;justify-content:center}
</style>
</head>
<body>

<canvas id="matrix" class="matrix-rain"></canvas>

<div id="confirmOverlay" class="confirm-overlay">
  <div class="confirm-dialog">
    <p id="confirmMsg" data-i18n="confirm_restart">Are you sure?</p>
    <div class="btn-group">
      <button class="btn" onclick="confirmAction(true)">&gt; <span data-i18n="btn_confirm">CONFIRM</span></button>
      <button class="btn" onclick="confirmAction(false)">&gt; <span data-i18n="btn_cancel">CANCEL</span></button>
    </div>
  </div>
</div>

<div id="pasteOverlay" class="confirm-overlay">
  <div class="confirm-dialog" style="max-width:600px;width:90%">
    <p style="font-size:1.1em;margin-bottom:8px">📋 <span data-i18n="paste_title">PASTE COOKIES</span>: <span id="pasteName" style="color:#0f0">gemini</span></p>
    <p id="pasteDesc" data-i18n="paste_desc1" style="font-size:0.75em;color:#060;margin-bottom:8px">
      Paste Netscape cookie format below (tab-separated).<br>
      Firefox → <strong>cookies.txt</strong> extension → Export → Ctrl+A → Ctrl+C → Ctrl+V ↓
    </p>
    <textarea id="pasteTextarea" style="width:100%;height:200px;background:#000;color:#0f0;border:1px solid #0a0;font-family:'Courier New',monospace;font-size:0.7em;padding:8px;resize:vertical;margin-bottom:8px" placeholder=".gemini.google.com	TRUE	/	FALSE	1767225600	name	value"></textarea>
    <div id="pasteHowto" style="font-size:0.7em;color:#060;margin-bottom:8px;text-align:left;background:rgba(0,255,0,0.03);padding:8px;border:1px solid #0a0">
      <strong data-i18n="paste_howto_title" style="color:#080">How to export cookies:</strong><br>
      <span data-i18n="paste_step1">1. Install <strong>cookies.txt</strong> extension in Firefox</span><br>
      <span data-i18n="paste_step2">2. Go to gemini.google.com (or claude.ai) — make sure you're logged in</span><br>
      <span data-i18n="paste_step3">3. Click the extension icon → <strong>Export</strong></span><br>
      <span data-i18n="paste_step4">4. Copy all text (Ctrl+A → Ctrl+C) and paste above (Ctrl+V)</span><br>
      <span data-i18n="paste_step5">5. Click <strong>SAVE</strong></span>
    </div>
    <div class="btn-group">
      <button class="btn" onclick="savePastedCookies()">&gt; <span data-i18n="btn_save">SAVE</span> ✓</button>
      <button class="btn" onclick="closePasteOverlay()">&gt; <span data-i18n="btn_cancel">CANCEL</span> ✕</button>
    </div>
  </div>
</div>
  </div>
</div>

<div id="toast" class="toast"></div>

<div id="howToUse" style="text-align:center;font-size:0.9em;color:#0a0;margin-bottom:16px;line-height:1.6;border:1px solid #0a0;padding:12px;background:rgba(0,255,0,0.03)">
  <strong data-i18n="howto_title">// HOW TO USE</strong><br>
  <strong style="color:#0f0">RESTART</strong>&nbsp;<span data-i18n="howto_restart">restart the proxy</span> &nbsp;|&nbsp;
  <strong style="color:#0f0">EXPORT</strong>&nbsp;<span data-i18n="howto_export">Firefox → export cookies to terminal</span> &nbsp;|&nbsp;
  <strong style="color:#0f0">PASTE</strong>&nbsp;<span data-i18n="howto_paste">paste cookies from clipboard</span> &nbsp;|&nbsp;
  <strong style="color:#0f0">TEST</strong>&nbsp;<span data-i18n="howto_test">check if proxy responds</span><br>
  <span style="color:#060"><span data-i18n="status_legend">STATUS — 🟢 alive / 🔴 dead</span> &nbsp;|&nbsp; <span data-i18n="cookie_legend">COOKIE EXPIRY — days until cookies expire</span></span>
</div>

<h1><span data-i18n="panel_title">// PROXY CONTROL PANEL</span></h1>
<div class="sub">[ system v1.0 ] — gemini-web2api + claude-web2api</div>

<div class="status-bar">
  <span id="vpnStatus"><span class="indicator unknown" style="width:6px;height:6px"></span> <span data-i18n="vpn_checking">VPN checking...</span></span>
  <span id="timeDisplay">--:--:--</span>
  <span>|</span>
  <span><a href="https://github.com/cyberanrhy/gemini-web2api" target="_blank" style="color:#080">gemini-web2api</a></span>
  <span><a href="https://github.com/cyberanrhy/claude-web2api" target="_blank" style="color:#080">claude-web2api</a></span>
  <span>|</span>
  <span><button id="langSwitch" onclick="toggleLang()" style="background:none;border:1px solid #0a0;color:#0f0;cursor:pointer;font-family:inherit;font-size:0.8em;padding:2px 6px">RU</button></span>
</div>

<div id="proxyWarning" style="display:none;background:#330;border:1px solid #fa0;color:#fa0;padding:8px 12px;margin-bottom:16px;font-size:0.75em">
  ⚠ <span data-i18n="proxy_warning">Proxy is enabled but VPN is offline — upstream may be blocked</span>
</div>

<div class="panel-grid">
  <div class="card gemini" id="geminiCard">
    <div class="card-header">
      <span class="indicator unknown" id="geminiIndicator"></span>
      <span class="card-title"><span class="gemini">GEMINI</span></span>
    </div>
    <div class="card-body" id="geminiBody">
      <div class="row"><span class="label">PORT</span><span class="value">8081</span></div>
      <div class="row"><span class="label" data-i18n="label_status">STATUS</span><span class="value" id="geminiStatus"><span data-i18n="scanning">scanning...</span></span></div>
      <div class="row"><span class="label" data-i18n="label_rt">RESPONSE TIME</span><span class="value" id="geminiRT">--</span></div>
      <div class="row"><span class="label" data-i18n="label_cookies">COOKIE EXPIRY</span><span class="value" id="geminiCookies">--</span></div>
      <div class="row"><span class="label" data-i18n="label_upstream">UPSTREAM</span><span class="value" id="geminiUpstream">--</span></div>
      <div class="row"><span class="label" data-i18n="label_proxy">PROXY</span><span class="value" id="geminiProxyRow"><span id="geminiProxyToggle" style="cursor:pointer"></span> <input id="geminiProxyUrl" type="text" style="background:#000;color:#0f0;border:1px solid #0a0;width:180px;font-family:inherit;font-size:0.85em;padding:1px 4px" placeholder="http://..."> <button class="btn small" onclick="saveProxy('gemini')" data-i18n-title="title_save_proxy">SAVE</button></span></div>
    </div>
    <div class="actions" id="geminiActions">
      <div class="installed-actions" id="geminiInstalled">
        <button class="btn btn-sm" onclick="doAction('gemini','restart')" data-i18n-title="title_restart">&gt; <span data-i18n="btn_restart">RESTART</span></button>
        <button class="btn btn-sm" onclick="doAction('gemini','cookies')" data-i18n-title="title_export">&gt; <span data-i18n="btn_export">EXPORT</span></button>
        <button class="btn btn-sm" onclick="openPaste('gemini')" data-i18n-title="title_paste">&gt; <span data-i18n="btn_paste">PASTE</span></button>
        <button class="btn btn-sm" onclick="doTest('gemini')" data-i18n-title="title_test">[ <span data-i18n="btn_test">TEST</span> ]</button>
        <button class="btn btn-sm" onclick="doAction('gemini','install')" data-i18n-title="title_reinstall">&gt; <span data-i18n="btn_reinstall">REINSTALL</span></button>
      </div>
      <div class="missing-actions" id="geminiMissing">
        <button class="btn btn-sm" onclick="doAction('gemini','install')">&gt; <span data-i18n="btn_install">INSTALL</span></button>
      </div>
    </div>
    <div id="geminiTest" class="test-result"><span style="color:#060" data-i18n="test_hint">TEST sends "say hi in 3 words" and shows the response</span></div>
  </div>

  <div class="card claude" id="claudeCard">
    <div class="card-header">
      <span class="indicator unknown" id="claudeIndicator"></span>
      <span class="card-title"><span class="claude">CLAUDE</span></span>
    </div>
    <div class="card-body" id="claudeBody">
      <div class="row"><span class="label">PORT</span><span class="value">8082</span></div>
      <div class="row"><span class="label" data-i18n="label_status">STATUS</span><span class="value" id="claudeStatus"><span data-i18n="scanning">scanning...</span></span></div>
      <div class="row"><span class="label" data-i18n="label_rt">RESPONSE TIME</span><span class="value" id="claudeRT">--</span></div>
      <div class="row"><span class="label" data-i18n="label_cookies">COOKIE EXPIRY</span><span class="value" id="claudeCookies">--</span></div>
      <div class="row"><span class="label" data-i18n="label_upstream">UPSTREAM</span><span class="value" id="claudeUpstream">--</span></div>
      <div class="row"><span class="label" data-i18n="label_proxy">PROXY</span><span class="value" id="claudeProxyRow"><span id="claudeProxyToggle" style="cursor:pointer"></span> <input id="claudeProxyUrl" type="text" style="background:#000;color:#0f0;border:1px solid #0a0;width:180px;font-family:inherit;font-size:0.85em;padding:1px 4px" placeholder="http://..."> <button class="btn small" onclick="saveProxy('claude')" data-i18n-title="title_save_proxy">SAVE</button></span></div>
    </div>
    <div class="actions" id="claudeActions">
      <div class="installed-actions" id="claudeInstalled">
        <button class="btn btn-sm" onclick="doAction('claude','restart')" data-i18n-title="title_restart">&gt; <span data-i18n="btn_restart">RESTART</span></button>
        <button class="btn btn-sm" onclick="doAction('claude','cookies')" data-i18n-title="title_export">&gt; <span data-i18n="btn_export">EXPORT</span></button>
        <button class="btn btn-sm" onclick="openPaste('claude')" data-i18n-title="title_paste">&gt; <span data-i18n="btn_paste">PASTE</span></button>
        <button class="btn btn-sm" onclick="doTest('claude')" data-i18n-title="title_test">[ <span data-i18n="btn_test">TEST</span> ]</button>
        <button class="btn btn-sm" onclick="doAction('claude','install')" data-i18n-title="title_reinstall">&gt; <span data-i18n="btn_reinstall">REINSTALL</span></button>
      </div>
      <div class="missing-actions" id="claudeMissing">
        <button class="btn btn-sm" onclick="doAction('claude','install')">&gt; <span data-i18n="btn_install">INSTALL</span></button>
      </div>
    </div>
    <div id="claudeTest" class="test-result"><span style="color:#060" data-i18n="test_hint">TEST sends "say hi in 3 words" and shows the response</span></div>
  </div>
</div>

<div class="section-title" data-i18n="section_logs">// LOGS</div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px">
  <div>
    <div style="font-size:0.7em;color:#060;margin-bottom:4px">gemini.log</div>
    <div class="log-box" id="geminiLogBox"><span data-i18n="loading">Loading...</span></div>
  </div>
  <div>
    <div style="font-size:0.7em;color:#060;margin-bottom:4px">claude.log</div>
    <div class="log-box" id="claudeLogBox"><span data-i18n="loading">Loading...</span></div>
  </div>
</div>

<div class="section-title" data-i18n="section_quickinfo">// QUICK INFO</div>
<p style="font-size:0.75em;color:#060;line-height:1.8">
  <strong style="color:#0f0">RESTART</strong> — <span data-i18n="qi_restart">kill & restart the proxy process</span>.<br>
  <strong style="color:#0f0">EXPORT</strong> — <span data-i18n="qi_export">open Firefox → export fresh cookies via terminal script (requires Firefox + cookies.txt)</span>.<br>
  <strong style="color:#0f0">PASTE</strong> — <span data-i18n="qi_paste">paste cookies from clipboard. cookies.txt → Export → Ctrl+A → Ctrl+C → Ctrl+V → SAVE</span>.<br>
  <strong style="color:#0f0">TEST</strong> — <span data-i18n="qi_test">send test request ("say hi in 3 words") and show response + time</span>.<br>
  <strong style="color:#0f0">COOKIE EXPIRY</strong> — <span data-i18n="qi_cookie_expiry">days until the earliest cookie expires (0 = update now)</span>.<br>
  <strong style="color:#0f0">STATUS</strong> — <span data-i18n="qi_status">🟢 alive / 🔴 dead / 🟡 checking...</span><br>
  <strong style="color:#0f0">RESPONSE TIME</strong> — <span data-i18n="qi_rt">how many ms the proxy took to respond (lower is better)</span>.<br>
  <strong style="color:#0f0">VPN</strong> — <span data-i18n="qi_vpn">checks if Hiddify (127.0.0.1:12334) is running</span>.
</p>
<p style="font-size:0.7em;color:#040;margin-top:8px">
  <em data-i18n="qi_tip">Tip: if the proxy is 🔴, try RESTART or update cookies via PASTE / EXPORT.</em>
</p>

<footer>
  <a href="https://github.com/cyberanrhy/gemini-web2api">gemini-web2api</a>
  &middot;
  <a href="https://github.com/cyberanrhy/claude-web2api">claude-web2api</a>
  &middot; Proxy Control Panel v1.0
  &middot; <span style="color:#060" data-i18n="footer_refresh">F5 — refresh status</span>
</footer>

<script>
// ── Internationalization ──
const LANG = {
  en: {
    lang_switch: 'RU',
    confirm_restart: 'This will temporarily interrupt the proxy (1-2 seconds). Continue?',
    btn_confirm: 'CONFIRM',
    btn_cancel: 'CANCEL',
    paste_title: 'PASTE COOKIES',
    paste_desc1: 'Paste Netscape cookie format below (tab-separated).<br>Firefox → <strong>cookies.txt</strong> extension → Export → Ctrl+A → Ctrl+C → Ctrl+V ↓',
    paste_howto_title: 'How to export cookies:',
    paste_step1: '1. Install <strong>cookies.txt</strong> extension in Firefox',
    paste_step2: '2. Go to gemini.google.com (or claude.ai) — make sure you\'re logged in',
    paste_step3: '3. Click the extension icon → <strong>Export</strong>',
    paste_step4: '4. Copy all text (Ctrl+A → Ctrl+C) and paste above (Ctrl+V)',
    paste_step5: '5. Click <strong>SAVE</strong>',
    btn_save: 'SAVE',
    howto_title: '// HOW TO USE',
    howto_restart: 'restart the proxy',
    howto_export: 'Firefox → export cookies to terminal',
    howto_paste: 'paste cookies from clipboard',
    howto_test: 'check if proxy responds',
    status_legend: 'STATUS — 🟢 alive / 🔴 dead',
    cookie_legend: 'COOKIE EXPIRY — days until cookies expire',
    panel_title: '// PROXY CONTROL PANEL',
    vpn_checking: 'VPN checking...',
    label_status: 'STATUS',
    label_rt: 'RESPONSE TIME',
    label_cookies: 'COOKIE EXPIRY',
    scanning: 'scanning...',
    title_restart: 'Kill & restart the proxy process',
    title_export: 'Open Firefox to export cookies to terminal',
    title_paste: 'Paste cookies from clipboard (Netscape format)',
    title_test: 'Send "say hi" to check if proxy responds',
    btn_restart: 'RESTART',
    btn_export: 'EXPORT',
    btn_paste: 'PASTE',
    btn_test: 'TEST',
    btn_install: 'INSTALL',
    btn_reinstall: 'REINSTALL',
    title_reinstall: 'Reinstall from GitHub (overwrites local changes)',
    not_installed: 'NOT INSTALLED',
    test_hint: 'TEST sends "say hi in 3 words" and shows the response',
    section_logs: '// LOGS',
    loading: 'Loading...',
    section_quickinfo: '// QUICK INFO',
    qi_restart: 'kill & restart the proxy process',
    qi_export: 'open Firefox → export fresh cookies via terminal script (requires Firefox + cookies.txt)',
    qi_paste: 'paste cookies from clipboard. cookies.txt → Export → Ctrl+A → Ctrl+C → Ctrl+V → SAVE',
    qi_test: 'send test request ("say hi in 3 words") and show response + time',
    qi_cookie_expiry: 'days until the earliest cookie expires (0 = update now)',
    qi_status: '🟢 alive / 🔴 dead / 🟡 checking...',
    qi_rt: 'how many ms the proxy took to respond (lower is better)',
    qi_vpn: 'checks if Hiddify (127.0.0.1:12334) is running',
    qi_tip: 'Tip: if the proxy is 🔴, try RESTART or update cookies via PASTE / EXPORT.',
    footer_refresh: 'F5 — refresh status',
    online: 'ONLINE',
    offline: 'OFFLINE',
    checking: 'checking...',
    days: 'days',
    unknown: 'unknown',
    vpn_online: 'ONLINE',
    vpn_offline: 'OFFLINE',
    toast_paste_short: 'Paste is too short — copy the full export from cookies.txt',
    toast_saved: 'cookies saved',
    toast_error: 'Error',
    toast_network: 'Network error',
    toast_status: 'Status error',
    prompt_restart: 'The proxy will be interrupted for 1-2 seconds.',
    sending: 'sending request...',
    ok: 'OK',
    fail: 'FAIL',
    no_response: 'no response',
    no_log: '[no log file]',
    empty: '[empty]',
    label_upstream: 'UPSTREAM',
    label_proxy: 'PROXY',
    proxy_off: 'proxy OFF',
    proxy_saved: 'proxy config saved',
    proxy_no_url: 'Enter a proxy URL first',
    proxy_warning: 'Proxy is enabled but VPN is offline — upstream may be blocked',
    title_save_proxy: 'Save proxy URL and restart',
    upstream_direct: 'Direct',
    upstream_vpn: 'Via VPN',
    upstream_blocked: 'Blocked',
  },
  ru: {
    lang_switch: 'EN',
    confirm_restart: 'Прокси временно прервётся (на 1-2 секунды). Продолжить?',
    btn_confirm: 'ПОДТВЕРДИТЬ',
    btn_cancel: 'ОТМЕНА',
    paste_title: 'ВСТАВИТЬ КУКИ',
    paste_desc1: 'Вставь сюда куки из буфера (Netscape формат — строки с табами).<br>Firefox → расширение <strong>cookies.txt</strong> → Export → Ctrl+A → Ctrl+C → Ctrl+V ↓',
    paste_howto_title: 'Как получить куки:',
    paste_step1: '1. Установи расширение <strong>cookies.txt</strong> в Firefox',
    paste_step2: '2. Зайди на gemini.google.com (или claude.ai) — ты должен быть залогинен',
    paste_step3: '3. Нажми на иконку расширения → <strong>Export</strong>',
    paste_step4: '4. Выдели всё (Ctrl+A) → скопируй (Ctrl+C) → вставь выше (Ctrl+V)',
    paste_step5: '5. Нажми <strong>SAVE</strong>',
    btn_save: 'СОХРАНИТЬ',
    howto_title: '// КАК ПОЛЬЗОВАТЬСЯ',
    howto_restart: 'перезапустить прокси',
    howto_export: 'Firefox → экспорт кук в терминал',
    howto_paste: 'вставить куки из буфера',
    howto_test: 'проверить, отвечает ли прокси',
    status_legend: 'СТАТУС — 🟢 живо / 🔴 умерло',
    cookie_legend: 'COOKIE EXPIRY — через сколько дней протухнут куки',
    panel_title: '// ПАНЕЛЬ УПРАВЛЕНИЯ ПРОКСИ',
    vpn_checking: 'Проверка VPN...',
    label_status: 'СТАТУС',
    label_rt: 'ВРЕМЯ ОТВЕТА',
    label_cookies: 'СРОК КУК',
    scanning: 'сканирование...',
    title_restart: 'Убить и перезапустить прокси',
    title_export: 'Открыть Firefox для экспорта кук в терминал',
    title_paste: 'Вставить куки из буфера (Netscape формат)',
    title_test: 'Отправить "say hi" для проверки прокси',
    btn_restart: 'ПЕРЕЗАПУСК',
    btn_export: 'ЭКСПОРТ',
    btn_paste: 'ВСТАВИТЬ',
    btn_test: 'ТЕСТ',
    btn_install: 'УСТАНОВИТЬ',
    btn_reinstall: 'ПЕРЕУСТАНОВИТЬ',
    title_reinstall: 'Переустановить с GitHub (перезапишет локальные изменения)',
    not_installed: 'НЕ УСТАНОВЛЕН',
    test_hint: 'ТЕСТ отправляет "say hi in 3 words" и показывает ответ',
    section_logs: '// ЛОГИ',
    loading: 'Загрузка...',
    section_quickinfo: '// ПОДСКАЗКИ',
    qi_restart: 'перезапустить прокси (убить процесс и запустить заново)',
    qi_export: 'открыть Firefox → экспортировать свежие куки через терминальный скрипт (нужен Firefox с cookies.txt)',
    qi_paste: 'вставить куки из буфера обмена. cookies.txt → Export → Ctrl+A → Ctrl+C → Ctrl+V → SAVE',
    qi_test: 'отправить тестовый запрос ("say hi in 3 words") и показать ответ + время',
    qi_cookie_expiry: 'через сколько дней протухнут самые старые куки (0 = пора обновлять)',
    qi_status: '🟢 живо / 🔴 не отвечает / 🟡 проверка...',
    qi_rt: 'сколько миллисекунд ждал ответ (чем меньше, тем лучше)',
    qi_vpn: 'проверка, работает ли Hiddify (127.0.0.1:12334)',
    qi_tip: 'Если прокси не отвечает (🔴), попробуй ПЕРЕЗАПУСК или обнови куки через ВСТАВИТЬ / ЭКСПОРТ.',
    footer_refresh: 'F5 — обновить статус',
    online: 'РАБОТАЕТ',
    offline: 'НЕ ОТВЕЧАЕТ',
    checking: 'проверка...',
    days: 'дн.',
    unknown: 'неизвестно',
    vpn_online: 'РАБОТАЕТ',
    vpn_offline: 'НЕ РАБОТАЕТ',
    toast_paste_short: 'Слишком коротко — скопируй полный экспорт из cookies.txt',
    toast_saved: 'куки сохранены',
    toast_error: 'Ошибка',
    toast_network: 'Ошибка сети',
    toast_status: 'Ошибка статуса',
    prompt_restart: 'Прокси временно прервётся (на 1-2 секунды).',
    sending: 'отправка запроса...',
    ok: 'OK',
    fail: 'ОШИБКА',
    no_response: 'нет ответа',
    no_log: '[нет лог-файла]',
    empty: '[пусто]',
    label_upstream: 'АПСТРИМ',
    label_proxy: 'ПРОКСИ',
    proxy_off: 'прокси ВЫКЛ',
    proxy_saved: 'настройки прокси сохранены',
    proxy_no_url: 'Сначала введи URL прокси',
    proxy_warning: 'Прокси включён, но VPN не работает — апстрим может быть заблокирован',
    title_save_proxy: 'Сохранить URL прокси и перезапустить',
    upstream_direct: 'Напрямую',
    upstream_vpn: 'Через VPN',
    upstream_blocked: 'Заблокирован',
  }
};

let currentLang = localStorage.getItem('panel_lang') || 'en';
document.getElementById('langSwitch').textContent = LANG[currentLang].lang_switch;

function t(key){
  return LANG[currentLang][key] || key;
}

function applyLang(){
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    el.innerHTML = t(key);
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    const key = el.getAttribute('data-i18n-title');
    el.title = t(key);
  });
  document.getElementById('langSwitch').textContent = t('lang_switch');
}

function toggleLang(){
  currentLang = currentLang === 'en' ? 'ru' : 'en';
  localStorage.setItem('panel_lang', currentLang);
  applyLang();
  fetchStatus();
}

// ── State ──
let pendingAction = null;

// ── Matrix Rain (optional, lightweight) ──
(function(){
  const c = document.getElementById('matrix');
  if(!c)return;
  const ctx = c.getContext('2d');
  let W, H;
  function resize(){W=c.width=window.innerWidth;H=c.height=window.innerHeight}
  resize(); window.addEventListener('resize', resize);
  const cols = Math.floor(W/20);
  const drops = Array(cols).fill(1);
  function draw(){
    ctx.fillStyle='rgba(10,10,10,0.05)';
    ctx.fillRect(0,0,W,H);
    ctx.fillStyle='#0f0';
    ctx.font='15px monospace';
    for(let i=0;i<drops.length;i++){
      const ch = String.fromCharCode(0x30A0+Math.random()*96);
      ctx.fillText(ch,i*20,drops[i]*20);
      if(drops[i]*20>H && Math.random()>0.975) drops[i]=0;
      drops[i]++;
    }
  }
  setInterval(draw, 60);
})();

// ── Toast ──
function showToast(msg, isError){
  const t=document.getElementById('toast');
  t.textContent='> '+msg;
  t.style.borderColor=isError?'#f00':'#0f0';
  t.style.color=isError?'#f00':'#0f0';
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),4000);
}

// ── Confirm Dialog ──
function askConfirm(msg, cb){
  document.getElementById('confirmMsg').textContent=msg;
  document.getElementById('confirmOverlay').classList.add('show');
  pendingAction=cb;
}
function confirmAction(ok){
  document.getElementById('confirmOverlay').classList.remove('show');
  if(ok && pendingAction) pendingAction();
  pendingAction=null;
}

// ── API Calls ──
async function api(method, path, body){
  try{
    const opts={method};
    if(body) opts.body=JSON.stringify(body);
    const r=await fetch(path, opts);
    return await r.json();
  }catch(e){
    return {error: e.message};
  }
}

// ── Fetch Status ──
async function fetchStatus(){
  const data = await api('GET', '/api/status');
  if(data.error){ showToast(t('toast_status')+': '+data.error, true); return; }
  
  // Check proxy warning
  let showWarning = false;
  for(const name of ['gemini','claude']){
    const s = data[name];
    if(!s) continue;
    if(s.proxy && s.proxy !== null && !data.vpn.alive) showWarning = true;
  }
  const pw = document.getElementById('proxyWarning');
  if(pw) pw.style.display = showWarning ? 'block' : 'none';
  
  for(const name of ['gemini','claude']){
    const s = data[name];
    if(!s) continue;
    const ind = document.getElementById(name+'Indicator');
    const st = document.getElementById(name+'Status');
    const rt = document.getElementById(name+'RT');
    const ck = document.getElementById(name+'Cookies');
    const installedDiv = document.getElementById(name+'Installed');
    const missingDiv = document.getElementById(name+'Missing');
    
    if(!s.installed){
      ind.className='indicator unknown';
      st.textContent=t('not_installed');
      st.style.color='#060';
      rt.textContent='--';
      ck.textContent='--';
      if(installedDiv) installedDiv.style.display='none';
      if(missingDiv) missingDiv.style.display='flex';
    }else if(s.alive){
      ind.className='indicator alive';
      st.textContent=t('online');
      st.style.color='#0f0';
      rt.textContent=s.response_time?s.response_time+'s':t('checking');
      if(installedDiv) installedDiv.style.display='flex';
      if(missingDiv) missingDiv.style.display='none';
    }else{
      ind.className='indicator dead';
      st.textContent=t('offline');
      st.style.color='#f00';
      rt.textContent='--';
      if(installedDiv) installedDiv.style.display='flex';
      if(missingDiv) missingDiv.style.display='none';
    }
    
    if(s.installed && s.cookie_expiry_days !== null && s.cookie_expiry_days !== undefined){
      const d = s.cookie_expiry_days;
      let color = '#0f0';
      if(d < 7) color = '#fa0';
      if(d < 3) color = '#f00';
      ck.textContent = d + ' ' + t('days');
      ck.style.color = color;
    } else if(s.installed) {
      ck.textContent = t('unknown');
      ck.style.color = '#060';
    }

    // Proxy toggle
    const toggle = document.getElementById(name+'ProxyToggle');
    const urlInput = document.getElementById(name+'ProxyUrl');
    if(toggle && urlInput){
      const isOn = s.proxy && s.proxy !== null;
      toggle.textContent = isOn ? '[ON]' : '[OFF]';
      toggle.style.color = isOn ? '#0f0' : '#500';
      toggle.title = isOn ? s.proxy : t('proxy_off');
      urlInput.value = isOn ? s.proxy : '';
      toggle.onclick = () => {
        if(isOn){
          // Turn off
          saveProxyConfig(name, null);
        } else {
          // Turn on — use current URL input or default
          const url = urlInput.value.trim() || 'http://127.0.0.1:12334';
          saveProxyConfig(name, url);
        }
      };
    }

    // Upstream status — fetch from /api/upstream (polled)
    const upEl = document.getElementById(name+'Upstream');
    if(upEl){
      if(!upEl._fetching){
        upEl._fetching = true;
        fetchUpstream();
      }
    }
  }

  // Fetch upstream data separately
  async function fetchUpstream(){
    const upData = await api('GET', '/api/upstream');
    if(upData && !upData.error){
      for(const n of ['gemini','claude']){
        const el = document.getElementById(n+'Upstream');
        if(!el) continue;
        const u = upData[n];
        if(u){
          const d = u.direct ? 'Direct' : (u.proxy ? 'Via VPN' : 'Blocked');
          const color = u.direct ? '#0f0' : (u.proxy ? '#fa0' : '#f00');
          const icon = u.direct ? '✔' : (u.proxy ? '🔒' : '✖');
          el.textContent = icon+' '+d;
          el.style.color = color;
        } else {
          el.textContent = '--';
          el.style.color = '#060';
        }
      }
    }
    // Re-fetch every 30s
    setTimeout(fetchUpstream, 30000);
  }
  
  const vpn = document.getElementById('vpnStatus');
  if(data.vpn){
    const a = data.vpn.alive;
    vpn.innerHTML = '<span class="indicator '+(a?'alive':'dead')+'" style="width:6px;height:6px"></span> VPN '+(a?t('vpn_online'):t('vpn_offline'));
  }
  
  document.getElementById('timeDisplay').textContent = new Date().toLocaleTimeString();
}

// ── Fetch Logs ──
async function fetchLogs(){
  for(const name of ['gemini','claude']){
    const box = document.getElementById(name+'LogBox');
    if(!box) continue;
    const data = await api('GET', `/api/logs/${name}`);
    if(data.log === null){
      box.textContent = t('no_log');
    } else {
      box.textContent = data.log || t('empty');
    }
    box.scrollTop = box.scrollHeight;
  }
}

// ── Actions ──
async function doAction(name, action){
  const popup = action==='restart' || action==='cookies';
  if(popup){
    const actionLabel = action === 'restart' ? t('btn_restart') : t('btn_export');
    askConfirm(`${name.toUpperCase()} — ${actionLabel}?\n${t('prompt_restart')}`, doActionCb);
  }else{
    doActionCb();
  }
  async function doActionCb(){
    const btn = event&&event.target||document.querySelector(`.card.${name} button`);
    if(btn) btn.disabled=true;
    const result = await api('POST', `/api/action/${name}/${action}`);
    if(btn) btn.disabled=false;
    if(result.success){
      showToast(`${name} ${action}: OK — ${result.message||''}`, false);
      setTimeout(fetchStatus, 3000);
    }else{
      showToast(`${name} ${action}: ${t('fail')} — ${result.message||result.error||''}`, true);
    }
  }
}

// ── Test ──
async function doTest(name){
  const div = document.getElementById(name+'Test');
  div.textContent = '> '+t('sending');
  div.className = 'test-result';
  const result = await api('GET', `/api/test/${name}`);
  if(result.success){
    div.className = 'test-result success';
    div.textContent = `> ${t('ok')} (${result.time}s): "${result.response}"`;
  }else{
    div.className = 'test-result fail';
    div.textContent = `> ${t('fail')} (${result.time}s): ${result.response||t('no_response')}`;
  }
}

// ── Paste Cookies ──
let pasteTarget = 'gemini';
function openPaste(name){
  pasteTarget = name;
  document.getElementById('pasteName').textContent = name.toUpperCase();
  document.getElementById('pasteTextarea').value = '';
  document.getElementById('pasteOverlay').classList.add('show');
  setTimeout(()=>document.getElementById('pasteTextarea').focus(), 100);
}
function closePasteOverlay(){
  document.getElementById('pasteOverlay').classList.remove('show');
}
async function savePastedCookies(){
  const textarea = document.getElementById('pasteTextarea');
  const raw = textarea.value;
  if(!raw || raw.length < 20){
    showToast(t('toast_paste_short'), true);
    return;
  }
  textarea.disabled = true;
  try{
    const r = await fetch(`/api/cookies/paste/${pasteTarget}`, {method:'POST', body:raw});
    const result = await r.json();
    if(result.success){
      showToast(`${pasteTarget} ${t('toast_saved')} (${result.message})`, false);
      closePasteOverlay();
      setTimeout(fetchStatus, 1000);
    }else{
      showToast(t('toast_error')+': ${result.message}', true);
    }
  }catch(e){
    showToast(t('toast_network')+': ${e.message}', true);
  }
  textarea.disabled = false;
}

// ── Proxy Config ──
async function saveProxyConfig(name, proxyVal){
  const result = await api('POST', `/api/config/${name}`, {proxy: proxyVal});
  if(result.success){
    showToast(`${name} proxy: ${t('proxy_saved')} (${proxyVal||'OFF'})`, false);
    setTimeout(fetchStatus, 2000);
  }else{
    showToast(`${name} proxy: ${t('fail')} — ${result.message||''}`, true);
  }
}

function saveProxy(name){
  const url = document.getElementById(name+'ProxyUrl').value.trim();
  if(!url){
    showToast(t('proxy_no_url'), true);
    return;
  }
  saveProxyConfig(name, url);
}

// ── Init ──
applyLang();
fetchStatus();
fetchLogs();
setInterval(fetchStatus, 10000);
setInterval(fetchLogs, 5000);
</script>
</body>
</html>"""

# ── Main ──────────────────────────────────────────────────────────────────

class ThreadedPanelServer(ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

def serve_forever(host="0.0.0.0", port=8083):
    server = None
    while True:
        try:
            if server:
                try:
                    server.server_close()
                except:
                    pass
            server = ThreadedPanelServer((host, port), PanelHandler)
            log(f"listening on http://{host}:{port}")
            log(f"open http://127.0.0.1:{port} in your browser")
            server.serve_forever()
        except KeyboardInterrupt:
            log("shutting down")
            try:
                server.shutdown()
            except:
                pass
            return
        except Exception as e:
            log(f"server crashed ({e}), restarting in 2 seconds...")
            time.sleep(2)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Proxy Control Panel")
    parser.add_argument("--port", type=int, default=PANEL_PORT, help="Panel port (default: 8083)")
    args = parser.parse_args()
    serve_forever(port=args.port)
