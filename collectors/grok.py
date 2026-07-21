"""xAI Grok / SuperGrok unified billing usage.

Two meters (same account, unified billing):
  1) Weekly included credits  — via `grok agent stdio` ACP method `_x.ai/billing`
     (same payload as TUI `/usage` weekly %).
  2) Monthly $ limit          — via GET https://cli-chat-proxy.grok.com/v1/billing
     (cents: monthlyLimit/used).

Auth: ~/.grok/auth.json (OIDC from `grok login`). No manual override required
when that file is present and fresh.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
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
    utcnow,
)

GROK_HOME = Path.home() / ".grok"
AUTH_PATH = GROK_HOME / "auth.json"
MONTHLY_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing"
ACP_TIMEOUT_S = 25.0


def _val(obj: Any) -> Any:
    """Unwrap {\"val\": N} money/amount wrappers; pass through plain values."""
    if isinstance(obj, dict) and "val" in obj and len(obj) <= 2:
        return obj.get("val")
    return obj


def _cents_to_usd(cents: Any) -> Optional[float]:
    try:
        if cents is None:
            return None
        return round(float(cents) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def _load_auth() -> Optional[dict[str, Any]]:
    if not AUTH_PATH.exists():
        return None
    try:
        data = json.loads(AUTH_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    # Single-entry map keyed by issuer::client_id
    entry = next(iter(data.values()))
    if not isinstance(entry, dict):
        return None
    return entry


def _bearer_token(entry: dict[str, Any]) -> Optional[str]:
    tok = entry.get("key") or entry.get("access_token")
    if not tok:
        return None
    return str(tok)


def _fetch_monthly(token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        MONTHLY_BILLING_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "hermes-submon/1.0",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-authenticateresponse": "authenticate-response",
            "x-grok-client-mode": "interactive",
            "x-grok-client-version": "0.2.106",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _acp_request(method: str, params: Optional[dict] = None, timeout: float = ACP_TIMEOUT_S) -> dict[str, Any]:
    """One-shot grok agent stdio JSON-RPC call (initialize → auth → method)."""
    proc = subprocess.Popen(
        ["grok", "agent", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert proc.stdin and proc.stdout
    q: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:
                q.put(line)
        finally:
            q.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def wait_id(want_id: int, deadline: float) -> dict[str, Any]:
        while time.time() < deadline:
            try:
                line = q.get(timeout=0.25)
            except queue.Empty:
                continue
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                return msg
        raise TimeoutError(f"ACP response id={want_id} timed out")

    deadline = time.time() + timeout
    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientInfo": {"name": "hermes-submon", "version": "1.0"},
                    "capabilities": {},
                },
            }
        )
        init = wait_id(1, deadline)
        if "error" in init:
            raise RuntimeError(f"initialize failed: {init['error']}")

        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "authenticate",
                "params": {"methodId": "cached_token"},
            }
        )
        auth = wait_id(2, deadline)
        if "error" in auth:
            raise RuntimeError(f"authenticate failed: {auth['error']}")

        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": method,
                "params": params or {},
            }
        )
        resp = wait_id(3, deadline)
        if "error" in resp:
            raise RuntimeError(f"{method} failed: {resp['error']}")
        result = resp.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"{method} returned non-object result")
        return result
    finally:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass


def _window_weekly(weekly: dict[str, Any]) -> dict[str, Any]:
    cfg = weekly.get("config") or {}
    pct = cfg.get("creditUsagePercent")
    try:
        pct_f = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct_f = None

    period = cfg.get("currentPeriod") or {}
    start = period.get("start") or cfg.get("billingPeriodStart")
    end = period.get("end") or cfg.get("billingPeriodEnd")
    frac = cycle_fraction(start, end)
    pct_time = round(frac * 100.0, 1) if frac is not None else None

    prepaid = _cents_to_usd(_val(cfg.get("prepaidBalance")))
    on_demand_used = _cents_to_usd(_val(cfg.get("onDemandUsed")))
    on_demand_cap = _cents_to_usd(_val(cfg.get("onDemandCap")))

    details = {
        "period_type": period.get("type"),
        "unified": cfg.get("isUnifiedBillingUser"),
        "prepaid_usd": prepaid,
        "on_demand_used_usd": on_demand_used,
        "on_demand_cap_usd": on_demand_cap,
        # Build vs API split is shown in TUI sometimes; not in this payload.
    }
    return {
        "name": "Weekly Quota",
        "kind": "weekly",
        "pct_used": pct_f,
        "resets_at": end,
        "cycle_start": start,
        "pct_time_elapsed": pct_time,
        "pace": pace_status(pct_f, pct_time),
        "details": details,
    }


