#!/usr/bin/env python3
"""
gemini-web2api - Gemini Web to OpenAI API proxy.
"""
import json
import urllib.request
import urllib.parse
import time
import ssl
import sys
import uuid
import re
import os
import random
import threading
import hashlib
import argparse
import base64
import shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

__version__ = "1.1.0"

# ─── Configuration ───────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260525.09_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.5-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "cerebras_api_key": None,
    "cerebras_base_url": "https://api.cerebras.ai/v1",
}

CONFIG = dict(DEFAULT_CONFIG)

# ─── Models ──────────────────────────────────────────────────────────────────
# Mapping from JS source: MODE_CATEGORY enum (028-6eb337387583.js)
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE

MODELS = {
    "gemini-pro": {
        "mode": 3, "think": 4, "provider": "gemini",
        "desc": "Pro model alias",
    },
    "gemini-3.5-flash": {
        "mode": 1, "think": 4, "provider": "gemini",
        "desc": "Fast general-purpose model",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0, "provider": "gemini",
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4, "provider": "gemini",
        "desc": "Pro model (requires cookie for real routing)",
    },
    "gemini-auto": {
        "mode": 4, "think": 4, "provider": "gemini",
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0, "provider": "gemini",
        "desc": "Dynamic thinking with adaptive depth",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4, "provider": "gemini",
        "desc": "Lightweight fast model",
    },
    "zai-glm-4.7": {
        "provider": "cerebras", "desc": "Z.ai GLM 4.7",
    },
    "gpt-oss-120b": {
        "provider": "cerebras", "desc": "GPT-OSS 120B",
    },
}

# ─── Utilities ───────────────────────────────────────────────────────────────

def log(msg: str):
    if CONFIG["log_requests"]:
        ts = time.strftime('%H:%M:%S')
        sys.stderr.write(f"[{ts}] {msg}\n")
        sys.stderr.flush()

def load_cookie() -> tuple:
    """Load cookie from file (Netscape or JSON format). Returns (cookie_str, sapisid)."""
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        with open(cookie_file, "r") as f:
            content = f.read().strip()

        if content.startswith("{"):
            data = json.loads(content)
            return data.get("cookie", ""), data.get("sapisid")

        cookie_parts = []
        sapisid = None
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                name = parts[5]
                value = parts[6]
                cookie_parts.append(f"{name}={value}")
                if name == "SAPISID":
                    sapisid = value

        return "; ".join(cookie_parts), sapisid
    except Exception as e:
        log(f"Cookie load error: {e}")
        return "", None

def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


# ─── Proactive session refresh ─────────────────────────────────────────────

_PROACTIVE_REFRESH_INTERVAL = 900  # 15 minutes

def start_proactive_refresh():
    """Background thread: periodically refresh the Gemini session."""
    import threading
    def _run():
        while True:
            time.sleep(_PROACTIVE_REFRESH_INTERVAL)
            try:
                old_token = CONFIG.get("xsrf_token")
                gemini_init()
                if CONFIG.get("xsrf_token") != old_token:
                    log("Proactive refresh: xsrf_token updated")
                else:
                    log("Proactive refresh: OK (token unchanged)")
            except Exception as e:
                log(f"Proactive refresh failed: {e}")
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    log(f"Proactive refresh started (every {_PROACTIVE_REFRESH_INTERVAL}s)")

def gemini_init(max_attempts=3):
    """Init Gemini session: GET /app, extract SNlM0e (xsrf) and cfb2h (build_label)."""
    prefix = account_prefix()
    url = f"https://gemini.google.com{prefix}/app"

    for attempt in range(max_attempts):
        try:
            cookie_str, sapisid = load_cookie()
            headers = {
                "Referer": f"https://gemini.google.com{prefix}/app",
                "X-Same-Domain": "1",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0",
            }
            if cookie_str:
                headers["Cookie"] = cookie_str
            if sapisid:
                headers["Authorization"] = make_sapisidhash(sapisid)
            if prefix:
                headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
            
            ctx = ssl.create_default_context()
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=CONFIG["request_timeout_sec"], context=ctx)
            text = resp.read().decode("utf-8", errors="replace")
            
            m = re.search(r'"SNlM0e":"([^"]+)"', text)
            if m:
                old = CONFIG.get("xsrf_token")
                CONFIG["xsrf_token"] = m.group(1)
                if not old or old != CONFIG["xsrf_token"]:
                    log(f"Extracted SNlM0e xsrf token from /app")
            m2 = re.search(r'"cfb2h":"([^"]+)"', text)
            if m2:
                page_bl = m2.group(1)
                if page_bl != CONFIG.get("gemini_bl"):
                    log(f"Init: page has cfb2h={page_bl} (config: {CONFIG.get('gemini_bl')})")
            return True
        except Exception as e:
            log(f"Init attempt {attempt+1}/{max_attempts}: {e}")
            if attempt < max_attempts - 1:
                time.sleep(2)
    return False


