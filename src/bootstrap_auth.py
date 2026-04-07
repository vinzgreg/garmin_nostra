"""
One-time Garmin DI token bootstrap using Playwright browser automation.

Use this when the normal programmatic login is blocked by Cloudflare (429).
A real Chromium browser window opens for each Garmin user, credentials are
auto-filled, and you can handle any CAPTCHA or MFA that appears. Once login
succeeds the script exchanges the SSO service ticket for DI OAuth tokens and
writes garmin_tokens.json to the configured token directory.  The main sync
service then picks those up on the next run without ever hitting SSO again.

Requirements (install once on the host — NOT in the Docker container):

    pip install playwright requests
    playwright install chromium

Usage:

    python3 src/bootstrap_auth.py [path/to/config.toml]

If no path is given the script looks for CONFIG_FILE env var, then
config.toml in the current working directory.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-reattr]
    except ImportError:
        print("ERROR: tomllib / tomli not available. Install with: pip install tomli")
        sys.exit(1)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants mirrored from garminconnect/client.py — must stay in sync
# ---------------------------------------------------------------------------

PORTAL_SSO_CLIENT_ID   = "GarminConnect"
PORTAL_SSO_SERVICE_URL = "https://connect.garmin.com/app"
SIGNIN_URL             = "https://sso.garmin.com/portal/sso/en-US/sign-in"
LOGIN_API_URL          = "https://sso.garmin.com/portal/api/login"
DI_TOKEN_URL           = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
DI_GRANT_TYPE          = (
    "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket"
)
DI_CLIENT_IDS = (
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI",
)
NATIVE_USER_AGENT = (
    "com.garmin.android.apps.connectmobile/5.23; ; Google/sdk_gphone64_arm64/google; "
    "Android/33; Dalvik/2.1.0"
)


# ---------------------------------------------------------------------------
# Config helpers (minimal copies from sync.py — no side effects)
# ---------------------------------------------------------------------------

def _resolve_env_vars(obj):
    if isinstance(obj, str) and obj.startswith("env:"):
        var_name = obj[4:]
        value = os.environ.get(var_name)
        if value is None:
            raise ValueError(
                f"Config references env:{var_name} but the variable is not set."
            )
        return value
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(item) for item in obj]
    return obj


def _load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    return _resolve_env_vars(cfg)


# ---------------------------------------------------------------------------
# DI token exchange (not Cloudflare-protected — plain requests is fine)
# ---------------------------------------------------------------------------

def _basic_auth(client_id: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:".encode()).decode()


def _exchange_ticket(ticket: str) -> tuple[str, str | None, str]:
    """Exchange a CAS service ticket for DI OAuth tokens.

    Tries each known client ID in order, returns (access_token, refresh_token,
    client_id) on the first success.
    """
    last_err = None
    for client_id in DI_CLIENT_IDS:
        try:
            r = requests.post(
                DI_TOKEN_URL,
                headers={
                    "Authorization": _basic_auth(client_id),
                    "User-Agent": NATIVE_USER_AGENT,
                    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cache-Control": "no-cache",
                },
                data={
                    "client_id": client_id,
                    "service_ticket": ticket,
                    "grant_type": DI_GRANT_TYPE,
                    "service_url": PORTAL_SSO_SERVICE_URL,
                },
                timeout=30,
            )
            if r.status_code == 429:
                raise RuntimeError(
                    "DI token endpoint rate-limited (429). "
                    "Wait a few minutes and try again."
                )
            if not r.ok:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                continue
            data = r.json()
            access_token = data["access_token"]
            refresh_token = data.get("refresh_token")
            return access_token, refresh_token, client_id
        except RuntimeError:
            raise
        except Exception as e:
            last_err = str(e)
            continue
    raise RuntimeError(
        f"DI token exchange failed for all client IDs. Last error: {last_err}"
    )


# ---------------------------------------------------------------------------
# Per-user browser bootstrap
# ---------------------------------------------------------------------------

def _bootstrap_user(username: str, password: str, token_dir: Path) -> None:
    print(f"\n--- Bootstrapping {username} ---")

    ticket_holder: dict[str, str] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        def on_response(response):
            """Capture serviceTicketId from any SSO login API response."""
            if ticket_holder.get("ticket"):
                return
            if LOGIN_API_URL not in response.url:
                return
            try:
                data = response.json()
                ticket = (
                    data.get("serviceTicketId")
                    # MFA completion responses nest the ticket here too
                    or data.get("data", {}).get("serviceTicketId")
                )
                if ticket:
                    ticket_holder["ticket"] = ticket
            except Exception:
                pass

        page.on("response", on_response)

        signin_full_url = (
            f"{SIGNIN_URL}"
            f"?clientId={PORTAL_SSO_CLIENT_ID}"
            f"&service={PORTAL_SSO_SERVICE_URL}"
        )
        print(f"Opening browser: {signin_full_url}")
        page.goto(signin_full_url)

        # Auto-fill credentials — try common selector variants
        try:
            page.wait_for_selector(
                "input[name='username'], input[type='email']", timeout=10_000
            )
            page.fill("input[name='username'], input[type='email']", username)
            page.fill("input[name='password'], input[type='password']", password)
            page.click("button[type='submit']")
            print("Credentials filled. Complete any CAPTCHA or MFA in the browser window …")
        except Exception as e:
            print(f"Could not auto-fill form ({e}). Please log in manually in the browser.")

        # Wait up to 5 minutes for the service ticket
        deadline = time.monotonic() + 300
        while not ticket_holder.get("ticket"):
            if time.monotonic() > deadline:
                browser.close()
                raise TimeoutError(
                    f"Timed out waiting for service ticket for {username}. "
                    "Make sure the login completed successfully."
                )
            time.sleep(0.5)

        browser.close()

    ticket = ticket_holder["ticket"]
    print(f"Service ticket captured. Exchanging for DI tokens …")

    access_token, refresh_token, client_id = _exchange_ticket(ticket)

    token_dir.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "chmod"):
        token_dir.chmod(0o700)
    token_file = token_dir / "garmin_tokens.json"
    token_file.write_text(
        json.dumps({
            "di_token": access_token,
            "di_refresh_token": refresh_token,
            "di_client_id": client_id,
        })
    )
    if hasattr(os, "chmod"):
        token_file.chmod(0o600)
    print(f"Tokens saved to {token_file}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("CONFIG_FILE", "config.toml")
    )

    if not Path(config_path).exists():
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)

    cfg = _load_config(config_path)
    token_dir = Path(cfg.get("storage", {}).get("token_dir", "/data/tokens"))
    users = cfg.get("users", [])

    garmin_users = [
        u for u in users
        if u.get("garmin_username") and u.get("garmin_password")
    ]

    if not garmin_users:
        print("No users with garmin_username/garmin_password found in config.")
        sys.exit(0)

    print(f"Found {len(garmin_users)} Garmin user(s) to bootstrap.")

    errors = []
    for user in garmin_users:
        name = user.get("name", user["garmin_username"])
        user_token_dir = token_dir / name
        try:
            _bootstrap_user(
                username=user["garmin_username"],
                password=user["garmin_password"],
                token_dir=user_token_dir,
            )
        except Exception as e:
            print(f"ERROR bootstrapping {name}: {e}")
            errors.append(name)

    print()
    if errors:
        print(f"Bootstrap failed for: {', '.join(errors)}")
        sys.exit(1)
    else:
        print("All users bootstrapped successfully.")
        print("Restart the garmin-nostra container to use the new tokens.")


if __name__ == "__main__":
    main()