def _window_monthly(monthly: dict[str, Any]) -> dict[str, Any]:
    cfg = (monthly.get("config") or monthly) if isinstance(monthly, dict) else {}
    used_cents = _val(cfg.get("used"))
    limit_cents = _val(cfg.get("monthlyLimit"))
    used_usd = _cents_to_usd(used_cents)
    limit_usd = _cents_to_usd(limit_cents)

    pct_f = None
    if used_cents is not None and limit_cents not in (None, 0, 0.0):
        try:
            pct_f = round(100.0 * float(used_cents) / float(limit_cents), 1)
        except (TypeError, ValueError, ZeroDivisionError):
            pct_f = None

    start = cfg.get("billingPeriodStart")
    end = cfg.get("billingPeriodEnd")
    frac = cycle_fraction(start, end)
    pct_time = round(frac * 100.0, 1) if frac is not None else None

    remaining_usd = None
    if used_usd is not None and limit_usd is not None:
        remaining_usd = round(limit_usd - used_usd, 2)

    return {
        "name": "Monthly Quota",
        "kind": "monthly",
        "pct_used": pct_f,
        "used": used_usd,
        "limit": limit_usd,
        "remaining": remaining_usd,
        "unit": "USD",
        "resets_at": end,
        "cycle_start": start,
        "pct_time_elapsed": pct_time,
        "pace": pace_status(pct_f, pct_time),
        "details": {
            "used_cents": used_cents,
            "limit_cents": limit_cents,
            "on_demand_cap_usd": _cents_to_usd(_val(cfg.get("onDemandCap"))),
            "history": cfg.get("history"),
        },
    }


def _from_override(ov: dict[str, Any]) -> dict:
    """Legacy manual path — still honored if present and force_manual set."""
    windows = []
    if ov.get("pct_used") is not None or ov.get("week_start") or ov.get("week_end"):
        pct = ov.get("pct_used")
        try:
            pct_f = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct_f = None
        start = ov.get("week_start") or ov.get("cycle_start")
        end = ov.get("week_end") or ov.get("cycle_end") or ov.get("resets_at")
        frac = cycle_fraction(start, end)
        pct_time = round(frac * 100.0, 1) if frac is not None else None
        windows.append(
            {
                "name": "Weekly Quota",
                "kind": "weekly",
                "pct_used": pct_f,
                "resets_at": end,
                "cycle_start": start,
                "pct_time_elapsed": pct_time,
                "pace": pace_status(pct_f, pct_time),
                "details": ov.get("details"),
            }
        )
    if ov.get("monthly_pct") is not None or ov.get("monthly_used") is not None:
        try:
            mp = float(ov["monthly_pct"]) if ov.get("monthly_pct") is not None else None
        except (TypeError, ValueError):
            mp = None
        start = ov.get("month_start")
        end = ov.get("month_end")
        frac = cycle_fraction(start, end)
        pct_time = round(frac * 100.0, 1) if frac is not None else None
        windows.append(
            {
                "name": "Monthly Quota",
                "kind": "monthly",
                "pct_used": mp,
                "used": ov.get("monthly_used"),
                "limit": ov.get("monthly_limit"),
                "resets_at": end,
                "cycle_start": start,
                "pct_time_elapsed": pct_time,
                "pace": pace_status(mp, pct_time),
            }
        )
    return provider_record(
        "grok",
        plan=ov.get("plan") or "SuperGrok / Premium+",
        windows=windows,
        status="ok",
        source="manual",
        notes=ov.get("notes") or "From manual_overrides.json",
        raw=ov,
    )


def collect() -> dict:
    ov = (read_manual_overrides().get("grok") or {}).copy()
    if ov.get("force_manual"):
        return _from_override(ov)

    entry = _load_auth()
    if not entry:
        if ov:
            return _from_override(ov)
        return provider_record(
            "grok",
            plan="SuperGrok / Premium+",
            status="login_required",
            source="grok-cli",
            notes="No ~/.grok/auth.json — run `grok login` on this host.",
            windows=[],
        )

    token = _bearer_token(entry)
    errors: list[str] = []
    weekly_raw: Optional[dict] = None
    monthly_raw: Optional[dict] = None
    plan = entry.get("subscription_tier") or "SuperGrok / Premium+"

    # Weekly (ACP) — authoritative for TUI /usage weekly %
    try:
        weekly_raw = _acp_request("_x.ai/billing")
        tier = weekly_raw.get("subscription_tier")
        if tier:
            plan = str(tier)
    except FileNotFoundError:
        errors.append("`grok` CLI not on PATH")
    except Exception as e:  # noqa: BLE001
        errors.append(f"weekly: {type(e).__name__}: {e}")

    # Monthly $ (HTTP)
    if token:
        try:
            monthly_raw = _fetch_monthly(token)
        except urllib.error.HTTPError as e:
            errors.append(f"monthly HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"monthly: {type(e).__name__}: {e}")
    else:
        errors.append("monthly: no access token in auth.json")

    windows: list[dict[str, Any]] = []
    if weekly_raw:
        windows.append(_window_weekly(weekly_raw))
    if monthly_raw:
        windows.append(_window_monthly(monthly_raw))

    if not windows:
        if ov:
            rec = _from_override(ov)
            rec["notes"] = (rec.get("notes") or "") + " | live failed: " + "; ".join(errors)
            return rec
        return provider_record(
            "grok",
            plan=plan,
            status="error",
            source="grok-cli",
            notes="; ".join(errors) or "no usage data",
            windows=[],
            raw={"errors": errors},
        )

    notes = (
        "Live: weekly via grok ACP `_x.ai/billing` (TUI /usage); "
        "monthly via cli-chat-proxy /v1/billing (cents→USD). "
        "Unified billing — Grok Build + API share weekly credits."
    )
    if errors:
        notes += " Partial: " + "; ".join(errors)

    return provider_record(
        "grok",
        plan=plan,
        windows=windows,
        status="ok",
        source="grok-cli",
        notes=notes,
        raw={
            "weekly": weekly_raw,
            "monthly": monthly_raw,
            "errors": errors,
            "collected_wall": iso(utcnow()),
        },
    )


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, default=str))
