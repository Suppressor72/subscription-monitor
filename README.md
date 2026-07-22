# Subscription Monitor

Local LAN dashboard for **multi-subscription AI coding usage** — pace bars, red-zone banners, and a 30‑minute collector loop.

![Subscription Monitor demo dashboard](docs/assets/dashboard-demo.png)

*Demo data (`/?demo=1`) — no real accounts or emails.*

Tracks (when credentials are available):

| Card | What it meters |
|------|----------------|
| **Cursor** | Monthly included / first-party / API pools |
| **Z.ai Coding** | 5‑hour + monthly tools quotas |
| **Grok / SuperGrok** | Weekly credits + monthly $ limit |
| **ChatGPT · Codex** | Weekly (and 5‑hour when present) via Codex `wham/usage` |
| **Antigravity · agy** | Shared **Gemini** and **Claude + GPT** WTUS pools (matches `agy` `/usage`) |
| **Google AI Pro · Gemini** | Code Assist per‑model request fractions (not web chat, not Antigravity) |

> **Disclaimer:** Collectors call undocumented / session-authenticated provider endpoints. They can break when vendors change APIs. This is a personal ops tool, not an official product. Use at your own risk; keep tokens off the public internet.

---

## Features

- **Even-pace bars** — green ≤ even pace, yellow to pace+10pp, red beyond  
- **Per-card red-zone banners** only (yellow does not spam)  
- **Sparklines** — 48h usage trend with per-segment coloring (green/yellow/red by pace zone)  
- **Reset countdowns** — `↻ 3d 7h` badge on each window (amber <6h, red <1h)  
- **Stale detection** — snapshot age &gt; 35 minutes → red badge + card chrome  
- **Self-contained** — server owns its own collector (background thread every 30m); no cron or external scheduler needed  
- **Optional auth** — `SUBMON_TOKEN` for LAN  
- **Manual overrides** — seed or force a provider when login is broken  
- **History** — daily JSONL under `data/history/` (gitignored)  
- **Demo mode** — `http://127.0.0.1:8787/?demo=1` serves sanitized fake data (good for screenshots)

---

## Quick start

```bash
git clone https://github.com/Suppressor72/subscription-monitor.git
cd subscription-monitor

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — at least SUBMON_TOKEN if binding on LAN

cp data/manual_overrides.example.json data/manual_overrides.json

# serve dashboard (collector runs automatically every 30m in-process)
python3 server.py
# → http://127.0.0.1:8787/
# → http://127.0.0.1:8787/?demo=1   (sanitized screenshot data)
```

Optional token URL: `http://127.0.0.1:8787/?token=your-secret`  
(The UI stores the token in `localStorage` after the first visit.)

### Run as a systemd service (survives reboot)

The server is self-contained — it collects data every 30 minutes via an
internal background thread. No cron or external scheduler is needed.
For reboot persistence, install as a **systemd user service**:

```bash
# 1. Enable lingering so user services run without an active login
loginctl enable-linger $USER

# 2. Copy the template and fill in your paths
cp subscription-monitor.service.template ~/.config/systemd/user/subscription-monitor.service
# Edit the file: replace __SUBMON_DIR__ with your checkout path
# and __PYTHON__ with the absolute path to your python (e.g. .venv/bin/python)

# 3. Enable + start
systemctl --user daemon-reload
systemctl --user enable --now subscription-monitor.service

# Check status / logs
systemctl --user status subscription-monitor
journalctl --user -u subscription-monitor -f
```

To change the collect interval, set `SUBMON_INTERVAL` (seconds) in `.env`
or the systemd `Environment=` line (default: `1800` = 30 min).

> **Note:** standalone `collect_all.py` is still available if you prefer
> running the collector out-of-process (cron, systemd timer, etc.). Just
> be aware the server also collects on its own — pick one or disable the
> internal thread by setting `SUBMON_INTERVAL=0`.

---

## Hermes Agent (optional)