def account_prefix() -> str:
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


# ─── Gemini Protocol ─────────────────────────────────────────────────────────

def gemini_stream_generate(prompt: str, model_id: int, think_mode: int) -> str:
    """Send prompt to Gemini StreamGenerate with retry."""
    cookie_str, sapisid = load_cookie()
    prefix = account_prefix()
    ctx = ssl.create_default_context()

    # Fresh xsrf from /app (same approach as working debug endpoint)
    ts_xsrf = int(time.time())
    h_xsrf = hashlib.sha1(f"{ts_xsrf} {sapisid} https://gemini.google.com".encode()).hexdigest()
    auth_xsrf = f"SAPISIDHASH {ts_xsrf}_{h_xsrf}"
    init_headers = {
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "Cookie": cookie_str,
        "Authorization": auth_xsrf,
    }
    try:
        init_req = urllib.request.Request(f"https://gemini.google.com{prefix}/app", headers=init_headers)
        init_resp = urllib.request.urlopen(init_req, timeout=CONFIG["request_timeout_sec"], context=ctx)
        init_text = init_resp.read().decode("utf-8", errors="replace")
        m = re.search(r'"SNlM0e":"([^"]+)"', init_text)
        fresh_xsrf = m.group(1) if m else ""
    except Exception as e:
        log(f"stream get xsrf failed: {e}, using CONFIG fallback")
        fresh_xsrf = CONFIG.get("xsrf_token") or ""

    body_params = {
        "at": fresh_xsrf,
        "f.req": json.dumps([None, json.dumps([[prompt, None, [None, None, None]]])])
    }
    body = urllib.parse.urlencode(body_params).encode()
    reqid = int(time.time()) % 1000000
    url = (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0",
    }
    if prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])

    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    log(f"upstream POST {model_id} len={len(body)}")
    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            ctx = ssl.create_default_context()
            proxy = CONFIG.get("proxy")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
            else:
                ctx.check_hostname = True
                ctx.verify_mode = ssl.CERT_REQUIRED
                resp = urllib.request.urlopen(req, timeout=CONFIG["request_timeout_sec"], context=ctx)
            raw = resp.read().decode("utf-8", errors="replace")
            return raw
        except Exception as e:
            err_body = b""
            if hasattr(e, 'read'):
                err_body = e.read()
                err_str = err_body.decode('utf-8', errors='replace')
                desc = "Authorization error (401)" if "401" in str(e) else "Access denied (403)" if "403" in str(e) else "Bad request (400)" if "400" in str(e) else "Technical error"
                log(f"Upstream error: {desc}. Detail: {err_str}")
            elif hasattr(e, 'response') and e.response is not None:
                err_body = e.response.content
                err_str = err_body.decode('utf-8', errors='replace')[:500]
                log(f"Upstream error: {e}. Detail: {err_str}")
            # Try to extract new xsrf token from error response
            if b'"48448350"' in err_body and b'"xsrf"' in err_body:
                m = re.search(rb'"xsrf","([^"]+)"', err_body)
                if m:
                    new_xsrf = m.group(1).decode()
                    log(f"Extracted new xsrf token from error response")
                    CONFIG["xsrf_token"] = new_xsrf
                    body_params["at"] = new_xsrf
                    body = urllib.parse.urlencode(body_params).encode()
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err


