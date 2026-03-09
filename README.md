# garmin-nostra

Dockerised Python service that automatically syncs Garmin Connect activities for multiple users.

For each new activity it:
- stores all metrics in a local SQLite database
- downloads and saves the GPX file
- renders a map image of the GPS track (OpenStreetMap tiles)
- sends a **private Mastodon DM** to the user with key stats and the map
- optionally pushes a CalDAV event to a Nextcloud calendar

Messages and calendar entries are formatted in **German** with metric units.

---

## Features

| Feature | Details |
|---|---|
| Multi-user | One `[[users]]` block per Garmin account |
| Mastodon DM | Bot sends direct mention; never appears on public timeline |
| Activity stats | Duration, distance, pace/speed, elevation, power, heart rate |
| Map image | GPX track rendered as PNG, attached to the DM |
| CalDAV | Optional per-user; pushes VEVENT to a Nextcloud calendar |
| SQLite | All Garmin data stored; queryable by user, type, time |
| Token caching | Garmin OAuth tokens persisted per user — avoids repeated logins |
| Retry | Failed integrations (CalDAV/Mastodon) are retried on the next run |

---

## Message format

The bot sends a direct Mastodon mention (visibility: `direct`) to each user:

**Running example:**
```
@alice@fosstodon.org

🏃 Morgenlauf – Di., 04. März 2025, 07:15 Uhr
⏱ 45:32  📏 8,50 km  💨 5:21 min/km
📈 115 m Anstieg  ❤️ Ø 148 bpm

#Laufen #Garmin
```

**Cycling example:**
```
@bob@mastodon.social

🚴 Nachmittagsfahrt – Di., 04. März 2025, 14:30 Uhr
⏱ 1:12:40  📏 38,20 km  💨 31,6 km/h
📈 540 m Anstieg  ⚡ Ø 210 W  ❤️ Ø 142 bpm

#Radfahren #Garmin
```

Attached: a 800×600 PNG map of the GPS track.

---

## Quick start

### 1. Clone and configure

```bash
git clone <repo-url> garmin-nostra
cd garmin-nostra
cp config.toml.example config.toml
$EDITOR config.toml
```

### 2. Create the data directory

```bash
mkdir -p data/gpx data/maps data/tokens
```

### 3. Build and run

```bash
docker compose up -d
docker compose logs -f
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
| `mastodon_handle` | — | `@user@instance` — the bot will DM this handle |
| `caldav_enabled` | `false` | Set `true` to push CalDAV events for this user |

---

## Data directory layout

```
data/
├── garmin_nostra.db      # SQLite database
├── gpx/
│   ├── alice/
│   │   └── 12345678.gpx
│   └── bob/
│       └── 87654321.gpx
├── maps/
│   ├── alice/
│   │   └── 12345678.png
│   └── bob/
│       └── 87654321.png
└── tokens/
    ├── alice/            # Garmin OAuth tokens
    └── bob/
```

The `data/` directory is bind-mounted from the host so all files survive container rebuilds.

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
| `src/mastodon_bot.py` | Bot that sends DM mentions with optional map attachment |
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

**Garmin MFA / 2FA**
On first run the container must be able to complete authentication. If your account uses MFA you will need to pre-authenticate interactively:
```bash
docker compose run --rm garmin-nostra python3 /app/src/garmin.py /app/config.toml
```
Follow any prompts; the token is then cached in `data/tokens/<name>/` for future headless runs.

**Map not attached**
The `staticmap` library fetches tiles from `tile.openstreetmap.org`. Make sure the container has outbound internet access. Indoor activities without GPS will not produce a map.

**Mastodon DM not visible**
Ensure the bot account has sent a follow request to the user (or the user follows the bot). On some instances DMs from unfollowed accounts land in filtered notifications.

**CalDAV calendar not found**
The calendar must already exist in Nextcloud. The error message lists available calendar names.
