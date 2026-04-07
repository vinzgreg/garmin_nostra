# Garmin Connect — Browser Bootstrap (Cloudflare bypass)

Use this when the normal programmatic login is blocked by Cloudflare (429 errors).
The script opens Garmin's login page in your real browser (Firefox, Chrome, etc.),
you log in normally, then paste the service ticket back into the script. It exchanges
the ticket for DI OAuth tokens and writes `garmin_tokens.json` so the sync service
can authenticate without hitting SSO again.

## Prerequisites

Run on the **host machine** (not inside the Docker container) — needs a display.

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

**Standalone (one user, no config.toml needed):**

```bash
python3 src/bootstrap_auth.py -o ~/data/garminnostra/tokens/betty
```

**Multi-user via config.toml:**

```bash
python3 src/bootstrap_auth.py --config config.toml --token-dir ~/data/garminnostra/tokens
```

**Single user from config:**

```bash
python3 src/bootstrap_auth.py --config config.toml --token-dir ~/data/garminnostra/tokens --user betty
```

**Use a specific browser:**

```bash
python3 src/bootstrap_auth.py -o ~/data/garminnostra/tokens/betty --browser firefox
```

For each user:

1. Your browser opens the Garmin SSO login page
2. **Before logging in**, press **F12** → Console tab
3. In Firefox, type `allow pasting` and press Enter (ignore the error)
4. Paste and run:
   ```js
   window.addEventListener('beforeunload', function(e) { e.preventDefault(); e.returnValue = ''; });
   ```
5. Log in normally (handle CAPTCHA/MFA as usual)
6. A **"Leave this page?"** dialog appears — click **Stay on Page**
7. Switch to the **Network** tab, filter by `login`
8. Click the `/portal/api/login` request → **Response** tab
9. Copy the `serviceTicketId` value (`ST-xxxxx`)
10. Paste it into the terminal when prompted

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

- Tokens are saved as `garmin_tokens.json` in the output directory
  with permissions `600` (owner read/write only).
- Once tokens exist, the sync service refreshes tokens transparently until the
  refresh token expires (up to ~1 year).
- If you add a new user to `config.toml` later, re-run the bootstrap script.
- You can copy working `garmin_tokens.json` files between installations.