def gemini_stream_generate_iter(prompt: str, model_id: int, think_mode: int):
    """Send prompt and yield incremental text deltas."""
    cookie_str, sapisid = load_cookie()
    prefix = account_prefix()
    ctx = ssl.create_default_context()

    # Fresh xsrf from /app
    ts_xsrf = int(time.time())
    h_xsrf = hashlib.sha1(f"{ts_xsrf} {sapisid} https://gemini.google.com".encode()).hexdigest()
    auth_xsrf = f"SAPISIDHASH {ts_xsrf}_{h_xsrf}"
    init_headers = {
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "Cookie": cookie_str,
        "Authorization": auth_xsrf,
    }
    try:
        init_req = urllib.request.Request(f"https://gemini.google.com{prefix}/app", headers=init_headers)
        init_resp = urllib.request.urlopen(init_req, timeout=CONFIG["request_timeout_sec"], context=ctx)
        init_text = init_resp.read().decode("utf-8", errors="replace")
        m = re.search(r'"SNlM0e":"([^"]+)"', init_text)
        fresh_xsrf = m.group(1) if m else CONFIG.get("xsrf_token") or ""
    except Exception:
        fresh_xsrf = CONFIG.get("xsrf_token") or ""

    body_params = {
        "at": fresh_xsrf,
        "f.req": json.dumps([None, json.dumps([[prompt, None, [None, None, None]]])])
    }
    body = urllib.parse.urlencode(body_params)
    reqid = int(time.time()) % 1000000
    prefix = account_prefix()
    url = (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0",
    }
    if prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    prev_text = ""

    if not HAS_HTTPX:
        # Fallback: non-streaming with urllib
        raw = gemini_stream_generate(prompt, model_id, think_mode)
        text = extract_response_text(raw)
        if text:
            yield text
        return

    if HAS_HTTPX:
        proxy = CONFIG.get("proxy")
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True) as client:
            with client.stream("POST", url, content=body, headers=headers) as resp:
                buf = ""
                for chunk in resp.iter_text():
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        if '"wrb.fr"' not in line or len(line) < 200:
                            continue
                        try:
                            arr = json.loads(line)
                            inner_str = arr[0][2]
                            if not inner_str or len(inner_str) < 50:
                                continue
                            inner2 = json.loads(inner_str)
                            if isinstance(inner2, list) and len(inner2) > 4 and inner2[4]:
                                for part in inner2[4]:
                                    if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                                        for t in part[1]:
                                            if isinstance(t, str) and len(t) > len(prev_text):
                                                delta = t[len(prev_text):]
                                                delta = clean_gemini_text(delta)
                                                if delta:
                                                    yield delta
                                                prev_text = t
                        except (json.JSONDecodeError, IndexError, TypeError):
                            pass


def clean_gemini_text(text: str) -> str:
    """Remove internal code execution artifacts."""
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    return text.strip()


def strip_reasoning_content(messages: list) -> list:
    """Remove reasoning_content from messages for Cerebras compatibility."""
    cleaned = []
    for msg in messages:
        if isinstance(msg, dict) and "reasoning_content" in msg:
            msg = {k: v for k, v in msg.items() if k != "reasoning_content"}
        cleaned.append(msg)
    return cleaned


def _parse_cerebras_error(resp) -> str:
    """Extract user-friendly error message from Cerebras response."""
    try:
        data = resp.json()
        err = data.get("error", {})
        msg = err.get("message", "")
        code = err.get("code", "")
        typ = err.get("type", "")
        
        if resp.status_code == 402:
            return "Cerebras: insufficient balance (402 Payment Required). Top up your Cerebras account."
        elif resp.status_code == 401:
            return "Cerebras: invalid API key (401 Unauthorized). Check your key in config."
        elif resp.status_code == 404:
            return f"Cerebras: model not found (404). {msg}"
        elif resp.status_code == 429:
            return "Cerebras: rate limit exceeded (429). Wait and retry."
        elif resp.status_code >= 500:
            return f"Cerebras: server error ({resp.status_code}). Retry later."
        elif msg:
            return f"Cerebras: {msg}"
        else:
            return f"Cerebras: error {resp.status_code}"
    except:
        return f"Cerebras: error {resp.status_code}"


def cerebras_chat_completions(req: dict):
    """Proxy request to Cerebras API (non-streaming)."""
    import httpx
    
    api_key = CONFIG.get("cerebras_api_key")
    base_url = CONFIG.get("cerebras_base_url", "https://api.cerebras.ai/v1")
    
    if not api_key:
        raise ValueError("Cerebras API key not configured")
    
    # Strip reasoning_content from messages
    req = dict(req)
    req["messages"] = strip_reasoning_content(req.get("messages", []))
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    url = f"{base_url}/chat/completions"
    
    proxy = CONFIG.get("proxy")
    transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
    
    with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"]) as client:
        resp = client.post(url, json=req, headers=headers)
        if resp.status_code != 200:
            raise ValueError(_parse_cerebras_error(resp))
        return resp.json()


def cerebras_chat_completions_stream(req: dict):
    """Proxy streaming request to Cerebras API."""
    import httpx
    
    api_key = CONFIG.get("cerebras_api_key")
    base_url = CONFIG.get("cerebras_base_url", "https://api.cerebras.ai/v1")
    
    if not api_key:
        raise ValueError("Cerebras API key not configured")
    
    # Strip reasoning_content from messages
    req = dict(req)
    req["messages"] = strip_reasoning_content(req.get("messages", []))
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    url = f"{base_url}/chat/completions"
    
    proxy = CONFIG.get("proxy")
    transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
    
    with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"]) as client:
        with client.stream("POST", url, json=req, headers=headers) as resp:
            if resp.status_code != 200:
                error_msg = _parse_cerebras_error(resp)
                yield f"data: {json.dumps({'error': {'message': error_msg}})}\n\n"
                return
            for line in resp.iter_lines():
                yield line


