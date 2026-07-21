"""
LAN-facing Subscription Usage Dashboard.

  uvicorn server:app --host 0.0.0.0 --port 8787

Auth: set SUBMON_TOKEN in env or a .env file; pass ?token=... or header X-Submon-Token.
If unset, the dashboard is open (bind carefully — use a firewall / token on LAN).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collectors.common import ENV_PATH, read_snapshot  # noqa: E402

app = FastAPI(title="Subscription Monitor", version="0.1.0")
STATIC = ROOT / "static"
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


@app.get("/api/health")
def health():
    return {"ok": True, "service": "subscription-monitor"}


@app.get("/api/usage")
def usage(
    request: Request,
    token: str | None = Query(None),
    x_submon_token: str | None = Header(None, alias="X-Submon-Token"),
):
    _check_auth(request, token=token, x_submon_token=x_submon_token)
    return JSONResponse(read_snapshot())


@app.post("/api/collect")
def collect_now(
    request: Request,
    token: str | None = Query(None),
    x_submon_token: str | None = Header(None, alias="X-Submon-Token"),
):
    """Trigger an immediate collection (still every-30m via cron)."""
    _check_auth(request, token=token, x_submon_token=x_submon_token)
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
