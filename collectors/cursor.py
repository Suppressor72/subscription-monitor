"""Cursor Pro usage via dashboard session cookie + internal API.

Works without a headed browser once a Chrome profile holds a valid
WorkosCursorSessionToken (logged-in cursor.com session).

Endpoints (verified 2026-07):
  POST https://cursor.com/api/dashboard/get-current-period-usage
  GET  https://cursor.com/api/usage-summary

Cookie source (first match wins):
  1. CURSOR_SESSION_TOKEN / WORKOS_CURSOR_SESSION_TOKEN env
  2. CURSOR_CHROME_COOKIES path
  3. Common Chrome/Chromium Cookies DB paths via browser_cookie3
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from collectors.common import (
    cycle_fraction,
    iso,
    pace_status,
    provider_record,
    read_manual_overrides,
)

USAGE_URL = "https://cursor.com/api/dashboard/get-current-period-usage"
SUMMARY_URL = "https://cursor.com/api/usage-summary"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

# Prefer explicit env, then common Chrome profile paths
_DEFAULT_COOKIE_CANDIDATES = [
    Path.home() / ".config/google-chrome/Profile 1/Cookies",
    Path.home() / ".config/google-chrome/Default/Cookies",
    Path.home() / ".config/chromium/Default/Cookies",
]


def _cookie_file() -> Optional[Path]:
    env = os.environ.get("CURSOR_CHROME_COOKIES")
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    for p in _DEFAULT_COOKIE_CANDIDATES:
        if p.exists():
            # Prefer a profile that actually has cursor cookies when possible
            return p
    return None


def _best_cookie_file() -> Optional[Path]:
    """Pick the Chrome Cookies DB that contains WorkosCursorSessionToken."""
    env = os.environ.get("CURSOR_CHROME_COOKIES")
    candidates = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(_DEFAULT_COOKIE_CANDIDATES)

    try:
        import sqlite3
        import shutil
        import tempfile
    except ImportError:
        return _cookie_file()

    seen = set()
    for p in candidates:
        if not p or not p.exists() or str(p) in seen:
            continue
        seen.add(str(p))
        try:
            td = tempfile.mkdtemp()
            dst = Path(td) / "Cookies"
            shutil.copy2(p, dst)
            con = sqlite3.connect(dst)
            n = con.execute(
                "SELECT COUNT(*) FROM cookies WHERE name = ? AND host_key LIKE ?",
                ("WorkosCursorSessionToken", "%cursor%"),
            ).fetchone()[0]
            con.close()
            if n:
                return p
        except Exception:  # noqa: BLE001
            continue
    return _cookie_file()


def _load_cookie_header() -> tuple[Optional[str], Optional[str]]:
    """Return (cookie_header, error_note). Never logs token values."""
    # Explicit token override (user-pasted) — highest priority
    tok = os.environ.get("CURSOR_SESSION_TOKEN") or os.environ.get(
        "WORKOS_CURSOR_SESSION_TOKEN"
    )
    if tok:
        # allow "WorkosCursorSessionToken=..." or raw value
        if tok.startswith("WorkosCursorSessionToken="):
            header = tok if "workos_id=" in tok else tok
        else:
            header = f"WorkosCursorSessionToken={tok}"
        return header, None

    cookie_path = _best_cookie_file()
    if not cookie_path:
        return None, "No Chrome Cookies DB found (set CURSOR_CHROME_COOKIES)"

    try:
        import browser_cookie3
    except ImportError:
        return None, "browser_cookie3 not installed (pip install browser-cookie3)"

    try:
        cj = browser_cookie3.chrome(
            cookie_file=str(cookie_path), domain_name="cursor.com"
        )
        parts = []
        has_session = False
        for c in cj:
            parts.append(f"{c.name}={c.value}")
            if c.name == "WorkosCursorSessionToken":
                has_session = True
        if not has_session:
            return (
                None,
                f"No WorkosCursorSessionToken in {cookie_path.parent.name} "
                "(log into cursor.com in that Chrome profile)",
            )
        return "; ".join(parts), None
    except Exception as e:  # noqa: BLE001
        return None, f"cookie decrypt failed: {type(e).__name__}: {e}"


def _http_json(method: str, url: str, cookie_header: str, body: Optional[str] = None) -> dict:
    data = None if body is None else body.encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Cookie", cookie_header)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", "https://cursor.com")
    req.add_header("Referer", "https://cursor.com/dashboard/spending")
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode())


def _ms_or_iso_to_iso(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, (int, float)) or (isinstance(val, str) and val.isdigit()):
        try:
            ms = float(val)
            # heuristic: seconds vs ms
            if ms < 1e12:
                ms *= 1000.0
            return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return None
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return val
    return None


def _collect_api() -> dict:
    cookie_header, err = _load_cookie_header()
    if not cookie_header:
        return provider_record(
            "cursor",
            plan="Pro",
            status="login_required",
            source="api",
            notes=err or "missing session cookie",
        )

    try:
        period = _http_json("POST", USAGE_URL, cookie_header, "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        return provider_record(
            "cursor",
            status="error",
            source="api",
            notes=f"usage HTTP {e.code}: {body}",
        )
    except Exception as e:  # noqa: BLE001
        return provider_record(
            "cursor",
            status="error",
            source="api",
            notes=f"usage {type(e).__name__}: {e}",
        )

    summary: dict[str, Any] = {}
    try:
        summary = _http_json("GET", SUMMARY_URL, cookie_header)
    except Exception:  # noqa: BLE001
        summary = {}

    plan_usage = period.get("planUsage") or {}
    ind = ((summary.get("individualUsage") or {}).get("plan")) or {}
    on_dem = ((summary.get("individualUsage") or {}).get("onDemand")) or {}

    total_pct = plan_usage.get("totalPercentUsed")
    if total_pct is None:
        total_pct = ind.get("totalPercentUsed")
    auto_pct = plan_usage.get("autoPercentUsed", ind.get("autoPercentUsed"))
    api_pct = plan_usage.get("apiPercentUsed", ind.get("apiPercentUsed"))

    try:
        total_f = float(total_pct) if total_pct is not None else None
    except (TypeError, ValueError):
        total_f = None
    try:
        auto_f = float(auto_pct) if auto_pct is not None else None
    except (TypeError, ValueError):
        auto_f = None
    try:
        api_f = float(api_pct) if api_pct is not None else None
    except (TypeError, ValueError):
        api_f = None

    # Report Cursor figures as returned. totalPercentUsed can be lower than
    # autoPercentUsed (first-party); that is Cursor's blended total, not a bug
    # in our math — mirror the dashboard as-is.
    start = _ms_or_iso_to_iso(
        summary.get("billingCycleStart") or period.get("billingCycleStart")
    )
    end = _ms_or_iso_to_iso(
        summary.get("billingCycleEnd") or period.get("billingCycleEnd")
    )
    frac = cycle_fraction(start, end)
    pct_time = round(frac * 100.0, 1) if frac is not None else None

    membership = summary.get("membershipType") or "pro"
    on_demand_enabled = on_dem.get("enabled")
    if on_demand_enabled is None:
        # spend limit section — UI "Disabled" when on-demand off
        on_demand_enabled = False

    windows = [
        {
            "name": "Monthly Quota",
            "kind": "monthly",
            "pct_used": round(total_f, 2) if total_f is not None else None,
            "resets_at": end,
            "cycle_start": start,
            "pct_time_elapsed": pct_time,
            "pace": pace_status(total_f, pct_time),
            "on_demand": "enabled" if on_demand_enabled else "disabled",
            "details": {
                "first_party_models_pct": round(auto_f, 2) if auto_f is not None else None,
                "api_pct": round(api_f, 2) if api_f is not None else None,
                "display_message": period.get("autoModelSelectedDisplayMessage")
                or summary.get("autoModelSelectedDisplayMessage"),
                "api_display_message": period.get("namedModelSelectedDisplayMessage")
                or summary.get("namedModelSelectedDisplayMessage"),
                "plan_spend": plan_usage.get("totalSpend"),
                "included_spend": plan_usage.get("includedSpend"),
                "bonus_spend": plan_usage.get("bonusSpend"),
            },
        }
    ]
    if auto_f is not None:
        windows.append(
            {
                "name": "Monthly First-Party Quota",
                "kind": "monthly",
                "pct_used": round(auto_f, 2),
                "resets_at": end,
                "cycle_start": start,
                "pct_time_elapsed": pct_time,
                "pace": pace_status(auto_f, pct_time),
            }
        )
    if api_f is not None:
        windows.append(
            {
                "name": "Monthly API Quota",
                "kind": "monthly",
                "pct_used": round(api_f, 2),
                "resets_at": end,
                "cycle_start": start,
                "pct_time_elapsed": pct_time,
                "pace": pace_status(api_f, pct_time),
            }
        )

    return provider_record(
        "cursor",
        plan=str(membership).title(),
        windows=windows,
        status="ok",
        source="api",
        notes="Live via WorkosCursorSessionToken + get-current-period-usage (mirrors Cursor % fields)",
        raw={
            "totalPercentUsed": total_f,
            "autoPercentUsed": auto_f,
            "apiPercentUsed": api_f,
            "billingCycleStart": start,
            "billingCycleEnd": end,
            "onDemandEnabled": on_demand_enabled,
        },
    )


def from_override(ov: dict[str, Any]) -> dict:
    pct = ov.get("pct_used")
    try:
        pct_f = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct_f = None
    start = ov.get("cycle_start")
    end = ov.get("cycle_end")
    frac = cycle_fraction(start, end)
    pct_time = round(frac * 100.0, 1) if frac is not None else None
    windows = [
        {
            "name": "Monthly Quota",
            "kind": "monthly",
            "pct_used": pct_f,
            "resets_at": end,
            "cycle_start": start,
            "pct_time_elapsed": pct_time,
            "pace": pace_status(pct_f, pct_time),
            "on_demand": ov.get("on_demand", "disabled"),
            "details": ov.get("details"),
        }
    ]
    return provider_record(
        "cursor",
        plan=ov.get("plan") or "Pro",
        windows=windows,
        status="ok",
        source="manual",
        notes=ov.get("notes") or "From manual_overrides.json",
        raw=ov,
    )


def collect() -> dict:
    """Prefer live API; fall back to manual_overrides on auth/network failure."""
    live = _collect_api()
    if live.get("status") == "ok":
        return live

    ov = (read_manual_overrides().get("cursor") or {}).copy()
    if ov:
        manual = from_override(ov)
        manual["notes"] = (
            f"API failed ({live.get('notes')}); using manual_overrides. "
            + (manual.get("notes") or "")
        ).strip()
        manual["status"] = "stale"
        return manual

    return live


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, default=str))