You do **not** need [Hermes Agent](https://hermes-agent.nousresearch.com) to run this. The server collects and serves on its own.

This project was **built and is maintained with Hermes** as the ops layer: collectors drift when vendors change APIs, logins expire, and red-zone pacing needs a human-readable nudge. Hermes is a good fit for that maintenance loop.

If you already run Hermes:

| Hook | What to do |
|------|------------|
| **Monitoring** | Call `GET /api/usage` periodically and alert on `login_required` / `error` / red-zone status. Or point Hermes at the dashboard URL. |
| **Env** | `HERMES_HOME` is searched for `.env`, so API keys can live in a Hermes profile without a second secrets file. |
| **Skill** | Optional: a small skill that "check subscription dashboard, fix login_required, summarize red cards" so chat/`/skill` can re-collect and explain. |
| **PR loop** | When a provider endpoint breaks, point Hermes at this repo + a failing `collect_all.py` log and let it patch the collector. |

Standalone users: ignore this section entirely.

---

## Provider setup

Collectors fail soft: missing login → `login_required` / notes on the card. Wire only what you use.

### Z.ai Coding Plan
Set `GLM_API_KEY` or `ZAI_API_KEY` in `.env` (or process env). Uses the official quota API.

### Cursor
Prefer staying logged into [cursor.com](https://cursor.com) in Chrome; the collector reads `WorkosCursorSessionToken` via `browser-cookie3`.

```bash
# Optional overrides
export CURSOR_CHROME_COOKIES="$HOME/.config/google-chrome/Default/Cookies"
# or paste the session cookie value:
export CURSOR_SESSION_TOKEN='...'
```

Requires: `pip install browser-cookie3` (listed in `requirements.txt`).

### Grok / SuperGrok
Needs a working `grok` CLI login on the host (`~/.grok/auth.json`). Weekly via ACP billing; monthly $ via CLI chat-proxy billing.

### ChatGPT · Codex
`codex login` (Sign in with ChatGPT) → `~/.codex/auth.json`.  
Meters **Codex** usage (`chatgpt.com/backend-api/wham/usage`), **not** ChatGPT web message caps.

### Google AI Pro · Code Assist (Gemini card)
`gemini` CLI Google sign-in → `~/.gemini/oauth_creds.json`.  
Calls `cloudcode-pa` `loadCodeAssist` + `retrieveUserQuota`.  
**Important:** User-Agent must **not** contain `GeminiCLI` (Google rejects some CLI fingerprints).

OAuth *client* id/secret (installed-app client inside `@google/gemini-cli`) is resolved automatically when Gemini CLI is installed, or via:

```bash
export GEMINI_OAUTH_CLIENT_ID=...
export GEMINI_OAUTH_CLIENT_SECRET=...
# or copy data/oauth_clients.example.json → data/oauth_clients.json
```

This is **not** the same meter as Antigravity / `agy`.

### Antigravity · agy
Uses Cockpit OAuth at `~/.antigravity_cockpit/credentials.json` and  
`https://daily-cloudcode-pa.googleapis.com` `fetchAvailableModels` with an `antigravity/...` User-Agent.

OAuth client is auto-read from the installed **Antigravity Cockpit** extension, or set `ANTIGRAVITY_OAUTH_CLIENT_ID/SECRET` / `data/oauth_clients.json`.

Shows shared groups matching the TUI `/usage` copy:

- **Gemini · 5 Hour / Weekly Quota** (binding window from `resetTime`)
- **Claude + GPT · …**

The public API returns one `remainingFraction` per model (the binding window). When the 5‑hour pool is tighter, you see 5h; when it is full, the same field often reflects weekly.

---

## Configuration

| Variable | Purpose |
|----------|---------|
| `SUBMON_TOKEN` | Optional shared secret for UI/API |
| `SUBMON_HOST` / `SUBMON_PORT` | Bind (default `0.0.0.0:8787`) |
| `SUBMON_INTERVAL` | Collector interval in seconds (default `1800` = 30 min; `0` disables internal collector) |
| `SUBMON_DATA` | Override data directory |
| `SUBMON_ENV` | Override `.env` path |
| `HERMES_HOME` | Also searched for `.env` (Hermes installs) |
| `GLM_API_KEY` / `ZAI_API_KEY` | Z.ai |
| `CURSOR_SESSION_TOKEN` | Cursor cookie override |
| `CURSOR_CHROME_COOKIES` | Path to Chrome Cookies DB |

Env load order for keys: **process env** → `SUBMON_ENV` → `./.env` → `$HERMES_HOME/.env` → `~/.hermes/.env`.

### Manual overrides

`data/manual_overrides.json` (copy from the `.example`):

```json
{
  "cursor": {
    "force_manual": true,
    "plan": "Pro",
    "pct_used": 12,
    "cycle_start": "2026-07-19T00:00:00-04:00",
    "cycle_end": "2026-08-19T00:00:00-04:00"
  }
}
```

Set `"force_manual": true` to skip the live collector for that provider.

---

## Pace & alerts

| Band | Rule |
|------|------|
| Green | `pct_used ≤ pct_time_elapsed` (even pace) |
| Yellow | between pace and **pace + 10 percentage points** of the full bar |
| Red | `pct_used ≥ even-pace + 10pp` |

**Card banners fire only in red** (plus stale / login / collector errors).  
No-cycle meters (no reset clock) use absolute bands: green ≤60, yellow ≤80, red &gt;80.

---

## Layout

Default grid: **5 cards on row 1**, remaining card(s) on row 2.  
Per-row banner slots equalize to the tallest red banner so card tops align.

---

## Project layout

```
subscription-monitor/
├── server.py                         # FastAPI + static UI + background collector
├── collect_all.py                    # standalone collector (optional — server runs it internally)
├── collectors/
│   ├── common.py                     # snapshot, pace, env helpers
│   ├── cursor.py
│   ├── zai.py
│   ├── grok.py
│   ├── chatgpt.py
│   ├── gemini.py                     # Code Assist
│   └── antigravity.py                # agy / daily-cloudcode-pa
├── static/index.html                 # dashboard
├── docs/
│   ├── demo-latest.json              # sanitized demo payload
│   └── assets/                       # README screenshots
├── subscription-monitor.service.template  # systemd user service (survives reboot)
├── data/                             # gitignored runtime (create locally)
├── requirements.txt
└── .env.example
```

---

## Security notes

- Do **not** commit `.env`, cookies, OAuth JSON, or `data/latest.json`.
- Prefer `SUBMON_TOKEN` whenever binding on `0.0.0.0`.
- Session cookies and OAuth refresh tokens are powerful — treat the host as trusted.
- Snapshots drop collector `raw` payloads before write to reduce accidental secret leakage.
- README screenshot uses `/?demo=1` only — never publish a capture of live `latest.json`.

---

## Extending

1. Add `collectors/myprovider.py` with a `collect() -> dict` using `provider_record(...)`.
2. Import and append in `collect_all.py`.
3. Add a label (and optional order entry) in `static/index.html` (`ORDER` / `LABELS`).

Window shape (minimal):

```python
{
  "name": "Weekly Quota",
  "kind": "weekly",
  "pct_used": 42.0,
  "pct_time_elapsed": 30.0,   # optional; enables pace coloring
  "resets_at": "2026-07-28T12:00:00+00:00",
  "pace": "ahead",            # from pace_status()
}
```

---

## License

MIT — see [LICENSE](LICENSE).

---

## Credits

Built as a personal ops dashboard for juggling Cursor, Z.ai, Grok, Codex, Gemini Code Assist, and Antigravity quotas on one LAN page. Maintained with [Hermes Agent](https://hermes-agent.nousresearch.com); runs fine without it. PRs and tweaks welcome.
