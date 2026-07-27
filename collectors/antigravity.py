"""Antigravity (agy) shared model-group quotas via daily-cloudcode-pa.

Matches agy TUI `/usage`:
  GEMINI MODELS — Flash + Pro share weekly + 5-hour WTUS limits
  CLAUDE AND GPT MODELS — Opus/Sonnet/GPT-OSS share the same shape

Auth (two paths, tried in order):
  1. ~/.antigravity_cockpit/credentials.json — Antigravity Cockpit IDE
     extension (Cursor/VS Code). Multi-account, stores refreshToken per
     account. Preferred when present.
  2. GNOME keyring (Linux) / macOS keychain — the `agy` CLI stores its
     consumer OAuth token under service='gemini', username='antigravity'.
     Single-account, same daily-cloudcode-pa API.

Gemini CLI oauth_creds are a different product surface (Code Assist
REQUESTS) — see collectors/gemini.py.

API (verified 2026-07-21):
  POST https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels
  UA must look like antigravity (e.g. antigravity/1.15.8 linux/amd64)
  Each model has quotaInfo.{remainingFraction, resetTime}
  Models in a group share the same remainingFraction + resetTime.

Note: the public JSON only exposes one remainingFraction per model — the
binding window (currently the 5-hour limit when both apply). Window kind
is labeled from resetTime distance (≤6h → Five Hour, ≤8d → Weekly).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .common import (
    cycle_fraction,
    pace_status,
    provider_record,
    read_manual_overrides,
    resolve_oauth_client,
)

PROVIDER = "antigravity"

_DEFAULT_ENDPOINT = "https://daily-cloudcode-pa.googleapis.com"
_UA = "antigravity/1.15.8 linux/amd64"

# Skip autocomplete / internal chat ids — only cascade agent models.
_SKIP_PREFIXES = ("tab_", "chat_")

# Group display order + short prefix (agy /usage groups).
_GROUP_ORDER = (
    ("gemini", "Gemini"),
    ("claude_gpt", "Claude + GPT"),
)


def _creds_path() -> Path:
    override = os.environ.get("ANTIGRAVITY_CREDS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".antigravity_cockpit" / "credentials.json"


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _load_cockpit_account() -> tuple[dict[str, Any], str, Path]:
    """Return (account_dict, email, path). Prefer activeEmail, else first account."""
    path = _creds_path()
    if not path.is_file():
        raise FileNotFoundError(f"missing {path}")
    data = json.loads(path.read_text())
    accounts = data.get("accounts") or {}
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError("no accounts in cockpit credentials")
    email = (
        os.environ.get("ANTIGRAVITY_EMAIL")
        or data.get("activeEmail")
        or next(iter(accounts))
    )
    acc = accounts.get(email)
    if not acc:
        raise ValueError(f"account {email!r} not in cockpit credentials")
    return acc, email, path


def _save_access(path: Path, email: str, access: str, expires_at_ms: int) -> None:
    try:
        data = json.loads(path.read_text())
        acc = (data.get("accounts") or {}).get(email)
        if not acc:
            return
        acc["accessToken"] = access
        acc["expireAt"] = expires_at_ms
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def _refresh_oauth_token(
    refresh_token: str, *, save_fn=None
) -> tuple[str, int]:
    """Exchange a refresh_token for a fresh access_token.

    Returns (access_token, expires_at_ms).  *save_fn* is an optional callback
    invoked as save_fn(access, expires_at_ms) so cockpit JSON / keyring can
    persist the refreshed token their own way.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    client_id, client_secret, src = resolve_oauth_client("antigravity")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Antigravity OAuth client not found — install Antigravity Cockpit "
            "or agy CLI, or set ANTIGRAVITY_OAUTH_CLIENT_ID/SECRET, or "
            f"data/oauth_clients.json (tried: {src})"
        )

    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        tok = json.loads(resp.read().decode())
    access = tok["access_token"]
    expires_in = int(tok.get("expires_in") or 3600)
    expires_at_ms = now_ms + expires_in * 1000
    if save_fn:
        try:
            save_fn(access, expires_at_ms)
        except Exception:
            pass
    return access, expires_at_ms


