"""Google AI Pro · Gemini Code Assist quota via Gemini CLI OAuth.

Uses ~/.gemini/oauth_creds.json (from `gemini` Sign in with Google).

Endpoints (verified 2026-07-21):
  POST https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist
  POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota

This is Gemini Code Assist / CLI request quotas under Google One AI Pro
(paidTier g1-pro-tier), NOT gemini.google.com web chat message caps.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from collectors.common import (
    DATA_DIR,
    cycle_fraction,
    iso,
    pace_status,
    provider_record,
    read_manual_overrides,
    resolve_oauth_client,
    utcnow,
)

GEMINI_HOME = Path.home() / ".gemini"
CREDS_PATH = GEMINI_HOME / "oauth_creds.json"
ACCOUNTS_PATH = GEMINI_HOME / "google_accounts.json"
PROJECT_CACHE_PATH = DATA_DIR / "gemini_project_cache.json"

TOKEN_URL = "https://oauth2.googleapis.com/token"
CODE_ASSIST = "https://cloudcode-pa.googleapis.com/v1internal"

# Card tracks: Pro / Flash / Flash-Lite "Latest" (product names).
# Each track lists preferred API modelIds first → first match in quota wins.
# Live cloudcode buckets (2026-07-21) use 3.1-pro-preview, 3-flash-preview,
# 3.1-flash-lite — not a separate "3.5-flash" id yet; Flash Latest maps to
# 3-flash-preview (product Flash Latest / 3.5 Flash).
LATEST_TRACKS: list[dict[str, Any]] = [
    {
        "label": "Pro Latest",
        "ids": [
            "gemini-3.1-pro-preview",
            "gemini-3-pro-preview",
            "gemini-2.5-pro",
        ],
    },
    {
        "label": "Flash Latest",
        "ids": [
            "gemini-3.5-flash",
            "gemini-3.5-flash-preview",
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
        ],
    },
    {
        "label": "Flash-Lite Latest",
        "ids": [
            "gemini-3.1-flash-lite",
            "gemini-3.1-flash-lite-preview",
            "gemini-3-flash-lite",
            "gemini-2.5-flash-lite",
        ],
    },
]
# Daily-style buckets only expose resetTime; assume 24h window for pace.
ASSUMED_WINDOW_HOURS = 24
UA = "hermes-subscription-monitor/1.0"


def _load_creds() -> Optional[dict[str, Any]]:
    if not CREDS_PATH.exists():
        return None
    try:
        data = json.loads(CREDS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("refresh_token"):
        return None
    return data


def _save_creds(data: dict[str, Any]) -> None:
    try:
        CREDS_PATH.write_text(json.dumps(data, indent=2) + "\n")
        CREDS_PATH.chmod(0o600)
    except OSError:
        pass


def _account_email() -> Optional[str]:
    if not ACCOUNTS_PATH.exists():
        return None
    try:
        d = json.loads(ACCOUNTS_PATH.read_text())
        return d.get("active") if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _load_cached_project() -> Optional[str]:
    if not PROJECT_CACHE_PATH.exists():
        return None
    try:
        d = json.loads(PROJECT_CACHE_PATH.read_text())
        p = d.get("project") if isinstance(d, dict) else None
        return str(p) if p else None
    except (OSError, json.JSONDecodeError):
        return None


def _save_cached_project(project: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PROJECT_CACHE_PATH.write_text(
            json.dumps({"project": project, "saved_at": iso()}, indent=2) + "\n"
        )
    except OSError:
        pass


def _expiry_epoch(creds: dict[str, Any]) -> Optional[float]:
    exp = creds.get("expiry_date")
    if exp is None:
        return None
    try:
        exp_f = float(exp)
    except (TypeError, ValueError):
        return None
    # Gemini CLI stores ms since epoch
    if exp_f > 1e12:
        return exp_f / 1000.0
    return exp_f


def _ensure_access_token(creds: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Return (access_token, error). Refreshes if expiring within 5 minutes."""
    access = creds.get("access_token")
    exp = _expiry_epoch(creds)
    now = time.time()
    if access and exp is not None and exp > now + 300:
        return str(access), None
    refresh = creds.get("refresh_token")
    if not refresh:
        return None, "no refresh_token in ~/.gemini/oauth_creds.json"

    client_id, client_secret, src = resolve_oauth_client("gemini")
    if not client_id or not client_secret:
        return (
            None,
            "Gemini OAuth client not found — install @google/gemini-cli, or set "
            "GEMINI_OAUTH_CLIENT_ID/SECRET, or data/oauth_clients.json "
            f"(tried: {src})",
        )

    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tok = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        return None, f"oauth refresh HTTP {e.code}: {detail}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return None, f"oauth refresh failed: {e}"

    new_access = tok.get("access_token")
    if not new_access:
        return None, "oauth refresh returned no access_token"

    creds["access_token"] = new_access
    if tok.get("id_token"):
        creds["id_token"] = tok["id_token"]
    if tok.get("refresh_token"):
        creds["refresh_token"] = tok["refresh_token"]
    if tok.get("expires_in") is not None:
        try:
            creds["expiry_date"] = int((now + float(tok["expires_in"])) * 1000)
        except (TypeError, ValueError):
            pass
    _save_creds(creds)
    return str(new_access), None


