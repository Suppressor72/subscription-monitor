"""Z.ai GLM Coding Plan usage via official monitor API.

Banked usage-limit resets (read-only awareness):
  GET https://zcode.z.ai/api/v1/coding-plan/reset/status
  Auth = ZCode CLI credentials (~/.zcode/v2/credentials.json): the
  `zcodejwttoken` (Authorization) + `oauth:zai:access_token`
  (X-Bigmodel-Authorization) pair the CLI itself uses. Values are
  AES-256-GCM encrypted at rest (enc:v1:); the key is derived from
  ZCODE_CREDENTIAL_SECRET or the CLI's deterministic fallback.
  Never calls /reset/use or /reset/opportunity (those mutate state).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from collectors.common import (
    banked_resets_record,
    iso,
    load_env_key,
    pace_status,
    provider_record,
)


ENDPOINT = "https://api.z.ai/api/monitor/usage/quota/limit"
RESET_STATUS_URL = "https://zcode.z.ai/api/v1/coding-plan/reset/status"


def _ms_to_iso(ms: Any) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _tool_quota_codes(lim: dict) -> set[str]:
    codes = set()
    for d in lim.get("usageDetails") or []:
        code = (d.get("modelCode") or "").lower()
        if code:
            codes.add(code)
    return codes


def _classify_limit(lim: dict) -> str:
    """Map API limit rows to UI labels on z.ai/.../coding-plan/personal/usage.

    Ground-truth (2026-07-21 UI vs API):
      - TOKENS_LIMIT unit=3 number=5, percentage N  →  "5 Hour Quota" (N%)
      - TIME_LIMIT with usageDetails search-prime/web-reader/zread,
        nextResetTime matching UI "Reset Time"     →  "Monthly Tools Quota"
    API type names are misleading relative to the product UI.
    Dashboard bar names use the shared "{Period} Quota" pattern.
    """
    t = (lim.get("type") or "").upper()
    codes = _tool_quota_codes(lim)
    toolish = {"search-prime", "web-reader", "zread", "search", "reader"}
    if codes & toolish or (
        t == "TIME_LIMIT" and lim.get("usageDetails") is not None
    ):
        return "monthly_tools"
    if t == "TOKENS_LIMIT" or (lim.get("number") == 5 and t != "TIME_LIMIT"):
        return "five_hour"
    if t == "TIME_LIMIT":
        # Fallback: bare TIME_LIMIT without tool details → treat as 5h
        return "five_hour"
    return "other"


def _label_for_kind(kind: str, lim: dict) -> str:
    if kind == "five_hour":
        return "5 Hour Quota"
    if kind == "monthly_tools":
        return "Monthly Tools Quota"
    t = (lim.get("type") or "quota").upper()
    return f"{t} ({lim.get('number')}×{lim.get('unit')})"


def _zcode_credentials_path() -> Path:
    if os.environ.get("ZCODE_CREDENTIALS"):
        return Path(os.environ["ZCODE_CREDENTIALS"]).expanduser()
    if os.environ.get("ZCODE_HOME"):
        return Path(os.environ["ZCODE_HOME"]).expanduser() / "v2" / "credentials.json"
    return Path.home() / ".zcode" / "v2" / "credentials.json"


def _node_platform() -> str:
    # Mirror Node os.platform() (used by ZCode's fallback secret).
    import sys

    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform.startswith("darwin"):
        return "darwin"
    return sys.platform or "linux"


def _zcode_secret(env: dict) -> str:
    if env.get("ZCODE_CREDENTIAL_SECRET"):
        return env["ZCODE_CREDENTIAL_SECRET"]
    username = "unknown"
    try:
        import pwd

        username = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        username = env.get("USER") or env.get("USERNAME") or username
    return f"zcode-credential-fallback:{_node_platform()}:{Path.home()}:{username}"


def _decrypt_enc_v1(value: str, env: dict) -> Optional[str]:
    """Decrypt ZCode's `enc:v1:<iv>.<tag>.<ct>` AES-256-GCM credential blob."""
    if not value.startswith("enc:v1:"):
        return value
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise RuntimeError("cryptography package required for ZCode credentials")
    parts = value[len("enc:v1:") :].split(".")
    if len(parts) != 3:
        return None

    def _b64u(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * ((4 - len(s) % 4) % 4))

    key = hashlib.sha256(_zcode_secret(env).encode()).digest()
    try:
        iv, tag, ct = (_b64u(p) for p in parts)
        return AESGCM(key).decrypt(iv, ct + tag, None).decode()
    except Exception:  # noqa: BLE001
        return None


def _load_zcode_auth() -> tuple[Optional[str], Optional[str]]:
    """(zcode_jwt, coding_plan_token) from ZCode CLI credential storage."""
    path = _zcode_credentials_path()
    if not path.exists():
        return None, None
    try:
        creds = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    env = dict(os.environ)
    jwt = _decrypt_enc_v1(creds.get("zcodejwttoken") or "", env)
    maas = _decrypt_enc_v1(
        creds.get("oauth:zai:access_token")
        or creds.get("oauth:bigmodel:access_token")
        or "",
        env,
    )
    jwt = jwt.strip() if jwt and jwt.strip() else None
    maas = maas.strip() if maas and maas.strip() else None
    return jwt, maas


