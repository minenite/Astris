"""Mint a Horizon-style MSA session for Astris (localhost only).

Sources, in order:
  1. PrismLauncher accounts.json (Flatpak + native paths)
  2. ~/.minecraft/minenite-accounts.json (login.live.com / Xbox app id)
  3. Optional device-code sign-in (same public client Minenite uses)

Never logs tokens. Used by dev_server /auth/*.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Public Xbox / Minecraft launcher client id (same as Minenite / many third-party tools).
XBOX_CLIENT_ID = "00000000402b5328"
PRISM_CLIENT_ID = "c36a9fb6-4f2a-41ff-90bd-ae7cc92031eb"
LIVE_TOKEN = "https://login.live.com/oauth20_token.srf"
LIVE_CONNECT = "https://login.live.com/oauth20_connect.srf"
LIVE_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
AZURE_TOKEN = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
AZURE_SCOPE = "XboxLive.signin offline_access"
DISCOVERY = "https://discovery.minecraftservices.com/minecraft/client"

HOME = Path.home()
PRISM_PATHS = [
    HOME / ".var/app/org.prismlauncher.PrismLauncher/data/PrismLauncher/accounts.json",
    HOME / ".local/share/PrismLauncher/accounts.json",
]
MINENITE_PATH = HOME / ".minecraft" / "minenite-accounts.json"

_lock = threading.Lock()
_cached: dict | None = None
_cached_at = 0.0
_device: dict | None = None


def _http_form(url: str, fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _http_json(url: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    hdrs = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method="GET" if data is None else "POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _http_json_status(url: str, payload: dict, headers: dict | None = None) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def _exchange_microsoft(ms: dict) -> dict:
    ms_token = ms.get("access_token") or ""
    refresh = ms.get("refresh_token") or ""
    if not ms_token:
        raise RuntimeError("Microsoft did not return an access token")
    prefix = "d=" if ms_token.startswith("ey") else "t="
    xbl = _http_json(
        "https://user.auth.xboxlive.com/user/authenticate",
        {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": prefix + ms_token,
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        },
    )
    uhs = (((xbl.get("DisplayClaims") or {}).get("xui") or [{}])[0]).get("uhs") or ""
    xsts = _http_json(
        "https://xsts.auth.xboxlive.com/xsts/authorize",
        {
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT",
        },
    )
    if "XErr" in xsts:
        raise RuntimeError(f"Xbox Live refused this account ({xsts['XErr']})")
    xsts_uhs = uhs or ((((xsts.get("DisplayClaims") or {}).get("xui") or [{}])[0]).get("uhs") or "")
    mc = _http_json(
        "https://api.minecraftservices.com/authentication/login_with_xbox",
        {"identityToken": f"XBL3.0 x={xsts_uhs};{xsts['Token']}"},
    )
    mc_token = mc.get("access_token") or ""
    if not mc_token:
        raise RuntimeError("Minecraft services did not return a session")
    profile = _http_json(
        "https://api.minecraftservices.com/minecraft/profile",
        headers={"Authorization": f"Bearer {mc_token}"},
    )
    name = profile.get("name") or ""
    uuid = profile.get("id") or ""
    if not name or not uuid:
        raise RuntimeError("This Microsoft account does not own Minecraft Java")
    return {
        "name": name,
        "uuid": uuid,
        "access_token": mc_token,
        "user_type": "msa",
        "xuid": profile.get("id"),  # profile id is mc uuid; xuid optional
        "refresh_token": refresh,
        "source": "minted",
    }


def _try_prism() -> dict | None:
    path = next((p for p in PRISM_PATHS if p.is_file()), None)
    if not path:
        return None
    data = json.loads(path.read_text())
    accounts = data.get("accounts") or []
    if not accounts:
        return None
    acc = next((a for a in accounts if a.get("active")), accounts[0])
    client_id = acc.get("msa-client-id") or PRISM_CLIENT_ID
    refresh = ((acc.get("msa") or {}).get("refresh_token")) or ""
    if not refresh:
        return None
    try:
        ms = _http_form(
            AZURE_TOKEN,
            {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "scope": AZURE_SCOPE,
            },
        )
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Prism refresh failed: {e.read().decode(errors='replace')[:180]}") from e
    session = _exchange_microsoft(ms)
    session["source"] = f"prism:{path}"
    # Persist rotated refresh back into Prism so the next mint works.
    if session.get("refresh_token"):
        try:
            acc.setdefault("msa", {})["refresh_token"] = session["refresh_token"]
            path.write_text(json.dumps(data, indent=2) + "\n")
        except OSError:
            pass
    return session


def _try_minenite() -> dict | None:
    if not MINENITE_PATH.is_file():
        return None
    data = json.loads(MINENITE_PATH.read_text())
    accounts = data.get("accounts") or []
    if not accounts:
        return None
    acc = accounts[0]
    refresh = acc.get("refreshToken") or ""
    if not refresh:
        return None
    try:
        if refresh.startswith("M."):
            ms = _http_form(
                LIVE_TOKEN,
                {
                    "grant_type": "refresh_token",
                    "client_id": XBOX_CLIENT_ID,
                    "refresh_token": refresh,
                    "scope": LIVE_SCOPE,
                },
            )
        else:
            ms = _http_form(
                AZURE_TOKEN,
                {
                    "grant_type": "refresh_token",
                    "client_id": XBOX_CLIENT_ID,
                    "refresh_token": refresh,
                    "scope": AZURE_SCOPE,
                },
            )
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Minenite refresh failed: {e.read().decode(errors='replace')[:180]}") from e
    session = _exchange_microsoft(ms)
    session["source"] = "minenite-accounts.json"
    if session.get("refresh_token"):
        try:
            acc["refreshToken"] = session["refresh_token"]
            acc["mcToken"] = session["access_token"]
            acc["mcExpiresAt"] = int(time.time() * 1000) + 20 * 3600 * 1000
            MINENITE_PATH.write_text(json.dumps(data, indent=2) + "\n")
        except OSError:
            pass
    return session


def device_start() -> dict:
    global _device
    js = _http_form(
        LIVE_CONNECT,
        {
            "client_id": XBOX_CLIENT_ID,
            "scope": LIVE_SCOPE,
            "response_type": "device_code",
        },
    )
    if js.get("error"):
        raise RuntimeError(js.get("error_description") or js["error"])
    with _lock:
        _device = {
            "device_code": js["device_code"],
            "interval": max(3, int(js.get("interval") or 5)),
            "expires_at": time.time() + int(js.get("expires_in") or 900),
        }
    return {
        "user_code": js.get("user_code"),
        "verify_url": js.get("verification_uri") or js.get("verification_url") or "https://www.microsoft.com/link",
        "interval": _device["interval"],
        "expires_in": int(js.get("expires_in") or 900),
    }


def device_poll() -> dict:
    global _cached, _cached_at, _device
    with _lock:
        state = dict(_device) if _device else None
    if not state:
        return {"status": "none"}
    if time.time() > state["expires_at"]:
        with _lock:
            _device = None
        return {"status": "expired"}
    try:
        js = _http_form(
            LIVE_TOKEN,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": XBOX_CLIENT_ID,
                "device_code": state["device_code"],
            },
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body)
        except json.JSONDecodeError:
            return {"status": "error", "message": body[:200]}
        error = err.get("error") or ""
        if error in ("authorization_pending", "slow_down"):
            return {"status": "pending"}
        return {"status": "error", "message": err.get("error_description") or error}
    if js.get("error"):
        error = js.get("error") or ""
        if error in ("authorization_pending", "slow_down"):
            return {"status": "pending"}
        return {"status": "error", "message": js.get("error_description") or error}
    session = _exchange_microsoft(js)
    session["source"] = "device-code"
    # Keep a copy for Minenite/Astris next time.
    try:
        MINENITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        uid = session["uuid"]
        if "-" not in uid and len(uid) == 32:
            uid = f"{uid[:8]}-{uid[8:12]}-{uid[12:16]}-{uid[16:20]}-{uid[20:]}"
        payload = {
            "accounts": [
                {
                    "uuid": uid,
                    "name": session["name"],
                    "refreshToken": session.get("refresh_token") or "",
                    "mcToken": session["access_token"],
                    "mcExpiresAt": int(time.time() * 1000) + 20 * 3600 * 1000,
                }
            ],
            "active": "0",
        }
        MINENITE_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    except OSError:
        pass
    public = _store(session)
    with _lock:
        _device = None
    return {"status": "ready", "session": public}


def _store(session: dict) -> dict:
    global _cached, _cached_at
    with _lock:
        _cached = session
        _cached_at = time.time()
    return {
        "name": session["name"],
        "uuid": session["uuid"],
        "access_token": session["access_token"],
        "user_type": "msa",
        "xuid": session.get("xuid"),
        "source": session.get("source"),
    }


def mint_session(force: bool = False) -> dict:
    """Return Horizon session JSON fields, or raise with a player-facing message."""
    global _cached, _cached_at
    with _lock:
        if not force and _cached and time.time() - _cached_at < 30 * 60:
            s = _cached
            return {
                "name": s["name"],
                "uuid": s["uuid"],
                "access_token": s["access_token"],
                "user_type": "msa",
                "xuid": s.get("xuid"),
                "source": s.get("source"),
            }

    errors: list[str] = []
    for label, fn in (("prism", _try_prism), ("minenite", _try_minenite)):
        try:
            session = fn()
            if session:
                return _store(session)
        except Exception as e:  # noqa: BLE001 - surface to the page
            errors.append(f"{label}: {e}")

    detail = "; ".join(errors) if errors else "no saved Microsoft account on this computer"
    raise RuntimeError(
        "Sign in required. Open Prism and sign in, or use Astris device sign-in. "
        + detail
    )


def join_forward(body: dict) -> tuple[int, str]:
    """Proxy session/join so the browser WASM does not need raw HTTPS sockets."""
    doc = _http_json(DISCOVERY)
    url = (
        ((doc.get("discovery") or {}).get("session") or {})
        .get("endpoints", {})
        .get("join", {})
        .get("uri")
    )
    if not url:
        return 503, '{"error":"session join endpoint missing"}'
    return _http_json_status(url, body)