def _post_json(access: str, method: str, payload: dict[str, Any]) -> tuple[int, Any]:
    url = f"{CODE_ASSIST}:{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read()[:800].decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"error": body}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return 0, {"error": str(e)}


def _short_model(model_id: str) -> str:
    m = model_id or "model"
    m = m.removeprefix("gemini-")
    return m


def _bucket_window(
    bucket: dict[str, Any], *, display_name: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Map retrieveUserQuota bucket → dashboard window."""
    frac_rem = bucket.get("remainingFraction")
    if frac_rem is None and bucket.get("remainingAmount") is None:
        return None
    try:
        remaining = float(frac_rem) if frac_rem is not None else None
    except (TypeError, ValueError):
        remaining = None
    if remaining is None:
        return None
    remaining = max(0.0, min(1.0, remaining))
    pct_used = round((1.0 - remaining) * 100.0, 2)

    reset_raw = bucket.get("resetTime")
    resets_at = None
    pct_time = None
    if isinstance(reset_raw, str) and reset_raw:
        try:
            end = datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            resets_at = end.isoformat()
            start = end - timedelta(hours=ASSUMED_WINDOW_HOURS)
            pct_time = cycle_fraction(start.isoformat(), end.isoformat())
            if pct_time is not None:
                pct_time = round(pct_time * 100.0, 2)
        except ValueError:
            resets_at = reset_raw

    model = bucket.get("modelId") or "unknown"
    token_type = bucket.get("tokenType") or "REQUESTS"
    if display_name:
        name = display_name
    else:
        name = f"{_short_model(str(model))} ({token_type.lower()})"

    return {
        "name": name,
        "pct_used": pct_used,
        "pct_remaining": round(remaining * 100.0, 2),
        "resets_at": resets_at,
        "pct_time_elapsed": pct_time,
        "pace": pace_status(pct_used, pct_time),
        "unit": token_type,
        "model_id": model,
        "remaining_fraction": remaining,
        "assumed_window_hours": ASSUMED_WINDOW_HOURS if pct_time is not None else None,
    }


def _index_buckets(buckets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """modelId → best REQUESTS (preferred) bucket."""
    by_id: dict[str, dict[str, Any]] = {}
    for b in buckets:
        mid = str(b.get("modelId") or "")
        if not mid:
            continue
        if mid not in by_id:
            by_id[mid] = b
        elif str(b.get("tokenType") or "").upper() == "REQUESTS":
            by_id[mid] = b
    return by_id


def _select_latest_windows(buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only Pro / Flash / Flash-Lite Latest tracks (3 bars max)."""
    by_id = _index_buckets(buckets)
    windows: list[dict[str, Any]] = []
    for track in LATEST_TRACKS:
        label = str(track["label"])
        ids = list(track.get("ids") or [])
        chosen = None
        for mid in ids:
            if mid in by_id:
                chosen = by_id[mid]
                break
        if not chosen:
            continue
        w = _bucket_window(chosen, display_name=label)
        if w:
            # Keep underlying id visible in raw via model_id already
            windows.append(w)
    return windows


def _manual_fallback(reason: str, status: str = "error") -> dict[str, Any]:
    overrides = read_manual_overrides().get("gemini") or read_manual_overrides().get(
        "google_ai"
    ) or {}
    if overrides.get("windows") or overrides.get("plan"):
        return provider_record(
            "gemini",
            plan=overrides.get("plan") or "Google AI Pro",
            windows=list(overrides.get("windows") or []),
            status="ok",
            source="manual",
            notes=f"Manual override ({reason})",
            raw={"override": True, "reason": reason},
        )
    return provider_record(
        "gemini",
        plan=None,
        windows=[],
        status=status,
        source="gemini-cli",
        notes=reason,
    )


def collect() -> dict[str, Any]:
    overrides = read_manual_overrides().get("gemini") or read_manual_overrides().get(
        "google_ai"
    ) or {}
    if overrides.get("force_manual"):
        return provider_record(
            "gemini",
            plan=overrides.get("plan") or "Google AI Pro",
            windows=list(overrides.get("windows") or []),
            status="ok",
            source="manual",
            notes="force_manual override",
            raw={"override": True},
        )

    creds = _load_creds()
    if not creds:
        return _manual_fallback(
            "No ~/.gemini/oauth_creds.json — run `gemini` and Sign in with Google "
            "(use the Google AI Pro account).",
            status="login_required",
        )

    access, err = _ensure_access_token(creds)
    if not access:
        return _manual_fallback(
            f"{err} — re-auth: run `gemini` interactive login.",
            status="login_required",
        )

    meta = {
        "ideType": "GEMINI_CLI",
        "platform": "LINUX_AMD64",
        "pluginType": "GEMINI",
    }
    code, load = _post_json(access, "loadCodeAssist", {"metadata": meta})
    if code == 401:
        # force refresh once
        creds["expiry_date"] = 0
        access, err = _ensure_access_token(creds)
        if not access:
            return _manual_fallback(f"401 then refresh failed: {err}", status="login_required")
        code, load = _post_json(access, "loadCodeAssist", {"metadata": meta})

    if code != 200 or not isinstance(load, dict) or load.get("error"):
        msg = load if isinstance(load, str) else json.dumps(load)[:300]
        return _manual_fallback(f"loadCodeAssist HTTP {code}: {msg}")

    # Resolve project: API → override/env → last cached success
    project = load.get("cloudaicompanionProject")
    if isinstance(project, dict):
        project = project.get("id") or project.get("name")
    project = str(project) if project else None
    if not project:
        project = (
            overrides.get("project")
            or os.environ.get("GEMINI_CLOUD_PROJECT")
            or _load_cached_project()
        )
    if project:
        _save_cached_project(project)

    # Live key is paidTier (singular); also accept paidTiers list/dict.
    paid_obj = load.get("paidTier")
    if not isinstance(paid_obj, dict):
        pt = load.get("paidTiers")
        if isinstance(pt, dict):
            paid_obj = pt
        elif isinstance(pt, list) and pt and isinstance(pt[0], dict):
            paid_obj = pt[0]
        else:
            paid_obj = None

    current = load.get("currentTier") if isinstance(load.get("currentTier"), dict) else {}
    tier_name = None
    if isinstance(paid_obj, dict):
        tier_name = paid_obj.get("name") or paid_obj.get("id")
    if not tier_name and current:
        tier_name = current.get("name") or current.get("id")
    plan = tier_name or "Gemini"
    paid_id = paid_obj.get("id") if isinstance(paid_obj, dict) else None
    if paid_id == "g1-pro-tier" or (isinstance(tier_name, str) and "AI Pro" in tier_name):
        plan = "Google One AI Pro"
    elif paid_id:
        plan = f"{plan} ({paid_id})"

    email = _account_email()
    if email:
        plan = f"{plan} · {email}"

    windows: list[dict[str, Any]] = []
    quota_raw: Any = None
    if project:
        qcode, quota = _post_json(access, "retrieveUserQuota", {"project": project})
        quota_raw = quota
        if qcode == 200 and isinstance(quota, dict):
            buckets = quota.get("buckets") or []
            if isinstance(buckets, list):
                windows = _select_latest_windows(
                    [x for x in buckets if isinstance(x, dict)]
                )
        elif qcode not in (200,):
            # still return tier info
            pass

    notes_parts = [
        "Gemini Code Assist / CLI request quotas (Google AI Pro). "
        "Not gemini.google.com web chat caps.",
        f"project={project}" if project else "no cloudaicompanionProject",
    ]
    if paid_id:
        notes_parts.append(f"paidTier={paid_id}")
    if current.get("id"):
        notes_parts.append(f"codeAssist={current.get('id')}")
    manage = load.get("manageSubscriptionUri")
    if manage:
        notes_parts.append("manage: Google One settings")

    n_buckets = 0
    if isinstance(quota_raw, dict) and isinstance(quota_raw.get("buckets"), list):
        n_buckets = len(quota_raw["buckets"])
    if windows:
        mapped = ", ".join(
            f"{w.get('name')}→{w.get('model_id')}" for w in windows if w.get("model_id")
        )
        notes_parts.append(f"Latest tracks: {mapped}")
    if n_buckets and n_buckets != len(windows):
        notes_parts.append(f"API has {n_buckets} buckets; card shows Latest only")

    status = "ok"
    if not windows and not project:
        status = "error"
        notes_parts.insert(0, "No project/quota — may need to open Gemini CLI once.")

    return provider_record(
        "gemini",
        plan=plan,
        windows=windows,
        status=status,
        source="gemini-cli",
        notes=" · ".join(notes_parts),
        raw={
            "loadCodeAssist": {
                "currentTier": load.get("currentTier"),
                "paidTier": paid_obj,
                "cloudaicompanionProject": project,
                "gcpManaged": load.get("gcpManaged"),
            },
            "quota_bucket_count": n_buckets,
            "windows_shown": [w.get("model_id") for w in windows],
        },
    )


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, default=str))
