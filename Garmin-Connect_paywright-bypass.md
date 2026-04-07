# Garmin Connect — Browser Bootstrap (Cloudflare bypass)

Use this when the normal programmatic login is blocked by Cloudflare (429 errors).
The script opens Garmin's login page in your real browser (Firefox, Chrome, etc.),
you log in normally, then paste the redirect URL back into the script. It exchanges
the service ticket for DI OAuth tokens and writes `garmin_tokens.json` so the sync
service can authenticate without hitting SSO again.

## Prerequisites

Run on the **host machine** (not inside the Docker container).

Only needs `requests` (no Playwright, no extra browsers).

## Steps

### 1. Stop the container

```bash
docker compose down
```

### 2. Create a temporary venv

```bash
python3 -m venv /tmp/garmin-bootstrap
source /tmp/garmin-bootstrap/bin/activate
pip install requests
```

### 3. Run the bootstrap script

```bash
python3 src/bootstrap_auth.py config.toml --token-dir ~/data/garminnostra/tokens
```

For each Garmin user in your config:
1. Your default browser opens the Garmin SSO login page
2. Log in normally (handle CAPTCHA/MFA as usual)
3. After login, Garmin redirects to `https://connect.garmin.com/app?ticket=ST-xxxxx`
4. Copy the **full URL** from the browser address bar
5. Paste it into the terminal when prompted

The script extracts the ticket and exchanges it for DI tokens.

### 4. Restart the container

```bash
docker compose up -d
```

The logs should now show `Authenticated via saved tokens` instead of a credential
login attempt.

### 5. Clean up the venv

```bash
deactivate
rm -rf /tmp/garmin-bootstrap
```

## Notes

- Tokens are saved to `~/data/garminnostra/tokens/<username>/garmin_tokens.json`
  with permissions `600` (owner read/write only).
- Once tokens exist, the sync service refreshes tokens transparently until the
  refresh token expires (up to ~1 year).
- If you add a new user to `config.toml` later, re-run the bootstrap script.