def _ensure_access_token(acc: dict[str, Any], email: str, path: Path) -> str:
    """Cockpit JSON path: check cached accessToken, refresh if stale."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    expire_at = int(acc.get("expireAt") or 0)
    token = acc.get("accessToken") or ""
    if token and expire_at and expire_at > now_ms + 5 * 60 * 1000:
        return token

    refresh = acc.get("refreshToken")
    if not refresh:
        raise RuntimeError("cockpit account has no refreshToken — re-login in cockpit")

    access, expires_at_ms = _refresh_oauth_token(
        refresh,
        save_fn=lambda a, e: _save_access(path, email, a, e),
    )
    acc["accessToken"] = access
    acc["expireAt"] = expires_at_ms
    return access


# ---------------------------------------------------------------------------
# Keyring auth (agy CLI)
# ---------------------------------------------------------------------------

# Sentinel returned by _resolve_creds_source
_COCKPIT = "cockpit"
_KEYRING = "keyring"


def _load_keyring_account() -> tuple[dict[str, Any], str]:
    """Read agy CLI token from GNOME keyring / macOS keychain.

    Returns (account_dict, email).  The account_dict mimics the cockpit shape:
        {"refreshToken": ..., "accessToken": ..., "expireAt": ...}
    so _ensure_access_token works unchanged.

    Raises RuntimeError if no keyring backend is available or the entry
    is absent.
    """
    raw = _read_keyring_secret()
    if not raw:
        raise RuntimeError("no Antigravity entry in keyring")
    data = json.loads(raw)

    token = data.get("token") or {}
    refresh = token.get("refresh_token")
    if not refresh:
        raise RuntimeError("keyring token has no refresh_token — re-login via agy CLI")

    access = token.get("access_token") or ""
    expiry_str = token.get("expiry") or ""
    expire_at_ms = 0
    if expiry_str:
        try:
            dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
            expire_at_ms = int(dt.timestamp() * 1000)
        except ValueError:
            pass

    # Try to get email from id_token JWT payload
    email = os.environ.get("ANTIGRAVITY_EMAIL") or ""
    if not email:
        id_tok = data.get("id_token") or ""
        if id_tok:
            try:
                import base64
                payload = id_tok.split(".")[1]
                payload += "=" * (-len(payload) % 4)  # pad
                claims = json.loads(base64.urlsafe_b64decode(payload))
                email = claims.get("email") or ""
            except Exception:
                pass

    acc = {
        "refreshToken": refresh,
        "accessToken": access,
        "expireAt": expire_at_ms,
    }
    return acc, email or "agy-cli"


def _read_keyring_secret() -> str | None:
    """Return the raw JSON string stored in the OS keyring, or None.

    Strategy (tried in order):
      1. Direct import of gi (if PyGObject is in the venv — rare)
      2. Subprocess to system python3 (has gi on GNOME desktops)
      3. keyring library (macOS keychain / other backends)
    """
    # --- Strategy 1: direct gi import (venv with PyGObject) ---
    raw = _read_keyring_via_gi()
    if raw:
        return raw

    # --- Strategy 2: subprocess to system python3 ---
    raw = _read_keyring_via_subprocess()
    if raw:
        return raw

    # --- Strategy 3: keyring library (macOS) ---
    try:
        import keyring  # type: ignore[import-untyped]

        val = keyring.get_password("gemini", "antigravity")
        if val:
            return val
    except ImportError:
        pass

    return None


def _read_keyring_via_gi() -> str | None:
    """Try reading keyring via PyGObject (gi) in the current process."""
    try:
        import gi  # type: ignore[import-untyped]
        gi.require_version("Secret", "1")
        from gi.repository import Secret  # type: ignore[import-untyped]

        service = Secret.Service.get_sync(Secret.ServiceFlags.LOAD_COLLECTIONS, None)
        for coll in service.get_collections():
            coll.load_items_sync(None)
            for item in coll.get_items():
                attrs = item.get_attributes() or {}
                if (
                    attrs.get("service") == "gemini"
                    and attrs.get("username") == "antigravity"
                ):
                    item.load_secret_sync(None)
                    secret = item.get_secret()
                    if secret:
                        return secret.get_text()
        return None
    except Exception:
        return None


def _read_keyring_via_subprocess() -> str | None:
    """Call system python3 (which has gi on GNOME desktops) to read keyring.

    We use a small inline script that prints the secret JSON to stdout.
    Falls back through common system python paths.
    """
    import subprocess

    script = (
        "import json\n"
        "try:\n"
        "    import gi; gi.require_version('Secret','1')\n"
        "    from gi.repository import Secret\n"
        "    svc = Secret.Service.get_sync(Secret.ServiceFlags.LOAD_COLLECTIONS, None)\n"
        "    for c in svc.get_collections():\n"
        "        c.load_items_sync(None)\n"
        "        for it in c.get_items():\n"
        "            a = it.get_attributes() or {}\n"
        "            if a.get('service')=='gemini' and a.get('username')=='antigravity':\n"
        "                it.load_secret_sync(None)\n"
        "                s = it.get_secret()\n"
        "                if s: print(s.get_text()); raise SystemExit(0)\n"
        "except SystemExit: raise\n"
        "except Exception: pass\n"
    )
    for py in ("/usr/bin/python3", "python3"):
        try:
            result = subprocess.run(
                [py, "-c", script],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            continue
    return None


def _save_keyring_access(access: str, expires_at_ms: int) -> None:
    """Persist refreshed access_token back to keyring (best-effort)."""
    try:
        raw = _read_keyring_secret()
        if not raw:
            return
        data = json.loads(raw)
        token = data.get("token") or {}
        token["access_token"] = access
        token["expiry"] = datetime.fromtimestamp(
            expires_at_ms / 1000, tz=timezone.utc
        ).isoformat()
        data["token"] = token

        # Try gi direct, then subprocess
        try:
            import gi  # type: ignore[import-untyped]
            gi.require_version("Secret", "1")
            from gi.repository import Secret  # type: ignore[import-untyped]

            schema = Secret.Schema.new(
                "org.freedesktop.Secret.Generic",
                Secret.SchemaFlags.DONT_MATCH_NAME,
                {"service": Secret.SchemaAttributeType.STRING,
                 "username": Secret.SchemaAttributeType.STRING},
            )
            Secret.password_store_sync(
                schema,
                {"service": "gemini", "username": "antigravity"},
                Secret.COLLECTION_DEFAULT,
                "Antigravity CLI token",
                json.dumps(data),
                None,
            )
            return
        except Exception:
            pass

        # Fallback: subprocess to system python3
        import subprocess
        store_script = (
            "import json, sys\n"
            "data = json.loads(sys.stdin.read())\n"
            "import gi; gi.require_version('Secret','1')\n"
            "from gi.repository import Secret\n"
            "schema = Secret.Schema.new(\n"
            "    'org.freedesktop.Secret.Generic',\n"
            "    Secret.SchemaFlags.DONT_MATCH_NAME,\n"
            "    {'service': Secret.SchemaAttributeType.STRING,\n"
            "     'username': Secret.SchemaAttributeType.STRING})\n"
            "Secret.password_store_sync(\n"
            "    schema, {'service':'gemini','username':'antigravity'},\n"
            "    Secret.COLLECTION_DEFAULT, 'Antigravity CLI token',\n"
            "    json.dumps(data), None)\n"
        )
        for py in ("/usr/bin/python3", "python3"):
            try:
                subprocess.run(
                    [py, "-c", store_script],
                    input=json.dumps(data),
                    text=True,
                    timeout=10,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    capture_output=True,
                )
                return
            except (subprocess.SubprocessError, FileNotFoundError):
                continue
    except Exception:
        pass  # best-effort; next cycle re-reads whatever agy last wrote


def _resolve_creds() -> tuple[dict[str, Any], str, str, Any]:
    """Try cockpit JSON first, then keyring.

    Returns (account_dict, email, source_tag, opaque_path_or_obj).
    Raises if neither source is available.
    """
    # Path 1: cockpit JSON
    try:
        acc, email, path = _load_cockpit_account()
        return acc, email, _COCKPIT, path
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        pass

    # Path 2: keyring (agy CLI)
    acc, email = _load_keyring_account()
    return acc, email, _KEYRING, None


def _ensure_keyring_access_token(acc: dict[str, Any]) -> str:
    """Keyring path: check cached access_token, refresh if stale."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    expire_at = int(acc.get("expireAt") or 0)
    token = acc.get("accessToken") or ""
    if token and expire_at and expire_at > now_ms + 5 * 60 * 1000:
        return token

    refresh = acc.get("refreshToken")
    if not refresh:
        raise RuntimeError("keyring token has no refreshToken — re-login via agy CLI")

    access, expires_at_ms = _refresh_oauth_token(
        refresh,
        save_fn=_save_keyring_access,
    )
    acc["accessToken"] = access
    acc["expireAt"] = expires_at_ms
    return access


