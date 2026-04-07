"""
One-time Garmin DI token bootstrap using your real browser.

Use this when the normal programmatic login is blocked by Cloudflare (429).
Opens the Garmin SSO page in your browser, you log in normally, then paste
the service ticket back. The script exchanges it for DI OAuth tokens and
writes garmin_tokens.json.

Requirements: pip install requests

Standalone usage (no config.toml needed):

    python3 src/bootstrap_auth.py -o ~/data/garminnostra/tokens/betty
    python3 src/bootstrap_auth.py -o . --browser firefox

Multi-user via config.toml:

    python3 src/bootstrap_auth.py --config config.toml --token-dir ~/data/garminnostra/tokens
    python3 src/bootstrap_auth.py --config config.toml --token-dir ~/data/garminnostra/tokens --user betty
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

import requests


# ---------------------------------------------------------------------------
# Constants mirrored from garminconnect/client.py — must stay in sync
# ---------------------------------------------------------------------------

PORTAL_SSO_CLIENT_ID   = "GarminConnect"
PORTAL_SSO_SERVICE_URL = "https://connect.garmin.com/app"
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

BEFOREUNLOAD_SNIPPET = (
    "window.addEventListener('beforeunload', function(e) "
    "{ e.preventDefault(); e.returnValue = ''; });"
)


# ---------------------------------------------------------------------------
# DI token exchange (not Cloudflare-protected — plain requests is fine)
# ---------------------------------------------------------------------------

def _basic_auth(client_id: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:".encode()).decode()


def _exchange_ticket(ticket: str) -> tuple[str, str | None, str]:
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
# Parse ticket from user input (accepts multiple formats)
# ---------------------------------------------------------------------------

def _parse_ticket(raw: str) -> str | None:
    """Extract a service ticket from raw user input."""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("ST-"):
        return raw
    if "ticket=" in raw:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(raw)
        params = parse_qs(parsed.query)
        tickets = params.get("ticket", [])
        if tickets:
            return tickets[0]
    if "serviceTicketId" in raw:
        match = re.search(r'"serviceTicketId"\s*:\s*"(ST-[^"]+)"', raw)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Save tokens
# ---------------------------------------------------------------------------

def _save_tokens(access_token: str, refresh_token: str | None,
                 client_id: str, token_dir: Path) -> None:
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
# Interactive bootstrap for one user
# ---------------------------------------------------------------------------

def _bootstrap_user(label: str, token_dir: Path,
                    browser_name: str | None = None) -> None:
    print(f"\n--- Bootstrapping {label} ---")

    sso_url = (
        f"{SIGNIN_URL}?"
        + urlencode({
            "clientId": PORTAL_SSO_CLIENT_ID,
            "service": PORTAL_SSO_SERVICE_URL,
        })
    )

    print(
        f"\nBEFORE you log in:\n"
        f"  1. Press F12 to open DevTools → Console tab\n"
        f"  2. Type 'allow pasting' and press Enter (Firefox only, ignore the error)\n"
        f"  3. Paste the following and press Enter:\n"
        f"\n"
        f"     {BEFOREUNLOAD_SNIPPET}\n"
        f"\n"
        f"  4. Log in normally (complete CAPTCHA/MFA if needed)\n"
        f"  5. A 'Leave this page?' dialog appears — click STAY ON PAGE\n"
        f"  6. Switch to the Network tab, filter by 'login'\n"
        f"  7. Click the /portal/api/login request → Response tab\n"
        f"  8. Copy the serviceTicketId value (ST-xxxxx)\n"
        f"  9. Paste it below\n"
    )

    print(f"Opening: {sso_url}")
    if browser_name:
        try:
            webbrowser.get(browser_name).open(sso_url)
        except webbrowser.Error:
            print(f"WARNING: Could not open '{browser_name}'. Falling back to default browser.")
            webbrowser.open(sso_url)
    else:
        webbrowser.open(sso_url)

    ticket = None
    while not ticket:
        raw = input("\nPaste the serviceTicketId value (ST-...): ")
        ticket = _parse_ticket(raw)
        if not ticket:
            print("Could not find a service ticket (ST-...) in that input. Try again.")

    print("Service ticket captured. Exchanging for DI tokens …")
    access_token, refresh_token, client_id = _exchange_ticket(ticket)
    _save_tokens(access_token, refresh_token, client_id, token_dir)


# ---------------------------------------------------------------------------
# Config-based multi-user mode
# ---------------------------------------------------------------------------

def _load_config(path: str) -> dict:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-reattr]

    def resolve(obj):
        if isinstance(obj, str) and obj.startswith("env:"):
            var = obj[4:]
            val = os.environ.get(var)
            if val is None:
                raise ValueError(f"env:{var} is not set.")
            return val
        if isinstance(obj, dict):
            return {k: resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [resolve(i) for i in obj]
        return obj

    with open(path, "rb") as f:
        return resolve(tomllib.load(f))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Bootstrap Garmin DI OAuth tokens using your real browser.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Standalone (one user, no config.toml):\n"
            "  python3 src/bootstrap_auth.py -o ~/data/garminnostra/tokens/betty\n"
            "\n"
            "Multi-user via config.toml:\n"
            "  python3 src/bootstrap_auth.py --config config.toml --token-dir ~/data/garminnostra/tokens\n"
            "  python3 src/bootstrap_auth.py --config config.toml --token-dir ~/data/garminnostra/tokens --user betty\n"
        ),
    )
    parser.add_argument(
        "-o", "--output", metavar="DIR",
        help="Output directory for garmin_tokens.json (standalone mode, one user). "
             "Default: current directory.",
    )
    parser.add_argument(
        "--config", metavar="FILE",
        help="Path to config.toml (multi-user mode). Reads users and token_dir from config.",
    )
    parser.add_argument(
        "--token-dir", metavar="DIR",
        help="Override token_dir from config (e.g. ~/data/garminnostra/tokens).",
    )
    parser.add_argument(
        "--user", metavar="NAME",
        help="Bootstrap only this user (matched against 'name' in config). "
             "Without this flag, all Garmin users in config are bootstrapped.",
    )
    parser.add_argument(
        "--browser", metavar="NAME",
        help="Browser to open (e.g. 'firefox'). Default: system default.",
    )
    args = parser.parse_args()

    # Standalone mode: -o / --output
    if args.output:
        output_dir = Path(args.output).expanduser()
        label = output_dir.name or "Garmin user"
        _bootstrap_user(label, output_dir, browser_name=args.browser)
        return

    # Multi-user mode: --config
    if args.config:
        if not Path(args.config).exists():
            print(f"ERROR: Config file not found: {args.config}")
            sys.exit(1)

        cfg = _load_config(args.config)
        token_dir = Path(
            args.token_dir or cfg.get("storage", {}).get("token_dir", "/data/tokens")
        ).expanduser()
        users = cfg.get("users", [])

        garmin_users = [
            u for u in users
            if u.get("garmin_username") and u.get("garmin_password")
        ]

        if args.user:
            garmin_users = [
                u for u in garmin_users
                if u.get("name", "").lower() == args.user.lower()
                or u.get("garmin_username", "").lower() == args.user.lower()
            ]
            if not garmin_users:
                print(f"ERROR: No Garmin user matching '{args.user}' found in config.")
                sys.exit(1)

        if not garmin_users:
            print("No users with garmin_username/garmin_password found in config.")
            sys.exit(0)

        print(f"Found {len(garmin_users)} Garmin user(s) to bootstrap.")

        errors = []
        for user in garmin_users:
            name = user.get("name", user["garmin_username"])
            user_token_dir = token_dir / name
            try:
                _bootstrap_user(name, user_token_dir, browser_name=args.browser)
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
        return

    # No mode specified — default to standalone with current directory
    if not args.output and not args.config:
        output_dir = Path(".").resolve()
        print(f"No --config or -o specified. Token will be saved to {output_dir}/garmin_tokens.json")
        _bootstrap_user("Garmin user", output_dir, browser_name=args.browser)


if __name__ == "__main__":
    main()
