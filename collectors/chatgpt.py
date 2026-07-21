"""ChatGPT Plus · Codex quota via existing Codex ChatGPT OAuth.

Uses ~/.codex/auth.json (from `codex login` / Sign in with ChatGPT).

Endpoints (verified 2026-07-21):
  GET https://chatgpt.com/backend-api/wham/usage
  GET https://chatgpt.com/backend-api/subscriptions?account_id=...

This is the **Codex** rate-limit pool included with Plus (weekly + optional 5h
secondary), NOT ChatGPT web message caps (those need a chatgpt.com browser
session and are still manual / future).
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
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

CODEX_HOME = Path.home() / ".codex"
AUTH_PATH = CODEX_HOME / "auth.json"
WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
SUBSCRIPTIONS_URL = "https://chatgpt.com/backend-api/subscriptions"
TOKEN_URL = "https://auth.openai.com/oauth/token"
# Public Codex / ChatGPT OAuth client (same family as CLI login)
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
UA = "codex_cli_rs/0.142.5 hermes-submon/1.0"


def _b64url_json(segment: str) -> Optional[dict]:
    try:
        pad = "=" * ((4 - len(segment) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(segment + pad))
    except Exception:  # noqa: BLE001
        return None


def _jwt_exp(token: str) -> Optional[float]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        pl = _b64url_json(parts[1]) or {}
        exp = pl.get("exp")
        return float(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


def _load_auth() -> Optional[dict[str, Any]]:
    if not AUTH_PATH.exists():
        return None
    try:
        data = json.loads(AUTH_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        return None
    return data


def _save_auth(data: dict[str, Any]) -> None:
    """Best-effort write-back after refresh (mode 600)."""
    try:
        AUTH_PATH.write_text(json.dumps(data, indent=2))
        AUTH_PATH.chmod(0o600)
    except OSError:
        pass


def _maybe_refresh(auth: dict[str, Any]) -> dict[str, Any]:
    """Refresh access_token if expiring within 5 minutes."""
    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token") or ""
    refresh = tokens.get("refresh_token")
    exp = _jwt_exp(access)
    now = time.time()
    if exp is not None and exp > now + 300:
        return auth
    if not refresh:
        return auth

    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": OAUTH_CLIENT_ID,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001
        return auth

    new_access = payload.get("access_token")
    if not new_access:
        return auth
    tokens = dict(tokens)
    tokens["access_token"] = new_access
    if payload.get("refresh_token"):
        tokens["refresh_token"] = payload["refresh_token"]
    if payload.get("id_token"):
        tokens["id_token"] = payload["id_token"]
    auth = dict(auth)
    auth["tokens"] = tokens
    auth["last_refresh"] = iso(utcnow())
    _save_auth(auth)
    return auth


def _headers(access: str, account_id: Optional[str]) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {access}",
        "Accept": "application/json",
        "User-Agent": UA,
    }
    if account_id:
        h["ChatGPT-Account-Id"] = account_id
        h["OpenAI-Account-Id"] = account_id
    return h


def _get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _window_from_rl(
    *,
    name: str,
    kind: str,
    win: dict[str, Any],
) -> dict[str, Any]:
    pct = win.get("used_percent")
    try:
        pct_f = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct_f = None

    reset_at = win.get("reset_at")
    resets_iso = None
    if reset_at is not None:
        try:
            resets_iso = datetime.fromtimestamp(float(reset_at), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            resets_iso = None

    window_s = win.get("limit_window_seconds")
    cycle_start = None
    pct_time = None
    try:
        ws = float(window_s) if window_s is not None else None
    except (TypeError, ValueError):
        ws = None

    if resets_iso and ws and ws > 0:
        end = datetime.fromisoformat(resets_iso)
        start = datetime.fromtimestamp(end.timestamp() - ws, tz=timezone.utc)
        cycle_start = start.isoformat()
        frac = cycle_fraction(cycle_start, resets_iso)
        pct_time = round(frac * 100.0, 1) if frac is not None else None
    elif win.get("reset_after_seconds") is not None and ws:
        # Elapsed ≈ window - remaining
        try:
            rem = float(win["reset_after_seconds"])
            elapsed = max(0.0, ws - rem)
            pct_time = round(min(100.0, 100.0 * elapsed / ws), 1)
            if resets_iso:
                end = datetime.fromisoformat(resets_iso)
                cycle_start = datetime.fromtimestamp(
                    end.timestamp() - ws, tz=timezone.utc
                ).isoformat()
        except (TypeError, ValueError, OSError):
            pass

    label = name
    if ws is not None:
        hours = ws / 3600.0
        if abs(hours - 5) < 0.1:
            label = "5 Hour Quota"
        elif abs(hours - 168) < 1 or abs(ws - 604800) < 60:
            label = "Weekly Quota"
        else:
            label = f"{hours:.0f} Hour Quota"

    return {
        "name": label,
        "kind": kind,
        "pct_used": pct_f,
        "resets_at": resets_iso,
        "cycle_start": cycle_start,
        "pct_time_elapsed": pct_time,
        "pace": pace_status(pct_f, pct_time),
        "details": {
            "limit_window_seconds": window_s,
            "reset_after_seconds": win.get("reset_after_seconds"),
            "raw_used_percent": win.get("used_percent"),
        },
    }


def _windows_from_usage(usage: dict[str, Any]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    rl = usage.get("rate_limit") or {}

    primary = rl.get("primary_window")
    if isinstance(primary, dict):
        windows.append(
            _window_from_rl(name="Primary Quota", kind="codex_primary", win=primary)
        )

    secondary = rl.get("secondary_window")
    if isinstance(secondary, dict):
        windows.append(
            _window_from_rl(name="Secondary Quota", kind="codex_secondary", win=secondary)
        )

    # Prefer 5h first, then weekly (stable card order)
    def sort_key(w: dict) -> tuple:
        name = (w.get("name") or "").lower()
        if "5 hour" in name or "5-hour" in name or name.startswith("5h"):
            return (0, name)
        if "week" in name:
            return (1, name)
        return (2, name)

    windows.sort(key=sort_key)

    # Optional extra limit blobs
    for extra_key, kind in (
        ("code_review_rate_limit", "code_review"),
        ("additional_rate_limits", "additional"),
    ):
        blob = usage.get(extra_key)
        if not blob:
            continue
        if isinstance(blob, dict) and blob.get("primary_window"):
            windows.append(
                _window_from_rl(
                    name="Code review",
                    kind=kind,
                    win=blob["primary_window"],
                )
            )
        elif isinstance(blob, list):
            for i, item in enumerate(blob):
                if isinstance(item, dict) and item.get("used_percent") is not None:
                    windows.append(
                        _window_from_rl(
                            name=item.get("limit_name") or f"Extra {i+1}",
                            kind=kind,
                            win=item,
                        )
                    )

    return windows


def _from_override(ov: dict[str, Any]) -> dict:
    windows_in = ov.get("windows") or []
    windows = []
    if windows_in:
        for w in windows_in:
            pct = w.get("pct_used")
            try:
                pct_f = float(pct) if pct is not None else None
            except (TypeError, ValueError):
                pct_f = None
            start = w.get("cycle_start")
            end = w.get("resets_at") or w.get("cycle_end")
            frac = cycle_fraction(start, end)
            pct_time = round(frac * 100.0, 1) if frac is not None else None
            if pct_f is None and w.get("used") is not None and w.get("limit"):
                try:
                    pct_f = 100.0 * float(w["used"]) / float(w["limit"])
                except (TypeError, ValueError, ZeroDivisionError):
                    pct_f = None
            windows.append(
                {
                    "name": w.get("name") or "cap",
                    "pct_used": pct_f,
                    "used": w.get("used"),
                    "limit": w.get("limit"),
                    "resets_at": end,
                    "cycle_start": start,
                    "pct_time_elapsed": pct_time,
                    "pace": pace_status(pct_f, pct_time),
                }
            )
    else:
        pct = ov.get("pct_used")
        try:
            pct_f = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct_f = None
        windows = [
            {
                "name": ov.get("name") or "Primary cap",
                "pct_used": pct_f,
                "resets_at": ov.get("resets_at") or ov.get("cycle_end"),
                "cycle_start": ov.get("cycle_start"),
                "pct_time_elapsed": None,
                "pace": "unknown",
            }
        ]
    return provider_record(
        "chatgpt",
        plan=ov.get("plan") or "Plus",
        windows=windows,
        status="ok",
        source="manual",
        notes=ov.get("notes") or "From manual_overrides.json",
        raw=ov,
    )


def collect() -> dict:
    ov = (read_manual_overrides().get("chatgpt") or {}).copy()
    if ov.get("force_manual"):
        return _from_override(ov)

    auth = _load_auth()
    if not auth:
        if ov:
            return _from_override(ov)
        return provider_record(
            "chatgpt",
            plan="Plus",
            status="login_required",
            source="codex",
            notes="No ~/.codex/auth.json — run `codex login` (Sign in with ChatGPT).",
            windows=[],
        )

    auth = _maybe_refresh(auth)
    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not access:
        return provider_record(
            "chatgpt",
            plan="Plus",
            status="login_required",
            source="codex",
            notes="Codex auth missing access_token — run `codex login`.",
            windows=[],
        )

    headers = _headers(access, account_id)
    errors: list[str] = []
    usage: Optional[dict] = None
    sub: Optional[dict] = None

    try:
        usage = _get_json(WHAM_USAGE_URL, headers)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # One refresh retry
            auth = _maybe_refresh(auth)
            tokens = auth.get("tokens") or {}
            access = tokens.get("access_token")
            if access:
                headers = _headers(access, tokens.get("account_id") or account_id)
                try:
                    usage = _get_json(WHAM_USAGE_URL, headers)
                except Exception as e2:  # noqa: BLE001
                    errors.append(f"wham/usage after refresh: {e2}")
            else:
                errors.append("wham/usage HTTP 401")
        else:
            errors.append(f"wham/usage HTTP {e.code}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"wham/usage: {type(e).__name__}: {e}")

    # Subscription metadata (plan / renew)
    try:
        q_account = account_id or (usage or {}).get("account_id") or ""
        # API accepts chatgpt account UUID from tokens; also try user id
        urls = []
        if tokens.get("account_id"):
            urls.append(f"{SUBSCRIPTIONS_URL}?account_id={urllib.parse.quote(tokens['account_id'])}")
        if usage and usage.get("user_id"):
            uid = usage["user_id"]
            urls.append(f"{SUBSCRIPTIONS_URL}?account_id={urllib.parse.quote(uid)}")
        if not urls and q_account:
            urls.append(f"{SUBSCRIPTIONS_URL}?account_id={urllib.parse.quote(q_account)}")
        for u in urls:
            try:
                sub = _get_json(u, headers)
                if sub:
                    break
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        errors.append(f"subscriptions: {type(e).__name__}: {e}")

    if not usage:
        if ov:
            rec = _from_override(ov)
            rec["notes"] = (rec.get("notes") or "") + " | live failed: " + "; ".join(errors)
            return rec
        return provider_record(
            "chatgpt",
            plan="Plus",
            status="error" if errors else "login_required",
            source="codex",
            notes="; ".join(errors) or "no usage payload",
            windows=[],
            raw={"errors": errors},
        )

    plan_type = (
        (sub or {}).get("plan_type")
        or usage.get("plan_type")
        or "plus"
    )
    plan_label = f"ChatGPT {str(plan_type).title()}"
    if sub and sub.get("active_until"):
        plan_label += f" · until {str(sub['active_until'])[:10]}"

    windows = _windows_from_usage(usage)
    rl = usage.get("rate_limit") or {}
    notes = (
        "Live Codex quota via chatgpt.com/backend-api/wham/usage "
        "(~/.codex ChatGPT OAuth). Not web chat message caps."
    )
    if rl.get("limit_reached"):
        notes += " LIMIT REACHED."
    if usage.get("credits"):
        c = usage["credits"]
        if c.get("has_credits") or (c.get("balance") and c.get("balance") != "0"):
            notes += f" Credits balance={c.get('balance')}."
    if errors:
        notes += " Partial: " + "; ".join(errors)

    return provider_record(
        "chatgpt",
        plan=plan_label,
        windows=windows,
        status="ok",
        source="codex",
        notes=notes,
        raw={
            "usage": usage,
            "subscription": sub,
            "errors": errors,
            "email": usage.get("email"),
        },
    )


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, default=str))
