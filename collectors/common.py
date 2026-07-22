"""Shared helpers for subscription usage collectors."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Package root = directory containing server.py / collect_all.py
PKG_ROOT = Path(__file__).resolve().parent.parent

# Runtime data dir (override with SUBMON_DATA for custom installs)
DATA_DIR = Path(
    os.environ.get("SUBMON_DATA", str(PKG_ROOT / "data"))
).expanduser()
SNAPSHOT_PATH = DATA_DIR / "latest.json"
HISTORY_DIR = DATA_DIR / "history"

# Back-compat aliases
ROOT = PKG_ROOT
HOME = Path(
    os.environ.get(
        "HERMES_HOME",
        str(Path.home() / ".hermes"),
    )
).expanduser()


def _env_file_candidates() -> list[Path]:
    """Ordered list of .env files to scan for API keys / tokens."""
    out: list[Path] = []
    if os.environ.get("SUBMON_ENV"):
        out.append(Path(os.environ["SUBMON_ENV"]).expanduser())
    out.append(PKG_ROOT / ".env")
    hh = os.environ.get("HERMES_HOME")
    if hh:
        out.append(Path(hh).expanduser() / ".env")
    out.append(Path.home() / ".hermes" / ".env")
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _default_env_path() -> Path:
    for p in _env_file_candidates():
        if p.exists():
            return p
    return PKG_ROOT / ".env"


ENV_PATH = _default_env_path()


def resolve_oauth_client(kind: str) -> tuple[Optional[str], Optional[str], str]:
    """Resolve (client_id, client_secret, source) for installed-app OAuth clients.

    Never hardcodes secrets in the repo (GitHub push protection). Order:
      1. Process env / .env  (GEMINI_OAUTH_CLIENT_* or ANTIGRAVITY_OAUTH_CLIENT_*)
      2. data/oauth_clients.json
      3. Auto-discover from installed Gemini CLI / Antigravity Cockpit on disk

    These are *public installed-app* OAuth clients shipped inside vendor CLIs,
    not end-user passwords. Still kept out of git history.
    """
    kind = (kind or "").strip().lower()
    if kind in ("gemini", "code_assist", "google_gemini"):
        env_id = load_env_key("GEMINI_OAUTH_CLIENT_ID", "GOOGLE_GEMINI_OAUTH_CLIENT_ID")
        env_sec = load_env_key(
            "GEMINI_OAUTH_CLIENT_SECRET", "GOOGLE_GEMINI_OAUTH_CLIENT_SECRET"
        )
        file_key = "gemini"
        discover = _discover_gemini_oauth_client
    elif kind in ("antigravity", "agy", "cockpit"):
        env_id = load_env_key(
            "ANTIGRAVITY_OAUTH_CLIENT_ID", "AGY_OAUTH_CLIENT_ID"
        )
        env_sec = load_env_key(
            "ANTIGRAVITY_OAUTH_CLIENT_SECRET", "AGY_OAUTH_CLIENT_SECRET"
        )
        file_key = "antigravity"
        discover = _discover_antigravity_oauth_client
    else:
        return None, None, f"unknown oauth kind {kind!r}"

    if env_id and env_sec:
        return env_id, env_sec, "env"

    cfg_path = DATA_DIR / "oauth_clients.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            block = cfg.get(file_key) or {}
            cid = (block.get("client_id") or "").strip()
            csec = (block.get("client_secret") or "").strip()
            if cid and csec:
                return cid, csec, str(cfg_path)
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    try:
        cid, csec, src = discover()
        if cid and csec:
            return cid, csec, src
    except Exception:
        pass
    return None, None, "not found"


def _read_id_secret_from_text(text: str) -> tuple[Optional[str], Optional[str]]:
    cid = None
    csec = None
    m = re.search(
        r"(\d{6,}-[a-z0-9]+\.apps\.googleusercontent\.com)",
        text,
        re.I,
    )
    if m:
        cid = m.group(1)
    m = re.search(r"(GOCSPX-[A-Za-z0-9_-]+)", text)
    if m:
        csec = m.group(1)
    return cid, csec


def _discover_gemini_oauth_client() -> tuple[Optional[str], Optional[str], str]:
    """Pull installed-app client from @google/gemini-cli-core if present."""
    candidates: list[Path] = []
    # npm global under nvm / system
    home = Path.home()
    for base in (
        home / ".nvm/versions/node",
        Path("/usr/lib/node_modules"),
        Path("/usr/local/lib/node_modules"),
        home / ".npm-global/lib/node_modules",
        home / ".local/lib/node_modules",
    ):
        if not base.exists():
            continue
        for p in base.rglob("oauth2.js"):
            s = str(p)
            if "gemini-cli-core" in s and "code_assist" in s:
                candidates.append(p)
        for p in base.rglob("oauth2.mjs"):
            s = str(p)
            if "gemini-cli" in s:
                candidates.append(p)
    # Also search sibling of `gemini` binary
    import shutil

    gem = shutil.which("gemini")
    if gem:
        gp = Path(gem).resolve()
        for p in gp.parent.parent.rglob("oauth2.js"):
            if "gemini" in str(p).lower():
                candidates.append(p)

    seen: set[str] = set()
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        cid, csec = _read_id_secret_from_text(text)
        if cid and csec:
            return cid, csec, str(p)
    return None, None, "gemini-cli not found"


def _discover_antigravity_oauth_client() -> tuple[Optional[str], Optional[str], str]:
    """Pull cockpit OAuth client from installed Antigravity Cockpit extension."""
    roots = [
        Path.home() / ".antigravity/extensions",
        Path.home() / ".config/Antigravity/extensions",
        Path("/usr/share/antigravity"),
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("extension.js"):
            if "antigravity-cockpit" in str(p):
                candidates.append(p)
        for p in root.rglob("main.js"):
            if "antigravity" in str(p).lower():
                candidates.append(p)
    # Prefer highest version-looking cockpit path last-sort
    candidates = sorted(candidates, key=lambda p: str(p))
    for p in reversed(candidates):
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        # Cockpit uses a specific client; require both id+secret in same file
        cid, csec = _read_id_secret_from_text(text)
        if cid and csec and (
            "1071006060591" in cid or "cockpit" in str(p)
        ):
            # Prefer the known cockpit client id prefix when multiple present
            ids = re.findall(
                r"(\d{6,}-[a-z0-9]+\.apps\.googleusercontent\.com)", text, re.I
            )
            secs = re.findall(r"(GOCSPX-[A-Za-z0-9_-]+)", text)
            for i in ids:
                if i.startswith("1071006060591"):
                    cid = i
                    break
            if secs:
                csec = secs[0]
            if cid and csec:
                return cid, csec, str(p)
    return None, None, "antigravity-cockpit not found"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow()).isoformat()


def load_env_key(*names: str) -> Optional[str]:
    """Read first matching KEY from process env, then .env files."""
    for name in names:
        val = os.environ.get(name)
        if val and val.strip():
            return val.strip().strip('"').strip("'")
    for path in _env_file_candidates():
        if not path.exists():
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for name in names:
            m = re.search(rf"^{re.escape(name)}=(.*)$", text, re.M)
            if m:
                val = m.group(1).strip().strip('"').strip("'")
                if val:
                    return val
    return None


def pace_status(pct_used: Optional[float], pct_time: Optional[float]) -> str:
    """ahead | on_pace | behind | unknown relative to even burn."""
    if pct_used is None or pct_time is None or pct_time <= 0:
        return "unknown"
    ratio = pct_used / pct_time
    if ratio >= 1.25:
        return "ahead"  # burning faster than even pace (risk)
    if ratio <= 0.75:
        return "behind"  # underusing this pool
    return "on_pace"


def cycle_fraction(
    start_iso: Optional[str], end_iso: Optional[str], now: Optional[datetime] = None
) -> Optional[float]:
    if not start_iso or not end_iso:
        return None
    now = now or utcnow()
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if end <= start:
        return None
    frac = (now - start).total_seconds() / (end - start).total_seconds()
    return max(0.0, min(1.0, frac))


def provider_record(
    provider: str,
    *,
    plan: Optional[str] = None,
    windows: Optional[list[dict[str, Any]]] = None,
    status: str = "ok",
    notes: Optional[str] = None,
    source: str = "api",
    raw: Any = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "plan": plan,
        "status": status,  # ok | error | stub | login_required
        "source": source,
        "collected_at": iso(),
        "notes": notes,
        "windows": windows or [],
        # Kept in-memory only; stripped before write_snapshot
        "raw": raw,
    }


def _public_provider(p: dict[str, Any]) -> dict[str, Any]:
    """Drop bulky/sensitive raw API payloads before disk write."""
    out = dict(p)
    out.pop("raw", None)
    return out


def write_snapshot(providers: list[dict[str, Any]]) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    public = [_public_provider(p) for p in providers]
    snap = {
        "generated_at": iso(),
        "host": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "providers": {p["provider"]: p for p in public},
        "alerts": derive_alerts(public),
    }
    SNAPSHOT_PATH.write_text(json.dumps(snap, indent=2, default=str))
    hist = {
        "t": snap["generated_at"],
        "p": {
            name: {
                "plan": p.get("plan"),
                "status": p.get("status"),
                "windows": [
                    {
                        "name": w.get("name"),
                        "pct": w.get("pct_used"),
                        "pct_time": w.get("pct_time_elapsed"),
                        "reset": w.get("resets_at"),
                        "pace": w.get("pace"),
                    }
                    for w in (p.get("windows") or [])
                ],
            }
            for name, p in snap["providers"].items()
        },
        "alerts": snap["alerts"],
    }
    day = utcnow().strftime("%Y%m%d")
    hpath = HISTORY_DIR / f"{day}.jsonl"
    with hpath.open("a") as f:
        f.write(json.dumps(hist, default=str) + "\n")
    return SNAPSHOT_PATH


def derive_alerts(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Red-zone only — must match dashboard bar logic (pace + 10pp, or ≥80% if no clock).

    Yellow (ahead of even pace but under +10pp) does NOT alert.
    """
    PACE_WARN_PP = 10.0
    NO_CLOCK_RED = 80.0
    alerts = []
    for p in providers:
        if p.get("status") == "error":
            alerts.append(
                {
                    "level": "warn",
                    "provider": p["provider"],
                    "message": p.get("notes") or "collector error",
                }
            )
            continue
        if p.get("status") in ("stub", "login_required"):
            alerts.append(
                {
                    "level": "info",
                    "provider": p["provider"],
                    "message": p.get("notes") or "no live collector yet",
                }
            )
            continue
        for w in p.get("windows") or []:
            pct = w.get("pct_used")
            name = w.get("name") or "usage"
            if pct is None:
                continue
            try:
                pct_f = float(pct)
            except (TypeError, ValueError):
                continue
            pct_time = w.get("pct_time_elapsed")
            try:
                time_f = float(pct_time) if pct_time is not None else None
            except (TypeError, ValueError):
                time_f = None

            if time_f is not None:
                red_at = min(100.0, time_f + PACE_WARN_PP)
                if pct_f + 1e-9 < red_at:
                    continue
                alerts.append(
                    {
                        "level": "critical" if pct_f >= 90 else "warn",
                        "provider": p["provider"],
                        "message": (
                            f"{name} in red — {pct_f:.0f}% used "
                            f"(red ≥ {red_at:.0f}% = even-pace + {PACE_WARN_PP:.0f}pp)"
                        ),
                        "window": name,
                    }
                )
            else:
                if pct_f < NO_CLOCK_RED:
                    continue
                alerts.append(
                    {
                        "level": "critical" if pct_f >= 90 else "warn",
                        "provider": p["provider"],
                        "message": (
                            f"{name} in red — {pct_f:.0f}% used "
                            f"(no cycle clock; red ≥ {NO_CLOCK_RED:.0f}%)"
                        ),
                        "window": name,
                    }
                )
    return alerts


def read_snapshot() -> dict[str, Any]:
    if not SNAPSHOT_PATH.exists():
        return {"generated_at": None, "providers": {}, "alerts": []}
    return json.loads(SNAPSHOT_PATH.read_text())


def read_manual_overrides() -> dict[str, Any]:
    path = DATA_DIR / "manual_overrides.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
