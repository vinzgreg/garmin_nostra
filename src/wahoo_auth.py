#!/usr/bin/env python3
"""One-time Wahoo OAuth 2.0 bootstrap — obtains refresh token for config.toml.

Usage:
    python3 wahoo_auth.py <client_id> <client_secret> [redirect_uri]

Steps:
  1. Opens (or prints) the Wahoo authorization URL in your browser.
  2. After you grant access, Wahoo redirects to the redirect_uri with a ?code=…
  3. Paste that code here when prompted.
  4. The script exchanges it for tokens and prints the refresh_token.

Store the printed refresh_token in config.toml:
    wahoo_refresh_token = "env:ALICE_WAHOO_REFRESH_TOKEN"
  or directly (not recommended):
    wahoo_refresh_token = "<the_token>"
"""

from __future__ import annotations

import sys
import urllib.parse

import requests

_API_BASE = "https://api.wahooligan.com"
_AUTH_URL = f"{_API_BASE}/oauth/authorize"
_TOKEN_URL = f"{_API_BASE}/oauth/token"
_DEFAULT_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"
_SCOPES = "user_read workouts_read offline_data"


def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <client_id> <client_secret> [redirect_uri]")
        sys.exit(1)

    client_id = sys.argv[1]
    client_secret = sys.argv[2]
    redirect_uri = sys.argv[3] if len(sys.argv) > 3 else _DEFAULT_REDIRECT

    # Step 1: Build and display the authorization URL
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": _SCOPES,
        "response_type": "code",
    })
    auth_url = f"{_AUTH_URL}?{params}"

    print()
    print("Open this URL in your browser and authorize the app:")
    print()
    print(f"  {auth_url}")
    print()

    # Try to open in browser (non-fatal if headless)
    try:
        import webbrowser
        webbrowser.open(auth_url)
        print("(Browser opened automatically.)")
    except Exception:
        pass

    # Step 2: Get the authorization code from the user
    print()
    code = input("Paste the authorization code here: ").strip()
    if not code:
        print("No code provided. Aborting.")
        sys.exit(1)

    # Step 3: Exchange code for tokens
    print()
    print("Exchanging code for tokens …")
    resp = requests.post(
        _TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"Token exchange failed (HTTP {resp.status_code}):")
        print(resp.text)
        sys.exit(1)

    token_data = resp.json()
    refresh_token = token_data.get("refresh_token")
    access_token = token_data.get("access_token")
    expires_in = token_data.get("expires_in")

    if not refresh_token:
        print("Warning: no refresh_token in response. Full response:")
        print(token_data)
        sys.exit(1)

    print()
    print("Success! Store this refresh token in your config.toml")
    print("(per-user wahoo_refresh_token) or as an environment variable:")
    print()
    print(f"  refresh_token = {refresh_token}")
    print()
    print(f"  access_token  = {access_token}  (expires in {expires_in}s, auto-refreshed)")
    print()
    print("Example config.toml entry:")
    print('  wahoo_refresh_token = "env:ALICE_WAHOO_REFRESH_TOKEN"')
    print()


if __name__ == "__main__":
    main()