def _post_json(url: str, access: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload if payload is not None else {}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
            "User-Agent": _UA,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} {url}: {body}") from e


def _classify_group(model_id: str, model: dict[str, Any]) -> str | None:
    """Return group key or None to skip."""
    mid = (model_id or "").lower()
    if any(mid.startswith(p) for p in _SKIP_PREFIXES):
        return None
    prov = (
        (model.get("modelProvider") or model.get("apiProvider") or "")
        .strip()
        .lower()
    )
    if prov == "google":
        return "gemini"
    if prov in ("anthropic", "openai"):
        return "claude_gpt"
    if "gemini" in mid or mid.startswith("gem"):
        return "gemini"
    if mid.startswith("claude") or mid.startswith("gpt") or "gpt-oss" in mid:
        return "claude_gpt"
    return None


def _window_kind_and_name(reset: datetime | None, now: datetime) -> tuple[str, str, timedelta]:
    """Infer five_hour vs weekly from reset distance (API only returns one bucket).

    Labels follow the dashboard-wide "{Period} Quota" pattern.
    """
    if not reset:
        return "unknown", "Quota", timedelta(hours=5)
    delta = reset - now
    if delta <= timedelta(hours=6):
        return "five_hour", "5 Hour Quota", timedelta(hours=5)
    if delta <= timedelta(days=8):
        return "weekly", "Weekly Quota", timedelta(days=7)
    return "unknown", "Quota", timedelta(hours=5)