def _fetch_banked_resets() -> dict:
    """GET coding-plan reset status (read-only). Raises with a user-facing note."""
    jwt, maas = _load_zcode_auth()
    if not jwt or not maas:
        raise RuntimeError(
            "no ZCode credentials (~/.zcode/v2/credentials.json) — sign in to Z.ai in ZCode"
        )
    headers = {
        "Authorization": jwt if jwt.startswith("Bearer ") else f"Bearer {jwt}",
        "X-Bigmodel-Authorization": maas,
        "Bigmodel-Target-Type": os.environ.get("ZCODE_TARGET_TYPE", "PERSONAL"),
        "Accept": "application/json",
    }
    if os.environ.get("ZCODE_ORGANIZATION_ID"):
        headers["Bigmodel-Target-Type"] = "TEAM"
        headers["Bigmodel-Organization"] = os.environ["ZCODE_ORGANIZATION_ID"]
        if os.environ.get("ZCODE_PROJECT_ID"):
            headers["Bigmodel-Project"] = os.environ["ZCODE_PROJECT_ID"]
    req = urllib.request.Request(RESET_STATUS_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")[:120]
        except Exception:  # noqa: BLE001
            pass
        if e.code == 401:
            raise RuntimeError("reset status HTTP 401 — re-login to Z.ai in ZCode")
        raise RuntimeError(f"reset status HTTP {e.code}{(' ' + detail) if detail else ''}")
    data = body.get("data") or {}
    if body.get("code") not in (0, 200):
        raise RuntimeError(f"reset status code={body.get('code')} {body.get('msg')}")

    items: list[dict] = []
    for entry in data.get("available_five_hour_resets") or []:
        items.append(
            {
                "status": "available",
                "expires_at": _ms_to_iso(entry.get("expire_at")),
                "title": "5-hour reset",
            }
        )
    for entry in data.get("available_week_resets") or []:
        items.append(
            {
                "status": "available",
                "expires_at": _ms_to_iso(entry.get("expire_at")),
                "title": "Weekly reset",
            }
        )
    return banked_resets_record(items=items)


def collect() -> dict:
    key = load_env_key("GLM_API_KEY", "ZAI_API_KEY", "ZHIPU_API_KEY", "ZHIPUAI_API_KEY")

    # Banked usage-limit resets are independent of the quota API key:
    # they authenticate with the ZCode CLI login, fail soft either way.
    banked: Optional[dict] = None
    banked_err: Optional[str] = None
    try:
        banked = _fetch_banked_resets()
    except Exception as e:  # noqa: BLE001
        banked_err = str(e)

    if not key:
        notes = "No GLM_API_KEY / ZAI_API_KEY in env or .env"
        if banked_err:
            notes += f" | {banked_err}"
        return provider_record(
            "zai",
            status="error",
            notes=notes,
            source="api",
            banked_resets=banked,
        )
    req = urllib.request.Request(
        ENDPOINT,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        notes = f"HTTP {e.code}: {e.reason}"
        if banked_err:
            notes += f" | {banked_err}"
        return provider_record(
            "zai",
            status="error",
            notes=notes,
            source="api",
            banked_resets=banked,
        )
    except Exception as e:  # noqa: BLE001
        notes = f"{type(e).__name__}: {e}"
        if banked_err:
            notes += f" | {banked_err}"
        return provider_record(
            "zai",
            status="error",
            notes=notes,
            source="api",
            banked_resets=banked,
        )

    data = body.get("data") or {}
    plan = data.get("level") or "unknown"
    windows = []
    now = datetime.now(timezone.utc)
    for lim in data.get("limits") or []:
        pct = lim.get("percentage")
        try:
            pct_f = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct_f = None
        resets = _ms_to_iso(lim.get("nextResetTime"))
        kind = _classify_limit(lim)
        label = _label_for_kind(kind, lim)
        pct_time = None
        if kind == "five_hour" and resets:
            try:
                end = datetime.fromisoformat(resets)
                # Rolling 5h window ending at nextResetTime when present & near.
                # If reset is >6h away, API may be carrying a different clock —
                # still compute against a 5h window ending at resets_at.
                start_ts = end.timestamp() - 5 * 3600
                elapsed = now.timestamp() - start_ts
                pct_time = max(0.0, min(100.0, (elapsed / (5 * 3600)) * 100.0))
            except Exception:  # noqa: BLE001
                pct_time = None
        elif kind == "monthly_tools" and resets:
            # Monthly tool pool: only end known from API; no reliable start → no pace
            pct_time = None

        # Prefer UI-scale numbers: currentValue/usage when present (tool quota)
        used = lim.get("currentValue")
        limit = lim.get("usage")
        remaining = lim.get("remaining")
        windows.append(
            {
                "name": label,
                "kind": kind,
                "pct_used": pct_f,
                "used": used,
                "limit": limit,
                "remaining": remaining,
                "resets_at": resets,
                "pct_time_elapsed": round(pct_time, 1) if pct_time is not None else None,
                "pace": pace_status(pct_f, pct_time),
                "details": lim.get("usageDetails"),
                "api_type": lim.get("type"),
            }
        )

    # Stable card order matching UI: 5h first, monthly tools second
    order = {"five_hour": 0, "monthly_tools": 1, "other": 9}
    windows.sort(key=lambda w: order.get(w.get("kind") or "other", 9))

    notes = "Official Z.ai quota API — labels mapped to coding-plan Usage UI"
    if banked_err:
        notes += f" | {banked_err}"

    return provider_record(
        "zai",
        plan=f"GLM Coding Plan ({str(plan).title()})" if plan else "GLM Coding Plan",
        windows=windows,
        status="ok",
        notes=notes,
        source="api",
        banked_resets=banked,
        raw={"code": body.get("code"), "limits": data.get("limits")},
    )


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, default=str))
