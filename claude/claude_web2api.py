#!/usr/bin/env python3
"""Claude.ai -> OpenAI-compatible proxy (port 8082)"""

import json, os, sys, time, uuid, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

from curl_cffi.requests import Session

CONFIG = {}
COOKIE_STRING = ""
ORGANIZATION_ID = None
CLAUDE_BASE = "https://claude.ai"

def log(msg):
    print(f"[claude-proxy] {msg}", file=sys.stderr, flush=True)

def load_config():
    global CONFIG
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(path) as f:
        CONFIG = json.load(f)
    log(f"config loaded: port={CONFIG.get('port', 8082)}")

def load_cookies():
    global COOKIE_STRING
    path = CONFIG.get("cookie_file") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "cookie_claude.txt")
    if not os.path.exists(path):
        log(f"cookie file not found: {path}")
        return False
    cookies = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies.append(f"{parts[5]}={parts[6]}")
    COOKIE_STRING = "; ".join(cookies)
    log(f"loaded {len(cookies)} cookies from {path}")
    return bool(cookies)

def _session():
    s = Session(impersonate="chrome110")
    s.headers.update({
        "User-Agent": CONFIG.get("user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.5",
        "DNT": "1",
    })
    if CONFIG.get("proxy"):
        s.proxies = {"http": CONFIG["proxy"], "https": CONFIG["proxy"]}
    s.trust_env = False
    return s

def claude_req(method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("Cookie", COOKIE_STRING)
    s = _session()
    # Improved Debug: log the request details
    if method == "POST":
        log(f"DEBUG: {method} {CLAUDE_BASE}{path} JSON={json.dumps(kwargs.get('json'))}")
    else:
        log(f"DEBUG: {method} {CLAUDE_BASE}{path}")
    resp = s.request(method, f"{CLAUDE_BASE}{path}", headers=headers, **kwargs)
    # Debug: log the response status and truncated content
    log(f"DEBUG: Response {resp.status_code} {resp.text[:200]}")
    return resp

def claude_req_retry(method, path, retries=5, backoff=1, **kwargs):
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = claude_req(method, path, **kwargs)
            if resp.status_code == 429 and attempt < retries:
                delay = backoff * (2 ** attempt) + 1
                log(f"claude_req retry {attempt+1}/{retries} (429, wait {delay}s)")
                time.sleep(delay)
                continue
            return resp
        except Exception as e:
            last_err = e
            err_str = str(e)
            should_retry = any(x in err_str for x in ["TLS", "SSL", "connect", "35", "56", "reset", "timeout"])
            if attempt < retries and should_retry:
                delay = backoff * (2 ** attempt)
                log(f"claude_req retry {attempt+1}/{retries} (wait {delay}s): {err_str[:100]}")
                time.sleep(delay)
                continue
            raise
    raise last_err


_last_request_time = 0
def rate_limit():
    """Ensure at least 1.5s between requests to avoid 429."""
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)
    _last_request_time = time.time()


def get_organization_id():
    rate_limit()
    resp = claude_req_retry("GET", "/api/organizations", timeout=30)
    if resp.status_code != 200:
        log(f"failed to get org id: {resp.status_code}")
        return None
    data = resp.json()
    if data and "uuid" in data[0]:
        return data[0]["uuid"]
    return None

def create_chat(org_id):
    rate_limit()
    resp = claude_req_retry("POST", f"/api/organizations/{org_id}/chat_conversations",
                            json={"name": ""}, timeout=30)
    return resp.json().get("uuid") if resp.status_code in (200, 201) else None

def delete_chat(org_id, chat_id):
    try:
        claude_req("DELETE", f"/api/organizations/{org_id}/chat_conversations/{chat_id}", timeout=10)
    except:
        pass

def format_prompt(messages):
    parts = []
    # Check if tools are available
    has_tools = False
    for msg in messages:
        tc = msg.get("tool_calls")
        if tc:
            has_tools = True
            break
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content") or ""
        if role == "system":
            parts.append(f"System: {content}")
        elif role == "user":
            parts.append(f"Human: {content}")
        elif role == "assistant":
            tc = msg.get("tool_calls")
            if tc and not content:
                tool_blocks = []
                for t in tc:
                    name = t.get("function", {}).get("name", "unknown")
                    args = t.get("function", {}).get("arguments", "{}")
                    tool_blocks.append(f"<invoke tool=\"{name}\">\n{args}\n</invoke>")
                parts.append("Assistant:\n" + "\n".join(tool_blocks))
            else:
                parts.append(f"Assistant: {content}")
        elif role == "tool":
            name = msg.get("name", "")
            tid = msg.get("tool_call_id", "")
            label = f" (tool: {name})" if name else f" (id: {tid})" if tid else ""
            parts.append(f"Human: [Tool result{label}]\n{content}")
    parts.append("Assistant:")
    return "\n\n".join(parts)

def get_timezone():
    try:
        from datetime import datetime
        return datetime.now().astimezone().tzname()
    except:
        return "Europe/Moscow"

def iter_sse_lines(resp):
    """Buffered SSE line iterator for curl_cffi streaming responses"""
    buf = ""
    for chunk in resp.iter_content():
        if chunk:
            buf += chunk.decode(errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                yield line.strip()
    # Flush remaining
    if buf.strip():
        yield buf.strip()

def parse_tool_calls(text):
    """Detect tool invocation patterns in Claude's text response.
    Returns list of (name, args_json_str) or empty list."""
    results = []
    # Pattern 1: <invoke tool="NAME">\nARGS_JSON\n</invoke>
    for m in re.finditer(r'<invoke\s+tool="([^"]+)"\s*>\s*\n?(\{.*?\})\s*\n?</invoke>', text, re.DOTALL):
        results.append((m.group(1), m.group(2)))
    if results:
        return results
    # Pattern 2: <atml:invoke name="NAME">...<atml:parameter name="P">V</atml:parameter>...</atml:invoke>
    for m in re.finditer(r'<atml:invoke\s+name="([^"]+)"\s*>', text):
        name = m.group(1)
        rest = text[m.end():]
        end_m = re.search(r'</atml:invoke>', rest)
        if not end_m:
            continue
        body = rest[:end_m.start()]
        params = {}
        for pm in re.finditer(r'<atml:parameter\s+name="([^"]+)"\s*>([^<]*)</atml:parameter>', body):
            params[pm.group(1)] = pm.group(2)
        results.append((name, json.dumps(params)))
    return results

class ClaudeProxyHandler(BaseHTTPRequestHandler):

    def _json_response(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()

    def _send_sse(self, data: str):
        try:
            self.wfile.write(f"data: {data}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse_error(self, msg: str):
        """Send error message inside an already-open SSE stream, then stop."""
        err_chunk = {
            "choices": [{"delta": {"content": f"[Claude proxy error: {msg}]"}, "finish_reason": "stop", "index": 0}]
        }
        self._send_sse(json.dumps(err_chunk, ensure_ascii=False))
        self._send_sse("[DONE]")
        self.close_connection = True

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/v1/models":
            models = [
                {"id": "claude-3-5-sonnet-20241022", "object": "model", "created": 1728000000, "owned_by": "anthropic"},
                {"id": "claude-3-5-haiku-20241022", "object": "model", "created": 1730000000, "owned_by": "anthropic"},
                {"id": "claude-3-opus-20240229", "object": "model", "created": 1709164800, "owned_by": "anthropic"},
                {"id": "claude-3-sonnet-20240229", "object": "model", "created": 1709164800, "owned_by": "anthropic"},
                {"id": "claude-3-haiku-20240307", "object": "model", "created": 1709769600, "owned_by": "anthropic"},
                {"id": "claude-2.1", "object": "model", "created": 1701302400, "owned_by": "anthropic"},
                {"id": "claude-haiku-4-5-20251001", "object": "model", "created": 1747000000, "owned_by": "anthropic"},
            ]
            self._json_response(200, {"object": "list", "data": models})
        elif path == "/health" or path == "/":
            self._json_response(200, {"status": "ok", "org_id": ORGANIZATION_ID,
                                      "cookies": bool(COOKIE_STRING)})
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self):
        global ORGANIZATION_ID

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path != "/v1/chat/completions":
            self._json_response(404, {"error": "not found"})
            return

        if not COOKIE_STRING:
            self._json_response(401, {"error": "no cookies loaded"})
            return

        try:
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            req = json.loads(body)
        except:
            self._json_response(400, {"error": "invalid json"})
            return

        stream = req.get("stream", False)

        global ORGANIZATION_ID
        if not ORGANIZATION_ID:
            try:
                ORGANIZATION_ID = get_organization_id()
                log(f"organization_id fetched on-demand: {ORGANIZATION_ID}")
            except Exception as e:
                log(f"on-demand org fetch failed: {e}")
                if not stream:
                    self._json_response(503, {"error": "could not fetch organization id"})
                else:
                    self._sse_error(f"could not fetch org id: {e}")
                return

        messages = req.get("messages", [])
        if not messages:
            self._json_response(400, {"error": "no messages"})
            return

        model = req.get("model") or CONFIG.get("model") or "claude"
        prompt = format_prompt(messages)
        log(f"chat request: model={model}, stream={stream}, messages={len(messages)}")

        chat_id = create_chat(ORGANIZATION_ID)
        if not chat_id:
            if not stream:
                self._json_response(502, {"error": "failed to create chat"})
            else:
                self._sse_error("failed to create chat")
            return

        try:
            headers = {
                "Accept": "text/event-stream, text/event-stream",
                "Content-Type": "application/json",
                "Origin": "https://claude.ai",
                "Referer": f"https://claude.ai/chat/{chat_id}",
                "TE": "trailers",
            }
            payload = {
                "attachments": [],
                "files": [],
                "prompt": prompt,
                "timezone": get_timezone(),
            }

            rate_limit()
            upstream = claude_req_retry("POST",
                f"/api/organizations/{ORGANIZATION_ID}/chat_conversations/{chat_id}/completion",
                json=payload, headers=headers, stream=True, timeout=240)

            if upstream.status_code != 200:
                err_text = upstream.text[:200]
                log(f"upstream error: {upstream.status_code} {err_text}")
                if stream:
                    self._sse_error(f"upstream error {upstream.status_code}")
                else:
                    self._json_response(502, {"error": f"upstream error {upstream.status_code}", "detail": err_text})
                return

            # Send SSE headers only after upstream is confirmed working
            if stream:
                self._sse_headers()
                self._stream_response(upstream, model)
            else:
                self._blocking_response(upstream, model)

        except Exception as e:
            log(f"request error: {e}")
            try:
                if stream:
                    self._sse_error(str(e))
                else:
                    self._json_response(502, {"error": str(e)})
            except:
                pass
        finally:
            try:
                delete_chat(ORGANIZATION_ID, chat_id)
            except:
                pass

    def _stream_response(self, upstream, model):
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        buf = ""
        tool_call = None

        for line in iter_sse_lines(upstream):
            if not line or not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except:
                continue

            if data.get("type") == "completion":
                text = data.get("completion", "")
                if text:
                    buf += text
            elif data.get("type") == "error":
                log(f"upstream SSE error: {data}")
                self._sse_error(data.get("message", str(data)))
                self.close_connection = True
                return
            elif data.get("type") in ("message_stop", "stop"):
                break
            elif data.get("type") == "content_block_delta":
                text = data.get("delta", {}).get("text", "")
                if text:
                    buf += text
            else:
                log(f"debug: unknown event type {data.get('type')}: {data}")

        # Check for tool invocations in Claude's response
        if buf:
            tool_calls = parse_tool_calls(buf)
            if tool_calls:
                for idx, (name, args_str) in enumerate(tool_calls):
                    tc_id = f"call_{uuid.uuid4().hex[:16]}"
                    # Name chunk
                    chunk = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": idx, "id": tc_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": ""}
                                }]
                            },
                            "finish_reason": None
                        }]
                    }
                    self._send_sse(json.dumps(chunk, ensure_ascii=False))
                    # Arguments chunk
                    chunk2 = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": idx,
                                    "function": {"arguments": args_str}
                                }]
                            },
                            "finish_reason": None
                        }]
                    }
                    self._send_sse(json.dumps(chunk2, ensure_ascii=False))
                # Final chunk
                final = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
                }
                self._send_sse(json.dumps(final, ensure_ascii=False))
                self._send_sse("[DONE]")
                self.close_connection = True
                return

        # No tool call — send as content
        if buf:
            chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"content": buf}, "finish_reason": None}]
            }
            self._send_sse(json.dumps(chunk, ensure_ascii=False))

        final = {
            "id": completion_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        }
        self._send_sse(json.dumps(final, ensure_ascii=False))
        self._send_sse("[DONE]")
        self.close_connection = True

    def _blocking_response(self, upstream, model):
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        content_parts = []

        for line in iter_sse_lines(upstream):
            if not line or not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except:
                continue
            if data.get("type") == "completion":
                content_parts.append(data.get("completion", ""))
            elif data.get("type") == "error":
                log(f"upstream error: {data}")
                self._json_response(502, {"error": data.get("message", str(data))})
                return

        content = "".join(content_parts).strip()

        # Check for tool invocations
        tool_calls = parse_tool_calls(content)
        if tool_calls:
            tcs = []
            for name, args_str in tool_calls:
                tcs.append({
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {"name": name, "arguments": args_str}
                })
            resp = {
                "id": completion_id, "object": "chat.completion",
                "created": created, "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": None, "tool_calls": tcs},
                    "finish_reason": "tool_calls"
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }
            self._json_response(200, resp)
            return

        resp = {
            "id": completion_id, "object": "chat.completion",
            "created": created, "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }
        self._json_response(200, resp)

    def log_message(self, fmt, *args):
        if "GET /v1/models" in fmt % args:
            return
        log(fmt % args)


def main():
    load_config()
    load_cookies()

    global ORGANIZATION_ID
    if COOKIE_STRING:
        try:
            ORGANIZATION_ID = get_organization_id()
            log(f"organization_id: {ORGANIZATION_ID}")
        except Exception as e:
            log(f"failed to get org id after retries: {e}. Server will try again on first request.")
            ORGANIZATION_ID = None
    else:
        log("no cookies loaded, server will return 401")

    port = CONFIG.get("port", 8082)

    class ThreadedHandler(ThreadingMixIn, HTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = ThreadedHandler(("0.0.0.0", port), ClaudeProxyHandler)
    log(f"listening on http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
