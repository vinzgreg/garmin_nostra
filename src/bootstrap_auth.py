"""
One-time Garmin DI token bootstrap using your real browser.

Use this when the normal programmatic login is blocked by Cloudflare (429).
The script starts a tiny local HTTP server, opens Garmin Connect in your
default browser, and intercepts the SSO service ticket from the redirect.
It then exchanges the ticket for DI OAuth tokens and writes garmin_tokens.json
so the sync service can authenticate without ever hitting SSO again.

Requirements (install once on the host — NOT in the Docker container):

    pip install requests

Usage:

    python3 src/bootstrap_auth.py [path/to/config.toml] [--token-dir DIR]

If no path is given the script looks for CONFIG_FILE env var, then
config.toml in the current working directory.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-reattr]
    except ImportError:
        print("ERROR: tomllib / tomli not available. Install with: pip install tomli")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Constants mirrored from garminconnect/client.py — must stay in sync
# ---------------------------------------------------------------------------

PORTAL_SSO_CLIENT_ID   = "GarminConnect"
SIGNIN_URL             = "https://sso.garmin.com/portal/sso/en-US/sign-in"
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

# The local redirect server. Garmin SSO redirects here with the ticket.
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"


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


def _exchange_ticket(ticket: str, service_url: str) -> tuple[str, str | None, str]:
    """Exchange a CAS service ticket for DI OAuth tokens."""
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
                    "service_url": service_url,
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
# Local HTTP server to capture the SSO redirect
# ---------------------------------------------------------------------------

class _TicketHandler(BaseHTTPRequestHandler):
    """Handles the redirect from Garmin SSO, extracts the service ticket."""

    ticket: str | None = None
    error: str | None = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        ticket = params.get("ticket", [None])[0]

        if ticket:
            _TicketHandler.ticket = ticket
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Login successful!</h2>"
                b"<p>You can close this tab. The bootstrap script is exchanging "
                b"tokens&hellip;</p></body></html>"
            )
        else:
            _TicketHandler.error = f"No ticket in redirect: {self.path}"
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Error</h2>"
                b"<p>No service ticket found in the redirect URL.</p></body></html>"
            )

    def log_message(self, format, *args):
        pass  # suppress request logging


# ---------------------------------------------------------------------------
# Per-user browser bootstrap
# ---------------------------------------------------------------------------

def _bootstrap_user(username: str, token_dir: Path) -> None:
    print(f"\n--- Bootstrapping {username} ---")

    # Build the SSO URL that redirects back to our local server after login.
    # The service URL must be one that Garmin's SSO accepts.
    # We use the standard connect.garmin.com/app as the service URL since that's
    # what the official login flow uses. After SSO validates the ticket, the
    # browser is redirected to this URL with ?ticket=ST-xxxxx appended.
    service_url = "https://connect.garmin.com/app"
    sso_url = (
        f"{SIGNIN_URL}?"
        + urlencode({
            "clientId": PORTAL_SSO_CLIENT_ID,
            "service": service_url,
        })
    )

    _TicketHandler.ticket = None
    _TicketHandler.error = None

    print(
        f"\nPlease log in as {username} in the browser window that opens.\n"
        f"After login, Garmin will redirect to connect.garmin.com.\n"
        f"Copy the FULL URL from the browser address bar after the redirect\n"
        f"and paste it here (it will contain ?ticket=ST-xxxxx).\n"
    )
    print(f"Opening: {sso_url}")
    webbrowser.open(sso_url)

    # Wait for the user to paste the redirect URL
    ticket = None
    while not ticket:
        raw = input("\nPaste the redirect URL (or just the ticket value ST-...): ").strip()
        if not raw:
            continue
        if raw.startswith("ST-"):
            ticket = raw
        else:
            parsed = urlparse(raw)
            params = parse_qs(parsed.query)
            tickets = params.get("ticket", [])
            if tickets:
                ticket = tickets[0]
            else:
                print("Could not find ?ticket= in that URL. Try again.")

    print(f"Service ticket captured. Exchanging for DI tokens …")

    access_token, refresh_token, client_id = _exchange_ticket(ticket, service_url)

    token_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        token_dir.chmod(0o700)
    token_file = token_dir / "garmin_tokens.json"
    token_file.write_text(
        json.dumps({
            "di_token": access_token,
            "di_refresh_token": refresh_token,
            "di_client_id": client_id,
        })
    )
    if sys.platform != "win32":
        token_file.chmod(0o600)
    print(f"Tokens saved to {token_file}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "config", nargs="?",
        default=os.environ.get("CONFIG_FILE", "config.toml"),
        help="Path to config.toml (default: CONFIG_FILE env var or config.toml)",
    )
    parser.add_argument(
        "--token-dir", metavar="DIR",
        help="Override token directory from config (useful when running on host "
             "where Docker paths differ, e.g. ~/data/garminnostra/tokens)",
    )
    args = parser.parse_args()
    config_path = args.config

    if not Path(config_path).exists():
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)

    cfg = _load_config(config_path)
    token_dir = Path(
        args.token_dir or cfg.get("storage", {}).get("token_dir", "/data/tokens")
    ).expanduser()
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
