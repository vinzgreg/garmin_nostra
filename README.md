# garmin-nostra

Dockerised Python service that automatically syncs Garmin Connect activities for multiple users.

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
| Multi-user | One `[[users]]` block per Garmin account |
| Mastodon post | Bot mentions the user; visibility is `public` or `unlisted` per user |
| Activity stats | Duration, distance, pace/speed, elevation, power, heart rate |
| Map image | GPX track rendered as PNG, attached to the DM |
| GPX + FIT files | Original GPX and FIT files downloaded and stored per activity |
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

### 3. Build and start

```bash
docker compose up -d --build
docker compose logs -f
```

On first start the container runs an immediate sync, then schedules a cron job at the configured `interval_minutes`.

---

## Operations

### Logs

```bash
# All output (startup, cron runs, sync script) goes to Docker's log
docker logs garmin-nostra -f
```

To also persist logs to a file, add `log_file = "/data/garmin_nostra.log"` to the `[storage]` section of `config.toml`, then tail it:

```bash
docker exec garmin-nostra tail -f /data/garmin_nostra.log
```

### Manual sync

Trigger a sync immediately without waiting for the next cron run:

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

A change to `interval_minutes` requires a restart so the new cron schedule is written.

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

Create the bot account on your preferred instance, go to **Preferences → Development → New application**, grant `write:statuses write:media` scopes, and copy the access token.

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

One block per Garmin Connect account:

| Key | Required | Description |
|---|---|---|
| `name` | ✓ | Unique identifier used for file/token paths |
| `garmin_username` | ✓ | Garmin Connect e-mail |
| `garmin_password` | ✓ | Garmin Connect password |
| `mastodon_handle` | — | `@user@instance` — the bot will mention this handle |
| `mastodon_public` | `false` | `true` = public post, `false` = unlisted (boostable but not on public timeline) |
| `caldav_enabled` | `false` | Set `true` to push CalDAV events for this user |

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
| `source` | TEXT | Origin of the record (always `GarminNoStra`) |
| `caldav_pushed` | INTEGER | 0/1 |
| `mastodon_posted` | INTEGER | 0/1 |

Full column list: see `src/storage.py`.

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
| `src/storage.py` | SQLite store — users, activities, sync audit log |
| `src/format.py` | German formatting: dates, numbers, pace, message builder |
| `src/map_render.py` | GPX → PNG via `staticmap` (OSM tiles) |
| `src/mastodon_bot.py` | Bot that posts mentions with optional map attachment (public or unlisted) |
| `src/caldav_push.py` | Builds VEVENT and pushes to Nextcloud CalDAV |

---

## Requirements

- Docker & Docker Compose
- A Mastodon bot account with `write:statuses write:media` scope
- Garmin Connect credentials per user
- *(optional)* A Nextcloud CalDAV calendar

Python dependencies (installed inside the container):

```
garminconnect  caldav  icalendar  Mastodon.py
gpxpy  staticmap  Pillow
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

**CalDAV calendar not found**
The calendar must already exist in Nextcloud. The error message lists available calendar names.