def _group_windows(models: dict[str, Any]) -> list[dict[str, Any]]:
    """Collapse per-model quotaInfo into one window per product group."""
    now = datetime.now(timezone.utc)
    buckets: dict[str, list[tuple[float, str | None, str]]] = defaultdict(list)
    labels: dict[str, set[str]] = defaultdict(set)

    for mid, m in (models or {}).items():
        if not isinstance(m, dict):
            continue
        gkey = _classify_group(mid, m)
        if not gkey:
            continue
        q = m.get("quotaInfo") or {}
        if "remainingFraction" not in q:
            continue
        try:
            frac = float(q["remainingFraction"])
        except (TypeError, ValueError):
            continue
        frac = max(0.0, min(1.0, frac))
        reset = q.get("resetTime")
        buckets[gkey].append((frac, reset if isinstance(reset, str) else None, mid))
        lab = m.get("displayName") or mid
        labels[gkey].add(str(lab))

    windows: list[dict[str, Any]] = []
    for gkey, gname in _GROUP_ORDER:
        items = buckets.get(gkey) or []
        if not items:
            continue
        frac = min(t[0] for t in items)
        resets = [t[1] for t in items if t[1]]
        reset_iso = None
        reset_dt = None
        if resets:
            parsed = [(r, _parse_dt(r)) for r in resets]
            parsed = [(r, d) for r, d in parsed if d is not None]
            if parsed:
                r, d = min(parsed, key=lambda x: x[1])  # type: ignore[arg-type, return-value]
                reset_iso, reset_dt = r, d

        kind, wname, window_len = _window_kind_and_name(reset_dt, now)
        pct_remaining = round(frac * 100, 2)
        pct_used = round(100.0 - pct_remaining, 2)

        start_iso = None
        pct_time = None
        if reset_dt:
            start_dt = reset_dt - window_len
            start_iso = start_dt.isoformat()
            # Prefer canonical Z form of reset if original had Z
            end_iso = reset_dt.isoformat()
            frac_t = cycle_fraction(start_iso, end_iso)
            if frac_t is not None:
                pct_time = round(frac_t * 100.0, 2)

        models_note = ", ".join(sorted(labels[gkey])[:6])
        if len(labels[gkey]) > 6:
            models_note += f" +{len(labels[gkey]) - 6} more"

        windows.append(
            {
                "name": f"{gname} · {wname}",
                "kind": kind,
                "pct_used": pct_used,
                "pct_remaining": pct_remaining,
                "resets_at": reset_iso,
                "cycle_start": start_iso,
                "pct_time_elapsed": pct_time,
                "pace": pace_status(pct_used, pct_time),
                "unit": "shared_pool",
                "group": gkey,
                "remaining_fraction": frac,
                "member_count": len(items),
                "models_sample": models_note,
            }
        )
    return windows


