"""xAI Grok / SuperGrok unified billing usage.

Two meters (same account, unified billing):
  1) Weekly included credits  — via `grok agent stdio` ACP method `_x.ai/billing`
     (same payload as TUI `/usage` weekly %).
  2) Monthly $ limit          — via GET https://cli-chat-proxy.grok.com/v1/billing
     (cents: monthlyLimit/used).

Auth: ~/.grok/auth.json (OIDC from `grok login`). Access tokens expire ~6h.
The grok CLI refreshes them on ACP `authenticate`; we also OIDC-refresh
ourselves when the token is near expiry so monthly HTTP does not 401.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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
OIDC_WELL_KNOWN = "https://auth.x.ai/.well-known/openid-configuration"
# Cold `grok agent stdio` can take 30–45s on a busy host before auth returns.
ACP_TIMEOUT_S = 60.0
TOKEN_SKEW = timedelta(minutes=5)


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


def _parse_dt(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _load_auth_file() -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Return (map_key, entry) from ~/.grok/auth.json."""
    if not AUTH_PATH.exists():
        return None, None
    try:
        data = json.loads(AUTH_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict) or not data:
        return None, None
    key = next(iter(data.keys()))
    entry = data.get(key)
    if not isinstance(entry, dict):
        return None, None
    return str(key), entry


def _load_auth() -> Optional[dict[str, Any]]:
    _, entry = _load_auth_file()
    return entry


def _bearer_token(entry: dict[str, Any]) -> Optional[str]:
    tok = entry.get("key") or entry.get("access_token")
    if not tok:
        return None
    return str(tok)


def _token_expired(entry: dict[str, Any], skew: timedelta = TOKEN_SKEW) -> bool:
    exp = _parse_dt(entry.get("expires_at"))
    if exp is None:
        return False
    return exp <= datetime.now(timezone.utc) + skew


def _save_auth_entry(map_key: str, entry: dict[str, Any]) -> None:
    try:
        data: dict[str, Any] = {}
        if AUTH_PATH.exists():
            try:
                raw = json.loads(AUTH_PATH.read_text())
                if isinstance(raw, dict):
                    data = raw
            except (OSError, json.JSONDecodeError):
                data = {}
        data[map_key] = entry
        AUTH_PATH.write_text(json.dumps(data, indent=2) + "\n")
        try:
            AUTH_PATH.chmod(0o600)
        except OSError:
            pass
    except OSError:
        pass


def _oidc_refresh(entry: dict[str, Any], map_key: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Refresh access token via auth.x.ai. Returns (token, error)."""
    refresh = entry.get("refresh_token")
    client_id = entry.get("oidc_client_id")
    if not refresh or not client_id:
        return None, "no refresh_token/oidc_client_id in auth.json"

    try:
        with urllib.request.urlopen(OIDC_WELL_KNOWN, timeout=15) as resp:
            oidc = json.loads(resp.read().decode())
        token_url = oidc.get("token_endpoint") or "https://auth.x.ai/oauth2/token"
    except Exception:
        token_url = "https://auth.x.ai/oauth2/token"

    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": str(refresh),
            "client_id": str(client_id),
        }
    ).encode()
    req = urllib.request.Request(
        token_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "hermes-submon/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            tok = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read()[:240].decode(errors="replace")
        return None, f"oidc refresh HTTP {e.code}: {detail}"
    except Exception as e:  # noqa: BLE001
        return None, f"oidc refresh failed: {e}"

    access = tok.get("access_token")
    if not access:
        return None, "oidc refresh returned no access_token"

    entry["key"] = access
    if tok.get("refresh_token"):
        entry["refresh_token"] = tok["refresh_token"]
    expires_in = tok.get("expires_in")
    if expires_in is not None:
        try:
            exp = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            entry["expires_at"] = exp.isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            pass
    if map_key:
        _save_auth_entry(map_key, entry)
    return str(access), None


def _ensure_bearer() -> tuple[Optional[str], Optional[dict[str, Any]], Optional[str]]:
    """Return (token, entry, error). Refreshes via OIDC when near expiry."""
    map_key, entry = _load_auth_file()
    if not entry:
        return None, None, "no auth.json"
    token = _bearer_token(entry)
    if token and not _token_expired(entry):
        return token, entry, None
    new_tok, err = _oidc_refresh(entry, map_key=map_key)
    if new_tok:
        return new_tok, entry, None
    if token:
        return token, entry, err or "using possibly expired token"
    return None, entry, err or "no access token"


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
        stderr=subprocess.PIPE,
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

    step = max(20.0, timeout / 3.0)
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
        init = wait_id(1, time.time() + step)
        if "error" in init:
            raise RuntimeError(f"initialize failed: {init['error']}")

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "authenticate",
                "params": {"methodId": "cached_token"},
            }
        )
        auth = wait_id(2, time.time() + step)
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
        resp = wait_id(3, time.time() + step)
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

    map_key, entry = _load_auth_file()
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

    errors: list[str] = []
    weekly_raw: Optional[dict] = None
    monthly_raw: Optional[dict] = None
    plan = entry.get("subscription_tier") or "SuperGrok / Premium+"

    # 1) Fresh bearer before monthly HTTP (tokens ~6h).
    token, entry, tok_err = _ensure_bearer()
    if tok_err and not token:
        errors.append(f"auth: {tok_err}")
    elif tok_err:
        errors.append(f"auth note: {tok_err}")

    # 2) Weekly ACP (TUI /usage). CLI authenticate may also refresh disk token.
    try:
        weekly_raw = _acp_request("_x.ai/billing")
        tier = weekly_raw.get("subscription_tier")
        if tier:
            plan = str(tier)
        token2, entry2, _ = _ensure_bearer()
        if token2:
            token, entry = token2, entry2 or entry
    except FileNotFoundError:
        errors.append("`grok` CLI not on PATH")
    except Exception as e:  # noqa: BLE001
        errors.append(f"weekly: {type(e).__name__}: {e}")

    # 3) Monthly $ with fresh bearer; one OIDC retry on 401.
    if token:
        try:
            monthly_raw = _fetch_monthly(token)
        except urllib.error.HTTPError as e:
            if e.code == 401 and entry is not None:
                new_tok, rerr = _oidc_refresh(entry, map_key=map_key)
                if new_tok:
                    try:
                        monthly_raw = _fetch_monthly(new_tok)
                    except urllib.error.HTTPError as e2:
                        errors.append(f"monthly HTTP {e2.code} after refresh")
                    except Exception as e2:  # noqa: BLE001
                        errors.append(f"monthly: {type(e2).__name__}: {e2}")
                else:
                    errors.append(f"monthly HTTP 401 ({rerr or 'refresh failed'})")
            else:
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
        status = (
            "login_required"
            if any("401" in x or "login" in x.lower() for x in errors)
            else "error"
        )
        return provider_record(
            "grok",
            plan=plan,
            status=status,
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
