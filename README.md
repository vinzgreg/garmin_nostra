# garmin-nostra

Dockerised Python service that automatically syncs **Garmin Connect** and/or **Wahoo** activities for multiple users.

For each new activity it:
- stores all metrics in a local SQLite database
- downloads and saves the GPX file
- renders a map image of the GPS track (OpenStreetMap tiles)
- posts a Mastodon mention to the user with key stats and the map (public or unlisted, per-user configurable)
- optionally pushes a CalDAV event to e.g. Nextcloud calendar

Messages and calendar entries are formatted in **German** with metric units.

---

## Features

| Feature | Details |
|---|---|
| Multi-user | One `[[users]]` block per account (Garmin or Wahoo) |
| Wahoo support | Sync from Wahoo Cloud API; optionally upload activities to Garmin Connect |
| Mastodon post | Bot mentions the user; visibility is `public` or `unlisted` per user |
| Activity stats | Duration, distance, pace/speed, elevation, power, heart rate |
| Map image | GPX track rendered as PNG, attached to the DM |
| GPX + FIT files | Original GPX and FIT files downloaded and stored per activity |
| KudosMachine | Polls activity posts for favourites and auto-replies with a kudos message mentioning the fav-giver; 100 random German messages or a custom template |
| CalDAV | Optional per-user; pushes VEVENT to an iCal compatible calendar |
| SQLite | All Garmin data stored; queryable by user, type, time |
| Token caching | Garmin OAuth tokens persisted per user — avoids repeated logins |
| Retry | Failed integrations (CalDAV/Mastodon) are retried on the next run |

---

## Message format

The bot posts a Mastodon mention to each user. Visibility is `unlisted` by default (boostable, but not on the public timeline); set `mastodon_public = true` for fully public posts.

**Running example:**
```
🏃 Morgenlauf – Di., 04. März 2025, 07:15 Uhr
⏱ 45:32  📏 8,50 km  💨 5:21 min/km
📈 115 m Anstieg  ❤️ Ø 148 bpm

#Laufen #GarminNostra @alice@fosstodon.org
```

**Cycling example:**
```
🚴 Nachmittagsfahrt – Di., 04. März 2025, 14:30 Uhr
⏱ 1:12:40  📏 38,20 km  💨 31,6 km/h
📈 540 m Anstieg  ⚡ Ø 210 W  ❤️ Ø 142 bpm

#Radfahren #GarminNostra @bob@mastodon.social
```

Attached: a 800×600 PNG map of the GPS track.

---

## Quick start

### 1. Clone and configure

```bash
git clone https://github.com/vinzgreg/garmin_nostra.git
cd garmin-nostra
cp config.toml.example config.toml
$EDITOR config.toml // editor like nano, vim...
```

### 2. Create the data directory

I have this data directory as part of my home directory. Don't be confused, in the config-file it will refer to it as /data... not ~/data.

```bash
mkdir -p ~/data/garminnostra
```

The container runs as non-root user `appuser` (UID 1000). If your host user has a different UID, adjust ownership:

```bash
sudo chown -R 1000:1000 ~/data/garminnostra
```

### 3. Build and start

```bash
docker compose up -d --build
docker compose logs -f
```

On first start the container runs an immediate sync, then loops at the configured `interval_minutes`.

---

## Wahoo setup

To sync activities from a Wahoo account you need a Wahoo developer app and an OAuth refresh token. This is a one-time setup per user.

### 1. Register a Wahoo developer app