def _from_override(ov: dict[str, Any], reason: str) -> dict[str, Any]:
    return provider_record(
        PROVIDER,
        plan=ov.get("plan") or "Antigravity",
        windows=list(ov.get("windows") or []),
        status="ok",
        source="manual",
        notes=ov.get("notes") or f"Manual override ({reason})",
        raw={"override": True, "reason": reason},
    )


def _fail(reason: str, status: str = "error", ov: dict | None = None) -> dict[str, Any]:
    ov = ov if ov is not None else (read_manual_overrides().get(PROVIDER) or {})
    if ov.get("windows") or ov.get("plan"):
        return _from_override(ov, reason)
    return provider_record(
        PROVIDER,
        plan="Antigravity",
        windows=[],
        status=status,
        source="antigravity-cockpit",
        notes=reason,
    )


def collect() -> dict[str, Any]:
    ov = (read_manual_overrides().get(PROVIDER) or {}).copy()
    if ov.get("force_manual"):
        return _from_override(ov, "force_manual")

    try:
        acc, email, source_tag, opaque = _resolve_creds()
    except Exception as e:
        return _fail(
            f"Antigravity auth not found ({e}). "
            "Install agy CLI and sign in (`agy` then follow prompts), "
            "or open Antigravity Cockpit extension and sign in, "
            "or set ANTIGRAVITY_CREDS.",
            status="login_required",
            ov=ov,
        )

    try:
        if source_tag == _COCKPIT:
            access = _ensure_access_token(acc, email, opaque)
        else:
            access = _ensure_keyring_access_token(acc)
    except Exception as e:
        return _fail(f"Token refresh failed: {e}", status="login_required", ov=ov)

    endpoint = (
        os.environ.get("ANTIGRAVITY_CLOUDCODE_URL") or _DEFAULT_ENDPOINT
    ).rstrip("/")
    try:
        res = _post_json(f"{endpoint}/v1internal:fetchAvailableModels", access, {})
    except Exception as e:
        return _fail(f"fetchAvailableModels failed: {e}", ov=ov)

    models = res.get("models") or {}
    wins = _group_windows(models)
    if not wins:
        return _fail(
            f"No group quotas in fetchAvailableModels ({len(models)} models). "
            "Account may lack Antigravity cascade access.",
            ov=ov,
        )

    samples = []
    for w in wins:
        if w.get("models_sample"):
            samples.append(f"{w.get('group')}: {w['models_sample']}")

    auth_note = (
        f"{email} · agy /usage groups via {endpoint.replace('https://', '')}. "
        "Models in a group share one pool (cost-weighted). "
    )
    if source_tag == _KEYRING:
        auth_note += f"[auth: agy CLI keyring] "
    if samples:
        auth_note += " | ".join(samples)

    return provider_record(
        PROVIDER,
        plan="Antigravity",
        windows=wins,
        status="ok",
        source="daily-cloudcode-pa",
        notes=auth_note,
        raw={
            "email": email,
            "endpoint": endpoint,
            "model_count": len(models),
            "ua": _UA,
            "auth_source": source_tag,
        },
    )


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, default=str))
