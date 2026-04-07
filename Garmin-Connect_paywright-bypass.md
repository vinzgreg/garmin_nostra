# Garmin Connect — Playwright Bootstrap (Cloudflare bypass)

Use this when the normal programmatic login is blocked by Cloudflare (429 errors).
The script opens a real Chromium browser, auto-fills credentials, and lets you handle
any CAPTCHA or MFA manually. It then exchanges the session ticket for DI OAuth tokens
and writes `garmin_tokens.json` so the sync service can authenticate without hitting
SSO again.

## Prerequisites

Run on the **host machine** (not inside the Docker container — it needs a display).

## Steps

### 1. Stop the container

```bash
docker compose down
```

### 2. Create a temporary venv and install dependencies

```bash
python3 -m venv /tmp/playwright-bootstrap
source /tmp/playwright-bootstrap/bin/activate
pip install playwright requests
playwright install chromium
```

### 3. Run the bootstrap script

```bash
python3 src/bootstrap_auth.py config.toml --token-dir ~/data/garminnostra/tokens
```

A browser window opens for each Garmin user in your config. Credentials are
auto-filled. Complete any CAPTCHA or MFA that appears — the script waits up to
5 minutes per user.

### 4. Restart the container

```bash
docker compose up -d
```

The logs should now show `Authenticated via saved tokens` instead of a credential
login attempt.

### 5. Clean up the venv

```bash
deactivate
rm -rf /tmp/playwright-bootstrap
```

## Notes

- Tokens are saved to `~/data/garminnostra/tokens/<username>/garmin_tokens.json`
  with permissions `600` (owner read/write only).
- Once tokens exist, the sync service never touches Garmin SSO — it refreshes
  tokens transparently until the refresh token expires (up to ~1 year).
- If you add a new user to `config.toml` later, re-run the bootstrap script.
  It will only open a browser for users that don't already have a valid token file.
