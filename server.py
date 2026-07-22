"""
LAN-facing Subscription Usage Dashboard.

  python3 server.py

The server owns its own collector — a background thread runs all
collectors every 30 minutes (configurable via SUBMON_INTERVAL env).
No external scheduler (cron, systemd timer, Hermes) is needed.

Auth: set SUBMON_TOKEN in .env; pass ?token=... or header X-Submon-Token.
If unset, the dashboard is open (bind carefully — use a firewall / token on LAN).
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collectors.common import ENV_PATH, HISTORY_DIR, DATA_DIR, read_snapshot  # noqa: E402
import collect_all  # noqa: E402
import json  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

logger = logging.getLogger("subscription-monitor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_DEFAULT_INTERVAL = int(os.environ.get("SUBMON_INTERVAL", "1800"))  # 30 min
_SETTINGS_PATH = DATA_DIR / "settings.json"

# Mutable collector state — the background thread watches this.
_collector_state: dict = {"interval_s": _DEFAULT_INTERVAL, "wake": threading.Event()}


def _load_server_settings() -> dict:
    """Load persisted server settings (survives restart)."""
    if _SETTINGS_PATH.exists():
        try:
            return json.loads(_SETTINGS_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _save_server_settings(s: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(json.dumps(s, indent=2) + "\n")


def _get_interval() -> int:
    """Current collector interval in seconds."""
    stored = _load_server_settings().get("interval_s")
    if stored and isinstance(stored, int) and stored >= 0:
        return stored
    return _DEFAULT_INTERVAL


def _set_interval(seconds: int) -> None:
    """Persist new interval and wake the collector thread."""
    s = _load_server_settings()
    s["interval_s"] = seconds
    _save_server_settings(s)
    _collector_state["interval_s"] = seconds
    _collector_state["wake"].set()


# Load persisted interval on startup
_collector_state["interval_s"] = _get_interval()

app = FastAPI(title="Subscription Monitor", version="0.2.0")
STATIC = ROOT / "static"
DOCS = ROOT / "docs"
STATIC.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _token() -> str | None:
    t = os.environ.get("SUBMON_TOKEN")
    if t:
        return t.strip()
    if ENV_PATH.exists():
        m = re.search(r"^SUBMON_TOKEN=(.*)$", ENV_PATH.read_text(errors="ignore"), re.M)
        if m:
            return m.group(1).strip().strip('"').strip("'") or None
    return None


def _check_auth(
    request: Request,
    token: str | None = None,
    x_submon_token: str | None = None,
) -> None:
    expected = _token()
    if not expected:
        return  # open mode
    got = token or x_submon_token or request.headers.get("X-Submon-Token")
    if got != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


def _demo_snapshot() -> dict:
    """Sanitized illustrative snapshot for README screenshots / ?demo=1."""
    path = DOCS / "demo-latest.json"
    data = json.loads(path.read_text())
    now = datetime.now(timezone.utc)
    iso = now.isoformat()
    # Keep demo "fresh" so UI never marks it stale
    data["generated_at"] = iso
    data["collected_at"] = iso
    data["host"] = "demo-host"
    data["demo"] = True
    for p in (data.get("providers") or {}).values():
        if isinstance(p, dict):
            p["collected_at"] = iso
            p["source"] = "demo"
            # Hard scrub any accidental PII keys
            for k in ("email", "account", "user", "project_id"):
                p.pop(k, None)
            notes = p.get("notes") or ""
            if "@" in notes:
                p["notes"] = "Demo data — illustrative only"
            plan = p.get("plan") or ""
            if "@" in plan:
                p["plan"] = plan.split("·")[0].strip() or "Demo plan"
    return data


@app.get("/api/history")
def history(
    request: Request,
    hours: int | None = Query(48, ge=1, le=168),
    token: str | None = Query(None),
    demo: int | None = Query(None),
    x_submon_token: str | None = Header(None, alias="X-Submon-Token"),
):
    """Compact history for sparklines: {provider: [{t, windows: {name: pct}}]}."""
    _check_auth(request, token=token, x_submon_token=x_submon_token)
    if demo or request.query_params.get("demo") in ("1", "true", "yes"):
        return JSONResponse(_demo_history(hours or 48))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out: dict[str, list] = {}
    if HISTORY_DIR.exists():
        for f in sorted(HISTORY_DIR.glob("*.jsonl")):
            try:
                for line in f.read_text(errors="ignore").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    h = json.loads(line)
                    t = h.get("t", "")
                    try:
                        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        continue
                    if dt < cutoff:
                        continue
                    for pname, pdata in (h.get("p") or {}).items():
                        windows = {}
                        for w in (pdata.get("windows") or []):
                            wn = w.get("name")
                            pct = w.get("pct")
                            if wn is not None and pct is not None:
                                windows[wn] = {
                                    "pct": pct,
                                    "ti": w.get("pct_time"),
                                }
                        if windows:
                            out.setdefault(pname, []).append({"t": t, "w": windows})
            except (json.JSONDecodeError, OSError):
                continue
    return JSONResponse(out)


def _demo_history(hours: int) -> dict:
    """Synthesize demo sparkline data from demo-latest.json."""
    import random
    base = _demo_snapshot()
    now = datetime.now(timezone.utc)
    pts: dict[str, list] = {}
    for pname, p in (base.get("providers") or {}).items():
        windows = {w["name"]: (w.get("pct_used") or 0, w.get("pct_time_elapsed")) for w in (p.get("windows") or [])}
        if not windows:
            continue
        series = []
        for i in range(hours, -1, -1):
            t = (now - timedelta(hours=i)).isoformat()
            wpts = {}
            for wn, (base_pct, base_ti) in windows.items():
                jitter = random.uniform(-3, 3)
                pct = round(max(0, min(100, base_pct - i * 1.2 + jitter)), 1)
                ti = round(max(0, min(100, (base_ti or 0) - i * 1.0)), 1) if base_ti else None
                wpts[wn] = {"pct": pct, "ti": ti}
            series.append({"t": t, "w": wpts})
        pts[pname] = series
    return pts


@app.on_event("startup")
def _start_collector():
    """Launch background collector thread."""
    t = threading.Thread(target=_collector_loop, daemon=True, name="submon-collector")
    t.start()
    logger.info("collector thread started (interval=%ds)", _get_interval())


def _collector_loop() -> None:
    """Collect immediately on startup, then every interval.

    Uses a threading.Event so POST /api/settings can wake the thread
    immediately when the interval changes — no restart needed.
    """
    while True:
        try:
            collect_all.main()
            logger.info("collect cycle complete")
        except Exception as e:  # noqa: BLE001
            logger.error("collect cycle failed: %s", e)
        interval = _collector_state["interval_s"]
        if interval <= 0:
            # Collector disabled — sleep until woken
            _collector_state["wake"].wait()
            _collector_state["wake"].clear()
        else:
            # Wait for interval, but also wake early if settings change
            _collector_state["wake"].wait(timeout=interval)
            _collector_state["wake"].clear()


@app.get("/api/health")
def health():
    return {"ok": True, "service": "subscription-monitor", "interval_s": _get_interval()}


@app.get("/api/settings")
def get_settings(
    request: Request,
    token: str | None = Query(None),
    x_submon_token: str | None = Header(None, alias="X-Submon-Token"),
):
    _check_auth(request, token=token, x_submon_token=x_submon_token)
    return JSONResponse({"interval_s": _get_interval()})


@app.post("/api/settings")
async def update_settings(
    request: Request,
    token: str | None = Query(None),
    x_submon_token: str | None = Header(None, alias="X-Submon-Token"),
):
    """Update server-side settings. Currently supports interval_s (collector interval)."""
    _check_auth(request, token=token, x_submon_token=x_submon_token)
    body = await request.json()
    interval = body.get("interval_s")
    if interval is None or not isinstance(interval, (int, float)) or interval < 0:
        raise HTTPException(status_code=400, detail="interval_s must be a non-negative number")
    interval_int = int(interval)
    _set_interval(interval_int)
    logger.info("collector interval changed to %ds", interval_int)
    return JSONResponse({"ok": True, "interval_s": interval_int})


@app.get("/api/usage")
def usage(
    request: Request,
    token: str | None = Query(None),
    demo: int | None = Query(None),
    x_submon_token: str | None = Header(None, alias="X-Submon-Token"),
):
    _check_auth(request, token=token, x_submon_token=x_submon_token)
    if demo or request.query_params.get("demo") in ("1", "true", "yes"):
        return JSONResponse(_demo_snapshot())
    return JSONResponse(read_snapshot())


@app.post("/api/collect")
def collect_now(
    request: Request,
    token: str | None = Query(None),
    x_submon_token: str | None = Header(None, alias="X-Submon-Token"),
):
    """Trigger an immediate collection (overrides the background 30m interval)."""
    _check_auth(request, token=token, x_submon_token=x_submon_token)
    if request.query_params.get("demo") in ("1", "true", "yes"):
        return JSONResponse(_demo_snapshot())
    import collect_all

    collect_all.main()
    return JSONResponse(read_snapshot())


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    token: str | None = Query(None),
    x_submon_token: str | None = Header(None, alias="X-Submon-Token"),
):
    _check_auth(request, token=token, x_submon_token=x_submon_token)
    html_path = STATIC / "index.html"
    return HTMLResponse(html_path.read_text())


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("SUBMON_HOST", "0.0.0.0")
    port = int(os.environ.get("SUBMON_PORT", "8787"))
    uvicorn.run("server:app", host=host, port=port, reload=False)