def extract_response_text(raw: str) -> str:
    """Parse StreamGenerate response to extract final text."""
    texts = []
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            arr = json.loads(line)
            inner_str = arr[0][2]
            if not inner_str or len(inner_str) < 50:
                continue
            inner = json.loads(inner_str)
            if isinstance(inner, list) and len(inner) > 4 and inner[4]:
                for part in inner[4]:
                    if isinstance(part, list) and len(part) > 1 and part[1]:
                        if isinstance(part[1], list):
                            for t in part[1]:
                                if isinstance(t, str) and len(t) > 0:
                                    texts.append(t)
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
    text = ""
    for t in reversed(texts):
        if t.strip():
            text = t
            break
    return clean_gemini_text(text)


# ─── OpenAI Format Helpers ───────────────────────────────────────────────────

MAX_CONTEXT_MSGS = 6
MAX_TOOL_CHARS = 4000

def trim_context(messages: list) -> list:
    """Trim messages to prevent oversized context payloads."""
    system = [m for m in messages if m.get("role") == "system"]
    others = [m for m in messages if m.get("role") != "system"]
    kept = others[-MAX_CONTEXT_MSGS:] if len(others) > MAX_CONTEXT_MSGS else others
    result = system + kept
    for m in result:
        content = m.get("content", "")
        if isinstance(content, str) and len(content) > MAX_TOOL_CHARS:
            m["content"] = content[:MAX_TOOL_CHARS] + f"\n[... truncated {len(content)-MAX_TOOL_CHARS} chars]"
    return result

def messages_to_prompt(messages: list, tools: list = None) -> str:
    """Convert OpenAI messages to prompt string."""
    parts = []
    if tools:
        tool_defs = []
        for tool in tools:
            fn = tool.get("function", tool) if tool.get("type") == "function" else tool
            tool_defs.append({
                "name": fn.get("name", tool.get("name", "")),
                "description": fn.get("description", tool.get("description", "")),
                "parameters": fn.get("parameters", tool.get("parameters", {})),
            })
        if tool_defs:
            parts.append(
                "[System instruction]: You MUST use the tools below to complete the user's request. "
                "You have FULL access to these tools — they are not blocked.\n"
                "To call a tool, respond with EXACTLY:\n"
                '```tool_call\n{"name": "tool_name", "arguments": {...}}\n```\n'
                "Then wait for the result before continuing.\n\n"
                f"Available tools:\n{json.dumps(tool_defs, indent=2)}"
            )
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content
                if c.get("type") in ("text", "input_text")
            )
        if role == "system":
            parts.append(f"[System instruction]: {content}")
        elif role == "assistant":
            if msg.get("tool_calls"):
                tc_strs = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    tc_strs.append(
                        f'```tool_call\n{{"name": "{fn.get("name")}", '
                        f'"arguments": {fn.get("arguments", "{}")}}}\n```'
                    )
                parts.append(f"[Assistant]: {content or ''}\n" + "\n".join(tc_strs))
            else:
                parts.append(f"[Assistant]: {content}")
        elif role == "tool":
            parts.append(f"[Tool result for {msg.get('name', '')}]: {content}")
        else:
            parts.append(content if content else "")
    return "\n\n".join(p for p in parts if p)


def parse_tool_calls(text: str) -> tuple:
    """Extract tool_call blocks. Returns (clean_text, tool_calls_list)."""
    tool_calls = []
    pattern = r'```tool_call\s*\n(.*?)```'
    for match in re.findall(pattern, text, re.DOTALL):
        try:
            data = json.loads(match.strip())
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": data["name"],
                    "arguments": json.dumps(data.get("arguments", {}), ensure_ascii=False),
                },
            })
        except (json.JSONDecodeError, KeyError):
            pass
    clean = re.sub(pattern, '', text, flags=re.DOTALL).strip()
    return clean, tool_calls


# ─── HTTP Handler ────────────────────────────────────────────────────────────

class GeminiHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if "GET /v1/models" in fmt % args:
            return
        log(fmt % args)

    def _reason(self, code):
        return {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
                404: "Not Found", 500: "Internal Server Error", 502: "Bad Gateway"}.get(code, "OK")

    def _sendall(self, status, content_type, body, extra_headers=None):
        if self.path != "/v1/models":
            log(f"<- {self.command} {self.path} -> {status}")
        header = f"HTTP/1.1 {status} {self._reason(status)}\r\n"
        header += f"Content-Type: {content_type}\r\n"
        header += "Access-Control-Allow-Origin: *\r\n"
        header += "Connection: close\r\n"
        if extra_headers:
            for k, v in extra_headers.items():
                header += f"{k}: {v}\r\n"
        header += "\r\n"
        full = header.encode() + (body if isinstance(body, bytes) else body.encode())
        try:
            self.request.sendall(full)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self._sendall(status, "application/json", body, {"Content-Length": str(len(body))})

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        auth = self.headers.get("Authorization", "")
        key = auth[7:] if auth.startswith("Bearer ") else self.headers.get("x-api-key", "")
        return key in keys

    def do_OPTIONS(self):
        self._sendall(204, "text/plain", "", {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*"
        })

    def do_GET(self):
        try:
            if self.path.startswith("/v1/") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            if self.path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif self.path.startswith("/v1beta/models"):
                self._handle_google_models_list()
            elif self.path == "/":
                self.send_json({"status": "ok", "version": __version__,
                                "models": list(MODELS.keys())})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"GET error: {e}")

    def do_POST(self):
        try:
            if self.path.startswith("/v1/") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            if self.path == "/v1/chat/completions":
                self.handle_chat(body)
            elif self.path == "/v1/responses":
                self.handle_responses(body)
            elif ":generateContent" in self.path:
                self._handle_google_generate(body, stream=False)
            elif ":streamGenerateContent" in self.path:
                self._handle_google_generate(body, stream=True)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_json({"error": {"message": str(e)}}, 500)
            except:
                pass

    def _resolve_model(self, model_name):
        think_override = None
        if "@think=" in model_name:
            model_name, think_str = model_name.rsplit("@think=", 1)
            think_override = int(think_str)
        cfg = MODELS.get(model_name)
        if not cfg:
            return None, None, None, None, f"Unknown model: {model_name}"
        provider = cfg.get("provider", "gemini")
        return model_name, cfg.get("mode"), (think_override if think_override is not None else cfg.get("think")), provider, None

    def _call_gemini(self, prompt, model_id, think_mode, tools):
        # Human-like think time based on prompt length
        words = len(prompt.split())
        think_delay = min(max(words / 50, 0.3), 4.0)  # ~1-4s for typical messages
        jitter = random.uniform(0.7, 1.3)
        total_delay = round(think_delay * jitter, 1)
        if total_delay > 0.5:
            log(f"Think delay {total_delay}s ({words} words)")
            time.sleep(total_delay)

        RATE_LIMIT_CODES = {"1150", "1152"}
        RATE_LIMIT_E_CODES = {3, 4, 8, 29, 32, 47}
        SESSION_EXPIRY_CODES = {"1060", "1095", "1155"}
        SESSION_EXPIRY_E_CODES = {38, 40, 49, 52}
        RETRY_DELAYS = [15, 60, 180, 360]
        recovered = False

        for attempt in range(len(RETRY_DELAYS) + 1):
            raw = gemini_stream_generate(prompt, model_id, think_mode)
            err = None
            code = None
            ERROR_CODES = {
                "1050": "Gemini temporarily unavailable (maintenance).",
                "1060": "Gemini session expired — need fresh cookies.",
                "1095": "Session blocked — try switching VPN node or refreshing cookies.",
            "1096": "CAPTCHA required — open Gemini in browser and verify you are human.",
                "1150": "Too many requests — Gemini rate limit exceeded.",
                "1152": "Too many requests — Gemini rate limit exceeded.",
                "1155": "Session expired or invalid — refresh cookies or re-authenticate.",
                "1160": "Response blocked by Gemini safety filter.",
                "1170": "Model unavailable for this request.",
                "1180": "Gemini network error — check your connection.",
                "1190": "Internal Gemini server error.",
                "1200": "Gemini timeout — response took too long.",
                "1210": "Unknown Gemini response — API may have changed.",
            }
            # First check for valid content - "e" at end is just telemetry
            has_content = bool(re.search(r'"wrb\.fr"', raw))
            if "BardErrorInfo" in raw:
                m = re.search(r'BardErrorInfo",\[(\d+)', raw)
                if m:
                    code = m.group(1)
                    err = ERROR_CODES.get(code, f"Gemini error {code} — see proxy logs")
            elif '"48448350"' in raw and '"xsrf"' in raw:
                m = re.search(r'"xsrf","([^"]+)"', raw)
                if m:
                    new_xsrf = m.group(1)
                    CONFIG["xsrf_token"] = new_xsrf
                    log(f"Extracted xsrf token from error response, retrying...")
                    if attempt < len(RETRY_DELAYS) + 1:
                        time.sleep(1)
                        continue
                err = "Authorization error — cookie/xsrf_token expired."
            elif '"xsrf"' in raw:
                err = "Authorization error — cookie/xsrf_token expired."
            elif '"e",' in raw and not has_content:
                m = re.search(r'\["e",(\d+)', raw)
                if m:
                    ecode = int(m.group(1))
                    E_CODES = {
                        1: "Unknown Gemini error.",
                        2: "Invalid request to Gemini.",
                        3: "Too many requests — Gemini rate limit exceeded.",
                        4: "Temporary Gemini error — retry later.",
                        5: "Gemini network error.",
                        8: "Gemini server temporarily overloaded — retry later.",
                        9: "Internal Gemini server error.",
                        10: "Unknown Gemini error.",
                        33: "CAPTCHA required — open Gemini in browser.",
                        37: "CAPTCHA required — open Gemini in browser.",
                        38: "Session invalid — refresh cookies.",
                        40: "Session invalid — refresh cookies.",
                        47: "Gemini server temporarily unavailable.",
                        49: "Session invalid — refresh cookies.",
                        52: "Session invalid — refresh cookies.",
                        55: "CAPTCHA required — open Gemini in browser.",
                        78: "Confirmation required — open Gemini in browser.",
                    }
                    err = E_CODES.get(ecode, f"Gemini error (code e:{ecode}) — see proxy logs")
                    if ecode in RATE_LIMIT_E_CODES:
                        code = f"e:{ecode}"
            if err and code and (code in RATE_LIMIT_CODES or (isinstance(code, str) and code.startswith("e:") and int(code.split(":")[1]) in RATE_LIMIT_E_CODES)):
                if attempt < len(RETRY_DELAYS):
                    base_delay = RETRY_DELAYS[attempt]
                    jitter = random.uniform(0.7, 1.3)
                    delay = round(base_delay * jitter, 1)
                    log(f"Rate limit, retry {attempt+1}/{len(RETRY_DELAYS)} in {delay}s...")
                    time.sleep(delay)
                    continue
            # Auto-recover session expiry (one retry only)
            if err and code and (code in SESSION_EXPIRY_CODES or (isinstance(code, str) and code.startswith("e:") and int(code.split(":")[1]) in SESSION_EXPIRY_E_CODES)):
                if not recovered:
                    recovered = True
                    log(f"Session expired ({code}), attempting auto-recovery...")
                    try:
                        gemini_init()
                        log("Session recovery OK, retrying request...")
                        time.sleep(2)
                        continue
                    except Exception as recoverr:
                        log(f"Session recovery failed: {recoverr}")
                else:
                    log(f"Session recovery already attempted, giving up.")
            if err:
                return err, None
            break

        text = extract_response_text(raw)
        tool_calls = None
        if tools and text:
            text, tool_calls = parse_tool_calls(text)
        return text or "", tool_calls

    def handle_chat(self, body: bytes):
        req = json.loads(body)
        model_name, model_id, think_mode, provider, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        log(f"chat: model={req.get('model')} provider={provider} msgs={len(req.get('messages', []))} stream={req.get('stream')} tools={bool(req.get('tools'))}")
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        if provider == "cerebras":
            self.handle_cerebras_chat(req, model_name)
            return

        tools = req.get("tools")
        msgs = trim_context(req.get("messages", []))
        prompt = messages_to_prompt(msgs, tools)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return

        stream = req.get("stream", False)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        if stream and not tools:
            # True streaming: forward chunks as they arrive
            try:
                self._sendall(200, "text/event-stream", "", {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})
                for delta_text in gemini_stream_generate_iter(prompt, model_id, think_mode):
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                             "model": model_name, "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]}
                    payload = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                    try:
                        self.request.sendall(payload)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                # Final chunk
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.request.sendall(f"data: {json.dumps(chunk)}\n\n".encode())
                self.request.sendall(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Stream error: {e}")
            return

        # Human-like delay when last message is a tool result
        msgs = req.get("messages", [])
        if msgs and msgs[-1].get("role") in ("tool",):
            delay = random.uniform(4.0, 15.0)
            log(f"Tool result detected, waiting {delay:.1f}s to simulate human...")
            time.sleep(delay)

        # Non-streaming (or tool calling which needs full response)
        raw = gemini_stream_generate(prompt, model_id, think_mode)
        if not re.search(r'"wrb\.fr"', raw):
            self.send_json({"error": "Gemini upstream error", "raw_first200": raw[:200]}, 502)
            return
        text = extract_response_text(raw)
        tool_calls = None
        if tools and text:
            text, tool_calls = parse_tool_calls(text)

        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            # Stream mode with tools: send as single chunk (need full parse for tool_calls)
            self._sendall(200, "text/event-stream", "", {"Cache-Control": "no-cache"})
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": model_name, "choices": [{"index": 0, "delta": msg, "finish_reason": finish}]}
            self.request.sendall(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.request.sendall(b"data: [DONE]\n\n")
        else:
            self.send_json({
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text)//4,
                          "total_tokens": (len(prompt)+len(text))//4},
            })

    def handle_cerebras_chat(self, req: dict, model_name: str):
        """Handle chat completions via Cerebras API."""
        stream = req.get("stream", False)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        try:
            if stream:
                self._sendall(200, "text/event-stream", "", {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})
                for line in cerebras_chat_completions_stream(req):
                    if line.strip():
                        # Cerebras returns SSE format, forward as-is
                        self.request.sendall(f"{line}\n".encode())
                self.request.sendall(b"data: [DONE]\n\n")
            else:
                resp = cerebras_chat_completions(req)
                # Ensure response has correct format
                resp["id"] = cid
                resp["model"] = model_name
                self.send_json(resp)
        except Exception as e:
            log(f"Cerebras error: {e}")
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)

    def handle_responses(self, body: bytes):
        """OpenAI Responses API for Codex CLI compatibility."""
        req = json.loads(body)
        model_name, model_id, think_mode, provider, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        input_items = req.get("input", [])
        tools = req.get("tools")

        messages = []
        if req.get("instructions"):
            messages.append({"role": "system", "content": req["instructions"]})
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
        elif isinstance(input_items, list):
            for item in input_items:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    if item.get("type") == "function_call_output":
                        messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                                         "name": item.get("name", ""), "content": item.get("output", "")})
                    elif item.get("role") == "assistant" or (item.get("type") == "message" and item.get("role") == "assistant"):
                        cp = item.get("content", [])
                        text_acc, tc_list = "", []
                        if isinstance(cp, list):
                            for c in cp:
                                if isinstance(c, dict):
                                    if c.get("type") == "output_text": text_acc += c.get("text", "")
                                    elif c.get("type") == "function_call": tc_list.append(c)
                        elif isinstance(cp, str):
                            text_acc = cp
                        m = {"role": "assistant", "content": text_acc or None}
                        if tc_list:
                            m["tool_calls"] = [{"id": tc.get("call_id", f"call_{i}"), "type": "function",
                                                "function": {"name": tc.get("name",""), "arguments": tc.get("arguments","{}")}}
                                               for i, tc in enumerate(tc_list)]
                        messages.append(m)
                    else:
                        role = item.get("role", "user")
                        content = item.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(c.get("text", "") for c in content if c.get("type") in ("text", "input_text"))
                        messages.append({"role": role, "content": content})

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        prompt = messages_to_prompt(messages, tools)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        messages = trim_context(messages)
        prompt = messages_to_prompt(messages, tools)

        # Human-like delay when last message is a tool result
        if messages and messages[-1].get("role") in ("tool", "function_call_output"):
            delay = random.uniform(4.0, 15.0)
            log(f"Tool result detected, waiting {delay:.1f}s to simulate human...")
            time.sleep(delay)

        try:
            text, tool_calls = self._call_gemini(prompt, model_id, think_mode, tools)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        rid = f"resp_{uuid.uuid4().hex[:16]}"
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
        if text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": text or "", "annotations": []}]})

        if req.get("stream"):
            self._sendall(200, "text/event-stream", "", {"Cache-Control": "no-cache"})
            ev = {"type": "response.created", "response": {"id": rid, "object": "response", "status": "in_progress", "model": model_name, "output": []}}
            self.request.sendall(f"event: response.created\ndata: {json.dumps(ev)}\n\n".encode())
            for item in output:
                if item["type"] == "function_call":
                    ev = {"type": "response.function_call_arguments.done", "item_id": item["id"], "call_id": item["call_id"], "name": item["name"], "arguments": item["arguments"]}
                    self.request.sendall(f"event: response.function_call_arguments.done\ndata: {json.dumps(ev)}\n\n".encode())
                elif item["type"] == "message":
                    for ci, cp in enumerate(item["content"]):
                        ev = {"type": "response.output_text.done", "item_id": item["id"], "content_index": ci, "text": cp["text"]}
                        self.request.sendall(f"event: response.output_text.done\ndata: {json.dumps(ev)}\n\n".encode())
            resp_obj = {"id": rid, "object": "response", "status": "completed", "model": model_name, "output": output,
                        "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text)//4, "total_tokens": (len(prompt)+len(text))//4}}
            self.request.sendall(f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': resp_obj})}\n\n".encode())
        else:
            self.send_json({"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                            "model": model_name, "output": output,
                            "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text)//4, "total_tokens": (len(prompt)+len(text))//4}})


    # ─── Google Native API (Gemini CLI compatible) ────────────────────────────

    def _parse_google_model_from_path(self):
        """Extract model name from /v1beta/models/{model}:method path."""
        m = re.match(r'/v1beta/models/([^:?]+)', self.path)
        if m:
            return m.group(1)
        return None

    def _handle_google_models_list(self):
        """GET /v1beta/models — Google AI format model list."""
        models = []
        for name, cfg in MODELS.items():
            models.append({
                "name": f"models/{name}",
                "displayName": name,
                "description": cfg["desc"],
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            })
        self.send_json({"models": models})

    def _google_contents_to_prompt(self, req: dict) -> str:
        """Convert Google API contents format to prompt string."""
        parts = []
        sys_inst = req.get("systemInstruction")
        if sys_inst:
            sys_parts = sys_inst.get("parts", [])
            sys_text = " ".join(p.get("text", "") for p in sys_parts if p.get("text"))
            if sys_text:
                parts.append(f"[System instruction]: {sys_text}")

        for content in req.get("contents", []):
            role = content.get("role", "user")
            text_parts = []
            for p in content.get("parts", []):
                if p.get("text"):
                    text_parts.append(p["text"])
            text = " ".join(text_parts)
            if role == "model":
                parts.append(f"[Assistant]: {text}")
            else:
                parts.append(text)
        return "\n\n".join(p for p in parts if p)

    def _handle_google_generate(self, body: bytes, stream: bool):
        """Handle Google native generateContent / streamGenerateContent."""
        req = json.loads(body)
        model_name = self._parse_google_model_from_path()
        if not model_name:
            self.send_json({"error": {"message": "model not specified in path"}}, 400)
            return

        model_name, model_id, think_mode, err = self._resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        prompt = self._google_contents_to_prompt(req)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty content"}}, 400)
            return

        try:
            text, _ = self._call_gemini(prompt, model_id, think_mode, None)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        candidate = {
            "content": {"parts": [{"text": text or ""}], "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text) // 4,
            "totalTokenCount": (len(prompt) + len(text)) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        if stream:
            self._sendall(200, "text/event-stream", "", {"Cache-Control": "no-cache"})
            self.request.sendall(f"data: {json.dumps(response_obj)}\n\n".encode())
        else:
            self.send_json(response_obj)


# ─── Main ────────────────────────────────────────────────────────────────────

def load_config(path: str):
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
        log(f"Config loaded: {path}")


def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None, help="Path to cookie file")
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--version", action="version", version=f"gemini-web2api {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("GEMINI_WEB2API_CONFIG")
    if not config_path:
        for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
            if os.path.exists(p):
                config_path = p
                break
    load_config(config_path)

    if not config_path:
        example = "./config.json.example"
        if os.path.exists(example):
            shutil.copy(example, "./config.json")
            config_path = "./config.json"
            log("Created config.json from config.json.example")
            load_config(config_path)
        else:
            print("ERROR: config.json not found.")
            print("  Run: cp config.json.example config.json")
            print("  Then run again.")
            sys.exit(1)

    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy

    cookie_file = CONFIG.get("cookie_file") or "./cookie.txt"
    if not os.path.exists(cookie_file):
        print(f"ERROR: cookie file not found at '{cookie_file}'.")
        print("  1. Open https://gemini.google.com/app in Firefox and log in")
        print("  2. Install 'cookies.txt' extension (https://addons.mozilla.org/firefox/addon/cookies-txt/)")
        print("  3. Click the extension → Export → save as the file above")
        print(f"  4. Then run again")
        sys.exit(1)
    CONFIG["cookie_file"] = cookie_file

    if not CONFIG.get("xsrf_token"):
        log("xsrf_token not set — trying auto-init...")
        try:
            gemini_init()
        except Exception as e:
            print(f"ERROR: Could not auto-init xsrf_token: {e}")
            print("  Set xsrf_token manually in config.json:")
            print("  1. Open https://gemini.google.com/app in Firefox")
            print("  2. View page source (Ctrl+U), search for 'SNlM0e'")
            print("  3. Copy the value into config.json as 'xsrf_token'")
            sys.exit(1)
    gemini_init()
    start_proactive_refresh()

    class ThreadedServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    port = CONFIG["port"]
    server = ThreadedServer((CONFIG["host"], port), GeminiHandler)
    print(f"gemini-web2api v{__version__}")
    print(f"  Listening: http://0.0.0.0:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    print(f"  Cookie:    {'yes (' + CONFIG['cookie_file'] + ')' if CONFIG.get('cookie_file') else 'none (anonymous)'}")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'none (uses system env HTTP_PROXY/HTTPS_PROXY)'}")
    print(f"  Retry:     {CONFIG['retry_attempts']}x / {CONFIG['retry_delay_sec']}s")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
