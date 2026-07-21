"""Z.ai GLM Coding Plan usage via official monitor API."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from collectors.common import iso, load_env_key, pace_status, provider_record


ENDPOINT = "https://api.z.ai/api/monitor/usage/quota/limit"


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


def collect() -> dict:
    key = load_env_key("GLM_API_KEY", "ZAI_API_KEY", "ZHIPU_API_KEY", "ZHIPUAI_API_KEY")
    if not key:
        return provider_record(
            "zai",
            status="error",
            notes="No GLM_API_KEY / ZAI_API_KEY in env or .env",
            source="api",
        )
    req = urllib.request.Request(
        ENDPOINT,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return provider_record(
            "zai",
            status="error",
            notes=f"HTTP {e.code}: {e.reason}",
            source="api",
        )
    except Exception as e:  # noqa: BLE001
        return provider_record(
            "zai",
            status="error",
            notes=f"{type(e).__name__}: {e}",
            source="api",
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

    return provider_record(
        "zai",
        plan=f"GLM Coding Plan ({str(plan).title()})" if plan else "GLM Coding Plan",
        windows=windows,
        status="ok",
        notes="Official Z.ai quota API — labels mapped to coding-plan Usage UI",
        source="api",
        raw={"code": body.get("code"), "limits": data.get("limits")},
    )


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, default=str))
