![Control Panel Preview](preview.jpg)

# Gemini & Claude Web2API — Free AI Proxy

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: Windows | Linux | macOS](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-blue)]()
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green)]()

**OpenAI-compatible proxy for Google Gemini and Anthropic Claude web APIs.**  
No API keys needed — works with cookies exported from your browser. Free access to Gemini and Claude through a standard `/v1/chat/completions` endpoint.

Perfect for local AI coding agents like [OpenCode](https://opencode.ai), Continue, or any OpenAI-compatible tool.

---

## Features

- **OpenAI-compatible** — `/v1/chat/completions`, streaming (SSE), tools/function calling
- **Gemini** — Flash, Flash Thinking, Flash Lite, Pro, Auto models
- **Claude** — Haiku 4.5, Sonnet 3.5, Opus 3, Haiku 3.5
- **Cross-platform** — Windows (native), Linux, macOS ⚠️ untested
- **No API key** — cookie-based authentication
- **Unified control panel** — web UI at `http://localhost:8083`
- **Tool calling** — Gemini natively, Claude via text-to-tool conversion
- **Rate-limit handling** — automatic retry with exponential backoff

---

## Quick Start

### Windows

```batch
install.bat
:: Then:
:: 1. Export cookies (see below)
:: 2. Run: start.bat
```

### Linux

```bash
bash install.sh
# Then:
# 1. Export cookies (see below)
# 2. Run: bash start.sh
```

### Manual

```bash
pip install -r requirements.txt
pip install -r gemini/requirements.txt
pip install -r claude/requirements.txt
cp gemini/config.json.example config.json
```

### Export Cookies

You need cookies for both services. Use the [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/) Firefox extension:

1. Open **Firefox**, log into [Gemini](https://gemini.google.com/app) and [Claude](https://claude.ai)
2. Install the extension, click its icon → **Export**
3. Save Gemini cookies as `cookie.txt`, Claude cookies as `cookie_claude.txt`
4. Place both files in the project root

> **Tip:** Use the **PASTE** button in the control panel to upload cookies instead of file management.

---

## Usage

### Control Panel (recommended)

```bash
# Windows
start.bat
# Linux
bash start.sh
```

Then open **http://localhost:8083** in your browser. The panel lets you:

- Start/stop each proxy
- Test chat completions
- Paste cookies
- View logs and status

### Direct API

```bash
# Gemini
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"Hello"}]}'

# Claude
curl http://localhost:8082/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","messages":[{"role":"user","content":"Hello"}]}'
```

### With OpenCode

Add to your OpenCode config (`~/.config/opencode/opencode.json` or `%APPDATA%\opencode\opencode.json`):

```json
{
  "provider": {
    "gemini-local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Gemini Local",
      "options": {
        "baseURL": "http://localhost:8081/v1",
        "apiKey": "sk-proxy"
      },
      "models": {
        "gemini-3.5-flash": { "name": "Gemini 3.5 Flash" },
        "gemini-3.5-flash-thinking": { "name": "Gemini 3.5 Flash Thinking" },
        "gemini-flash-lite": { "name": "Gemini Flash Lite" }
      }
    },
    "claude-local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Claude Local",
      "options": {
        "baseURL": "http://localhost:8082/v1",
        "apiKey": "sk-proxy"
      },
      "models": {
        "claude-3-5-sonnet-20241022": { "name": "Claude 3.5 Sonnet" },
        "claude-3-5-haiku-20241022": { "name": "Claude 3.5 Haiku" },
        "claude-3-opus-20240229": { "name": "Claude 3 Opus" },
        "claude-haiku-4-5-20251001": { "name": "Claude Haiku 4.5" }
      }
    }
  }
}
```

---

## Project Structure

```
gemini-claude-proxy/
├── gemini/
│   ├── gemini_web2api.py       # Gemini proxy server (port 8081)
│   ├── config.json.example
│   └── requirements.txt
├── claude/
│   ├── claude_web2api.py       # Claude proxy server (port 8082)
│   ├── config.json
│   └── requirements.txt
├── panel.py                    # Unified web control panel
├── install.bat / install.sh    # Installation scripts
├── start.bat / start.sh        # Start panel
├── stop.bat / stop.sh          # Stop all proxies
├── start_gemini.bat / .sh      # Start Gemini only
├── start_claude.bat / .sh      # Start Claude only
├── start_panel.bat             # Panel + auto-start proxies
├── requirements.txt            # Combined Python dependencies
├── .gitignore
├── LICENSE                     # MIT
└── README.md
```

---

## Models

### Gemini

| ID | Description | Notes |
|---|---|---|
| `gemini-3.5-flash` | Fast, general purpose | Recommended |
| `gemini-3.5-flash-thinking` | Deep reasoning, long output | ~20k chars |
| `gemini-flash-lite` | Lightweight, fastest | |
| `gemini-pro` | Pro model | May need Advanced sub |
| `gemini-auto` | Auto-select | Picks best model |

### Claude

| ID | Description | Notes |
|---|---|---|
| `claude-haiku-4-5-20251001` | Latest Haiku, fast & capable | **Recommended** |
| `claude-3-5-sonnet-20241022` | Balanced, good reasoning | |
| `claude-3-5-haiku-20241022` | Previous Haiku | |
| `claude-3-opus-20240229` | Most powerful | |
| `claude-3-sonnet-20240229` | Legacy Sonnet | |
| `claude-3-haiku-20240307` | Legacy Haiku | |

---

## Configuration

### Gemini (`config.json`)

| Field | Default | Description |
|---|---|---|
| `port` | `8081` | Server port |
| `host` | `"0.0.0.0"` | Bind address |
| `xsrf_token` | `""` | Auth token from Gemini page (`SNlM0e`) |
| `proxy` | `null` | HTTP proxy for upstream (e.g. `http://127.0.0.1:12334`) |
| `log_requests` | `false` | Log request/response bodies |
| `api_keys` | `[]` | Restrict API access |

### Claude (`claude/config.json`)

| Field | Default | Description |
|---|---|---|
| `port` | `8082` | Server port |
| `cookie_file` | `""` | Path to cookie file (auto: `cookie_claude.txt`) |
| `proxy` | `""` | HTTP proxy for upstream |
| `model` | `"claude-haiku-4-5-20251001"` | Default model |
| `log_requests` | `true` | Log requests |

---

## FAQ

### 401 / 403 Forbidden

**Cookies expired.** Re-export from Firefox and restart.  
Gemini: run `python3 -c "from gemini.gemini_web2api import gemini_init, load_config; load_config(); gemini_init()"`  
Claude: just re-export cookies.

### CAPTCHA required

Google or Cloudflare wants verification. Open the service in your browser, solve the CAPTCHA, then restart.

### Rate limited

The proxy retries automatically. If persistent, wait 5-10 minutes.

### "Suspicious traffic" / IP blocked

Your IP is flagged. Use a residential proxy or disable VPN. Most datacenter IPs are blocked.

### Claude says "I cannot use tools"

Claude Web API doesn't support native tool calling. The proxy converts tool calls to text and back. For full agentic features, use Gemini.

### Tool calling not working in OpenCode

Gemini: works natively.  
Claude: works if it outputs `<invoke tool="name">{"arg":"val"}</invoke>` — the proxy converts it to structured format.

---

## Cross-platform Compatibility

| Feature | Windows | Linux (Debian/Ubuntu) | macOS |
|---|---|---|---|
| Gemini proxy | ✅ Native | ✅ Native | ✅ Should work |
| Claude proxy | ✅ Native | ⚠️ `curl_cffi` may need `--break-system-packages` | ⚠️ untested |
| Control panel | ✅ Native | ✅ Native | ⚠️ untested |
| Cookie export | ✅ Firefox | ✅ Firefox | ✅ Firefox |
| Auto‑start on boot | ✅ Task Scheduler | ✅ systemd | ⚠️ untested |

### Known Issues

- **`curl_cffi` on Debian/Ubuntu**: install with `pip install --break-system-packages curl_cffi` if you get TLS/GnuTLS errors
- **`curl_cffi` on macOS**: not tested. If it fails, try `pip install curl_cffi` in a venv
- **Python 3.10+ required**

---

## License

MIT — see [LICENSE](LICENSE).

---

## Keywords

*gemini proxy, claude proxy, free ai api, openai compatible, cookie auth, gemini web api, claude web api, opencode provider, local ai proxy, no api key, gemini tool calling, claude haiku 4.5, windows ai proxy, linux ai proxy*