1. Go to [developers.wahooligan.com/cloud](https://developers.wahooligan.com/cloud) and sign in with your Wahoo account.
2. Create a new application with the following settings:

   | Field | Value |
   |---|---|
   | **Redirect URI** | `https://localhost` |
   | **Environment** | Sandbox (switch to Production once confirmed working) |
   | **Confidential** | Yes |
   | **Webhook** | Leave blank — not needed (garmin-nostra polls the API) |

3. Note the **Client ID** and **Client Secret** from the app page.

### 2. Obtain a refresh token

Run the bootstrap helper (on the host or inside the container):

```bash
# On the host (if you have Python + requests installed):
python3 src/wahoo_auth.py <client_id> <client_secret>

# Or inside a running container:
docker exec -it garmin-nostra python3 /app/src/wahoo_auth.py <client_id> <client_secret>
```

The script will:
1. Print an authorization URL and open it in your browser.
2. After you authorize, the browser redirects to `https://localhost?code=…` — the page will **not load** (this is expected).
3. Copy the `code` parameter from the browser's address bar and paste it back into the script.
4. The script prints your **refresh token**.

> **Note:** Wahoo refresh tokens expire after **60 days of inactivity**. As long as garmin-nostra syncs regularly, the token is refreshed automatically. If sync is paused for more than 60 days, re-run the bootstrap.

### 3. Configure the user

There are three source modes. Choose the one that fits your setup:

#### `source = "wahoo"` — Wahoo only

```toml
[[users]]
name                = "carol"
source              = "wahoo"
wahoo_client_id     = "env:CAROL_WAHOO_CLIENT_ID"
wahoo_client_secret = "env:CAROL_WAHOO_CLIENT_SECRET"
wahoo_refresh_token = "env:CAROL_WAHOO_REFRESH_TOKEN"
mastodon_handle     = "@carol@mastodon.social"
```

Store the actual values as environment variables in `docker-compose.yml`:

```yaml
environment:
  - CAROL_WAHOO_CLIENT_ID=your_client_id
  - CAROL_WAHOO_CLIENT_SECRET=your_client_secret
  - CAROL_WAHOO_REFRESH_TOKEN=your_refresh_token
```

#### `source = "garmin"` — Garmin only (default)

```toml
[[users]]
name            = "alice"
garmin_username = "alice@example.com"
garmin_password = "env:ALICE_GARMIN_PASSWORD"
mastodon_handle = "@alice@mastodon.social"
```

#### `source = "both"` — Wahoo and Garmin, deduplicated

Syncs from both platforms in a single user block. Wahoo is processed first.
Activities are tagged `[Wahoo]` or `[Garmin]` in the database and Mastodon posts.
No cross-platform upload happens — each activity stays on its original source.

If a Wahoo workout was auto-synced to Garmin (e.g. via the native Wahoo→Garmin integration), garmin-nostra detects the duplicate by matching start times (±2 minute window) and skips the Garmin copy. Nothing is posted or stored twice.

```toml
[[users]]
name                = "dave"
source              = "both"
wahoo_client_id     = "your_client_id_here"
wahoo_client_secret = "your_client_secret_here"
wahoo_refresh_token = "your_refresh_token_here"
garmin_username     = "dave@example.com"
garmin_password     = "your_garmin_password_here"
mastodon_handle     = "@dave@mastodon.social"
```

#### `source = "both_garmin_target"` — both sources, Wahoo pushed to Garmin

Like `"both"`, but Wahoo activities are automatically uploaded to Garmin Connect as FIT files (`wahoo_sync_to_garmin` is implied). Use this when Garmin Connect is the primary archive and Wahoo is a secondary source. Garmin-native activities already live on Garmin and are not pushed anywhere.

Duplicate detection applies: if Wahoo already auto-synced an activity to Garmin natively, the upload is skipped.

```toml
[[users]]
name                = "eve"
source              = "both_garmin_target"
wahoo_client_id     = "your_client_id_here"
wahoo_client_secret = "your_client_secret_here"
wahoo_refresh_token = "your_refresh_token_here"
garmin_username     = "eve@example.com"
garmin_password     = "your_garmin_password_here"
mastodon_handle     = "@eve@mastodon.social"
```

### 4. Optional: sync Wahoo activities to Garmin Connect

Only relevant for `source = "wahoo"`. To upload Wahoo activities to Garmin Connect automatically, add Garmin credentials to the same user block:

```toml
wahoo_sync_to_garmin = true
garmin_username      = "carol@example.com"
garmin_password      = "env:CAROL_GARMIN_PASSWORD"
```

Activities are uploaded as FIT files. If Wahoo has already synced the same activity to Garmin natively, the duplicate is detected and skipped.

> **Note:** For `source = "both"`, use `source = "both_garmin_target"` instead of setting `wahoo_sync_to_garmin` manually — it handles dedup correctly.

---

## Operations

### Logs

```bash
# All output (startup, sync runs) goes to Docker's log
docker logs garmin-nostra -f
```

To also persist logs to a file, add `log_file = "/data/garmin_nostra.log"` to the `[storage]` section of `config.toml`, then tail it:

```bash
docker exec garmin-nostra tail -f /data/garmin_nostra.log
```

### Manual sync

Trigger a sync immediately without waiting for the next scheduled run:

```bash
docker exec garmin-nostra python3 /app/src/sync.py /app/config.toml
```

### Inspect the database

```bash
# Open an interactive SQLite shell
docker exec -it garmin-nostra sqlite3 /data/garmin_nostra.db

# Total activity count and number of users
docker exec garmin-nostra sqlite3 /data/garmin_nostra.db \
  "SELECT count(distinct garmin_activity_id) as activity_count,
          count(distinct user_id) as unique_users
   FROM activities;"

# Recent activities (last 20)
docker exec garmin-nostra sqlite3 /data/garmin_nostra.db \
  "SELECT garmin_activity_id, user_id, start_time_utc, activity_type
   FROM activities ORDER BY start_time_utc DESC LIMIT 20;"

# Check which activities have been posted to Mastodon
docker exec garmin-nostra sqlite3 /data/garmin_nostra.db \
  "SELECT garmin_activity_id, user_id, mastodon_posted, caldav_pushed
   FROM activities ORDER BY start_time_utc DESC LIMIT 20;"

# Pending Mastodon posts
docker exec garmin-nostra sqlite3 /data/garmin_nostra.db \
  "SELECT u.name, a.garmin_activity_id, a.activity_type, a.start_time_utc
   FROM activities a JOIN users u ON a.user_id = u.id
   WHERE a.mastodon_posted = 0 ORDER BY a.start_time_utc;"
```

### Apply configuration changes

`config.toml` is mounted read-only; edit it on the host then restart:

```bash
docker compose restart garmin-nostra
```

A change to `interval_minutes` requires a restart so the new sleep interval takes effect.

### Rebuild after code changes

```bash
docker compose up -d --build
```

### Stop / remove

```bash
docker compose down          # stop and remove the container (data on host is kept)
```

---

## Configuration

All settings live in `config.toml` (git-ignored). See `config.toml.example` for a full template.

### `[bot]`

| Key | Description |
|---|---|
| `mastodon_api_base_url` | Base URL of the Mastodon instance the bot account lives on |
| `mastodon_access_token` | OAuth access token for the bot account |
| `kudosCustom` | *(optional)* Custom kudos reply template. Supports `{fav_giver}` and `{activity_user}` placeholders. If omitted, a random message from the built-in pool of 100 is used. |

Create the bot account on your preferred instance, go to **Preferences → Development → New application**, grant the following scopes, and copy the access token:

| Scope | Purpose |
|---|---|
| `read:statuses` | Fetch who favourited an activity post (KudosMachine) |
| `write:statuses` | Post activity summaries and kudos replies |
| `write:media` | Upload map images |

### `[sync]`

| Key | Default | Description |
|---|---|---|
| `interval_minutes` | `60` | How often the cron job runs |
| `lookback_days` | `30` | How far back to look on first run per user |
| `gpx_max_age_days` | *(unset)* | Skip GPX download for activities older than N days; omit to always download |
| `fit_max_age_days` | *(unset)* | Skip FIT download for activities older than N days; omit to always download |
| `mastodon_max_age_days` | *(unset)* | Skip Mastodon posts for activities older than N days (avoids rate limits on backfill) |
| `mastodon_post_delay_s` | `2.0` | Seconds to wait between consecutive Mastodon posts (avoids rate limits) |
| `request_timeout_s` | `30` | Timeout in seconds for all external HTTP calls |

### `[storage]`

| Key | Example value | Description |
|---|---|---|
| `db_path` | `/data/garmin_nostra.db` | SQLite database |
| `gpx_dir` | `/data/gpx` | GPX files |
| `fit_dir` | `/data/fit` | FIT files |
| `map_dir` | `/data/maps` | Map images |
| `token_dir` | `/data/tokens` | Garmin OAuth tokens (one subdirectory per user `name`) |
| `log_level` | `info` | Log verbosity: `debug`, `info`, or `error` |
| `log_file` | `/data/garmin_nostra.log` | *(optional)* Write logs to this file in addition to stdout |

> **Critical:** All paths must start with `/data/`. The container's only access to the host filesystem is through the volume mount `~/data/garminnostra → /data`. Do **not** use `~`, `~/data/...`, `/home/vinz/...`, or any other host path — those paths do not exist inside the container and the token/file lookup will silently fail.
>
> The corresponding host paths are:
> | Container path | Host path |
> |---|---|
> | `/data/garmin_nostra.db` | `~/data/garminnostra/garmin_nostra.db` |
> | `/data/gpx/` | `~/data/garminnostra/gpx/` |
> | `/data/fit/` | `~/data/garminnostra/fit/` |
> | `/data/maps/` | `~/data/garminnostra/maps/` |
> | `/data/tokens/<name>/` | `~/data/garminnostra/tokens/<name>/` |

### `[caldav]` *(optional)*

Remove this section to disable CalDAV globally. Individual users also need `caldav_enabled = true`.

| Key | Description |
|---|---|
| `url` | CalDAV root, e.g. `https://nextcloud.example.com/remote.php/dav` |
| `username` | Nextcloud username |
| `password` | Nextcloud password (or app password) |
| `calendar_name` | Name of the target calendar (must already exist) |

### `[[users]]`

One block per account (Garmin or Wahoo):

| Key | Required | Description |
|---|---|---|
| `name` | ✓ | Unique identifier used for file/token paths |
| `source` | `"garmin"` | `"garmin"` (default), `"wahoo"`, `"both"`, or `"both_garmin_target"` — see [source modes](#3-configure-the-user) |
| `garmin_username` | Garmin/sync | Garmin Connect e-mail (required for `source = "garmin"` or `wahoo_sync_to_garmin`) |
| `garmin_password` | Garmin/sync | Garmin Connect password |
| `wahoo_client_id` | Wahoo | Wahoo developer app client ID |
| `wahoo_client_secret` | Wahoo | Wahoo developer app client secret |
| `wahoo_refresh_token` | Wahoo | OAuth refresh token (obtained via `wahoo_auth.py`) |
| `wahoo_sync_to_garmin` | `false` | Upload Wahoo activities to Garmin Connect (requires Garmin credentials) |
| `mastodon_handle` | — | `@user@instance` — the bot will mention this handle |
| `mastodon_public` | `false` | `true` = public post, `false` = unlisted (boostable but not on public timeline) |
| `caldav_enabled` | `false` | Set `true` to push CalDAV events for this user |
| `suppressKudos` | `false` | Set `true` to opt this user out of kudos replies |

---

## Data directory layout

```
~/data/garminnostra/
├── garmin_nostra.db      # SQLite database
├── gpx/
│   ├── alice/
│   │   └── 12345678.gpx
│   └── bob/
│       └── 87654321.gpx
├── fit/
│   ├── alice/
│   │   └── 12345678.fit
│   └── bob/
│       └── 87654321.fit
├── maps/
│   ├── alice/
│   │   └── 12345678.png
│   └── bob/
│       └── 87654321.png
└── tokens/
    ├── alice/            # Garmin OAuth tokens
    └── bob/
```

The data directory is bind-mounted from the host (`~/data/garminnostra` by default — change the left side of the volume in `docker-compose.yml` to relocate it). All files survive container rebuilds.

---

## Database schema

### `users`
| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT UNIQUE | Config name |
| `garmin_username` | TEXT | |
| `mastodon_handle` | TEXT | |
| `caldav_enabled` | INTEGER | 0/1 |
| `created_at` | TEXT | ISO-8601 UTC |

### `activities`
One row per activity per user. Key columns:

| Column | Type | Description |
|---|---|---|
| `user_id` | INTEGER FK | |
| `garmin_activity_id` | TEXT | Garmin's ID |
| `activity_type` | TEXT | `running`, `cycling`, … |
| `start_time_utc` | TEXT | ISO-8601 UTC |
| `duration_s` | REAL | Total duration (seconds) |
| `distance_m` | REAL | Distance (metres) |
| `elevation_gain_m` | REAL | Positive elevation (metres) |
| `avg_hr` | INTEGER | Avg heart rate (bpm) |
| `avg_power_w` | REAL | Avg power (watts) |
| `normalized_power_w` | REAL | NP (watts) |
| `avg_speed_ms` | REAL | Avg speed (m/s) |
| `training_stress_score` | REAL | TSS |
| `vo2max_estimate` | REAL | |
| `calories` | INTEGER | |
| `raw_json` | TEXT | Full Garmin API payload |
| `gpx_path` | TEXT | Path to saved GPX file |
| `fit_path` | TEXT | Path to saved FIT file |
| `source` | TEXT | Origin of the record: `GarminNoStra` or `WahooNoStra` |
| `caldav_pushed` | INTEGER | 0/1 |
| `mastodon_posted` | INTEGER | 0/1 |

Full column list: see `src/storage.py`.

### `kudos_sent`
Deduplication log for KudosMachine — one row per (status, fav-giver) pair.

| Column | Type | Description |
|---|---|---|
| `status_id` | TEXT PK | Mastodon status ID of the activity post |
| `account_id` | TEXT PK | Mastodon account ID of the fav-giver |
| `sent_at` | TEXT | ISO-8601 UTC timestamp |

### `sync_runs`
Audit log — one row per sync attempt per user.

---

## Useful SQL queries

```sql
-- Total km per user this year
SELECT u.name, ROUND(SUM(a.distance_m) / 1000.0, 1) AS km
FROM activities a JOIN users u ON a.user_id = u.id
WHERE a.start_time_utc >= '2025-01-01'
GROUP BY u.name;

-- Monthly running km for alice
SELECT SUBSTR(start_time_utc, 1, 7) AS month,
       ROUND(SUM(distance_m) / 1000.0, 1) AS km,
       COUNT(*) AS runs
FROM activities
WHERE user_id = (SELECT id FROM users WHERE name = 'alice')
  AND activity_type = 'running'
GROUP BY month ORDER BY month;

-- Average pace trend (running) for alice
SELECT SUBSTR(start_time_utc, 1, 7) AS month,
       ROUND(AVG(duration_s / (distance_m / 1000.0)), 0) AS avg_pace_s_per_km
FROM activities
WHERE user_id = (SELECT id FROM users WHERE name = 'alice')
  AND activity_type = 'running' AND distance_m > 0
GROUP BY month ORDER BY month;

-- Activities with pending Mastodon post
SELECT u.name, a.garmin_activity_id, a.activity_type, a.start_time_utc
FROM activities a JOIN users u ON a.user_id = u.id
WHERE a.mastodon_posted = 0
ORDER BY a.start_time_utc;
```

---

## Module overview

| File | Role |
|---|---|
| `src/sync.py` | Main entry point; iterates users, orchestrates pipeline |
| `src/garmin.py` | Garmin Connect client with per-user token caching |
| `src/wahoo.py` | Wahoo Cloud API client with OAuth 2.0 token refresh |
| `src/wahoo_auth.py` | One-time OAuth bootstrap helper to obtain Wahoo refresh tokens |
| `src/storage.py` | SQLite store — users, activities, kudos deduplication, sync audit log |
| `src/format.py` | German formatting: dates, numbers, pace, message builder |
| `src/map_render.py` | GPX → PNG via `staticmap` (OSM tiles) |
| `src/mastodon_bot.py` | Bot that posts mentions with optional map attachment (public or unlisted) |
| `src/kudos_machine.py` | Polls activity posts for new favourites and sends kudos replies |
| `src/caldav_push.py` | Builds VEVENT and pushes to Nextcloud CalDAV |

---

## Requirements

- Docker & Docker Compose
- A Mastodon bot account with `read:statuses write:statuses write:media` scopes
- Garmin Connect credentials per Garmin user
- *(optional)* Wahoo developer app credentials per Wahoo user (register at [developers.wahooligan.com](https://developers.wahooligan.com/cloud))
- *(optional)* A Nextcloud CalDAV calendar

Python dependencies (installed inside the container):

```
garminconnect  caldav  icalendar  Mastodon.py
gpxpy  staticmap  Pillow  requests
```

---

## Troubleshooting

**Garmin authentication fails (401)**
Garmin's SSO can block automated credential logins from servers. The solution is to generate OAuth tokens interactively inside the container (where `garth` is already installed) and persist them to the data volume:

```bash
docker exec -it garmin-nostra python3 -c "import garth, getpass; garth.login('<garmin_username>', getpass.getpass('password: ')); garth.save('/data/tokens/<name>')"
```

Replace `<garmin_username>` and `<name>` (the user's `name` from `config.toml`). You will be prompted for the password. The tokens are written to `/data/tokens/<name>/` which is persisted on the host under `~/data/garminnostra/tokens/<name>/`.

After this, the next sync will load the saved tokens and skip the credential login entirely.

If you already have working tokens from a previous installation, you can copy them directly:

```bash
cp -r /old/path/tokens/<name> ~/data/garminnostra/tokens/<name>
```

**Garmin MFA / 2FA**
The interactive `garth.login()` command above also handles MFA — it will prompt for the one-time code if required.

**Map not attached**
The `staticmap` library fetches tiles from `tile.openstreetmap.org`. Make sure the container has outbound internet access. Indoor activities without GPS will not produce a map.

**Mastodon post not visible**
For `unlisted` posts, the post appears in the mentioned user's notifications and on the bot's profile, but not on the public timeline. To make posts appear publicly, set `mastodon_public = true` for that user. On some instances, mentions from unfollowed accounts land in filtered notifications.

**Wahoo authentication fails**
Re-run the OAuth bootstrap to obtain a fresh refresh token — see [Wahoo setup](#wahoo-setup) above for the full procedure. Wahoo refresh tokens expire after 60 days of inactivity.

**Wahoo activities have no map image**
Wahoo does not provide GPX files. Map rendering is currently only available for Garmin activities. FIT files are downloaded and stored.

**CalDAV calendar not found**
The calendar must already exist in Nextcloud. The error message lists available calendar names.

---

## Migrating from older versions

### Non-root container (March 2026)

The container no longer runs as `root`. Instead it uses a non-root user `appuser` with **UID 1000 / GID 1000**. This improves security but requires a one-time ownership fix on the data directory:

```bash
sudo chown -R 1000:1000 ~/data/garminnostra
```

> **Why?** Older versions ran as root inside Docker, so all files (GPX, FIT, maps, tokens, log, DB) were created with `root:root` ownership. The new non-root container cannot write to root-owned files.

You can verify the result with:

```bash
# Should return nothing (= no files left with wrong ownership)
find ~/data/garminnostra -not -user 1000 -ls
```

If your host user has a UID other than 1000, either adjust the `chown` to match the container's UID (1000), or override the container's user in `docker-compose.yml`:

```yaml
services:
  garmin-nostra:
    user: "1001:1001"   # replace with your host UID:GID
```

Then `chown` the data directory to match that UID instead.

### Cron replaced by sleep loop (March 2026)

The container no longer installs or uses `cron`. Sync scheduling is now a simple shell loop (`sleep` between runs). This means:

- No behaviour change for typical use — sync still runs at the configured `interval_minutes`.
- The interval is measured from end-of-sync to start-of-next-sync, not wall-clock aligned. For a 60-minute interval with a 2-minute sync, the next run starts at minute 62 instead of exactly on the hour. This is negligible in practice.
- Logs go directly to stdout (no `/proc/1/fd` redirects), which is cleaner for `docker logs`.

No action needed — just rebuild:

```bash
docker compose up -d --build
```

### Garmin API session reuse (March 2026)

GPX and FIT downloads now reuse the already-authenticated Garmin session instead of creating a fresh client (with a full OAuth token exchange + profile fetch) for every single download. For a sync with *N* new activities this eliminates **2×N** redundant authentication round-trips, cutting per-activity overhead by ~2 seconds each.

No action needed — the change is internal to `src/garmin.py`. Per-download timeouts via `ThreadPoolExecutor` are still in place.

### Environment variable secrets (March 2026)

Config values can now reference environment variables with the `env:` prefix. This is **opt-in** — existing plaintext configs work unchanged.

Before (plaintext in config.toml):
```toml
mastodon_access_token = "abc123secrettoken"
```

After (secret in environment, reference in config):
```toml
mastodon_access_token = "env:MASTODON_TOKEN"
```

```yaml
# docker-compose.yml
environment:
  - MASTODON_TOKEN=abc123secrettoken
```

This avoids storing secrets in the config file and works with Docker secrets, `.env` files, or CI/CD variable injection.

### Wahoo support (March 2026)

Users can now sync activities from Wahoo instead of Garmin Connect. This is **opt-in** — existing Garmin-only configurations work unchanged without any modifications.

See [Wahoo setup](#wahoo-setup) for the full setup procedure (developer app registration, OAuth bootstrap, config).

The database schema is extended automatically (two new columns added on first run). No manual migration needed.
