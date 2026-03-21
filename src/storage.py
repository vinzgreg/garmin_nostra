"""Multi-user SQLite store — activities, users, sync audit log."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Schema ───────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE,
    garmin_username  TEXT    NOT NULL,
    mastodon_handle  TEXT,
    caldav_enabled   INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 INTEGER NOT NULL REFERENCES users(id),
    garmin_activity_id      TEXT    NOT NULL,

    -- identity
    activity_name           TEXT,
    activity_type           TEXT,
    sport_type              TEXT,

    -- time
    start_time_utc          TEXT,
    start_time_local        TEXT,
    timezone                TEXT,
    duration_s              REAL,
    elapsed_time_s          REAL,
    moving_time_s           REAL,

    -- distance & elevation
    distance_m              REAL,
    elevation_gain_m        REAL,
    elevation_loss_m        REAL,
    min_elevation_m         REAL,
    max_elevation_m         REAL,

    -- speed
    avg_speed_ms            REAL,
    max_speed_ms            REAL,

    -- heart rate
    avg_hr                  INTEGER,
    max_hr                  INTEGER,
    resting_hr              INTEGER,

    -- power
    avg_power_w             REAL,
    max_power_w             REAL,
    normalized_power_w      REAL,

    -- cadence / stride
    avg_cadence             INTEGER,
    max_cadence             INTEGER,
    avg_stride_length_m     REAL,
    avg_vertical_osc_cm     REAL,
    avg_ground_contact_ms   REAL,

    -- training load
    aerobic_training_effect REAL,
    training_stress_score   REAL,
    vo2max_estimate         REAL,
    intensity_factor        REAL,

    -- misc
    calories                INTEGER,
    steps                   INTEGER,
    avg_temperature_c       REAL,
    max_temperature_c       REAL,
    start_lat               REAL,
    start_lon               REAL,

    -- full raw payload for future-proofing
    raw_json                TEXT,

    -- sync state
    gpx_path                TEXT,
    fit_path                TEXT,
    source                  TEXT,
    caldav_pushed           INTEGER NOT NULL DEFAULT 0,
    mastodon_posted         INTEGER NOT NULL DEFAULT 0,
    mastodon_status_id      TEXT,
    synced_at               TEXT    NOT NULL,

    UNIQUE(user_id, garmin_activity_id)
);

CREATE TABLE IF NOT EXISTS kudos_sent (
    status_id   TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (status_id, account_id)
);

CREATE INDEX IF NOT EXISTS idx_act_user      ON activities(user_id);
CREATE INDEX IF NOT EXISTS idx_act_type      ON activities(activity_type);
CREATE INDEX IF NOT EXISTS idx_act_start     ON activities(start_time_utc);
CREATE INDEX IF NOT EXISTS idx_act_user_type ON activities(user_id, activity_type);

CREATE TABLE IF NOT EXISTS sync_runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL REFERENCES users(id),
    started_at           TEXT    NOT NULL,
    completed_at         TEXT,
    activities_found     INTEGER NOT NULL DEFAULT 0,
    activities_processed INTEGER NOT NULL DEFAULT 0,
    status               TEXT,
    error_message        TEXT
);
"""


def _activity_type_key(raw: Any) -> str:
    if isinstance(raw, dict):
        return raw.get("typeKey", "workout")
    return str(raw or "workout")


def _sport_type_key(raw: Any) -> str | None:
    if isinstance(raw, dict):
        return raw.get("typeKey")
    return str(raw) if raw else None


def _cadence(act: dict) -> int | None:
    return (
        act.get("averageRunCadence")
        or act.get("averageBikeCadence")
        or act.get("averageCadence")
    )


def _max_cadence(act: dict) -> int | None:
    return act.get("maxRunCadence") or act.get("maxBikeCadence")


def _map_activity(user_id: int, act: dict) -> dict:
    """Map a raw Garmin API activity dict to our storage schema."""
    return {
        "user_id":                 user_id,
        "garmin_activity_id":      str(act["activityId"]),
        "activity_name":           act.get("activityName"),
        "activity_type":           _activity_type_key(act.get("activityType")),
        "sport_type":              _sport_type_key(act.get("sportType")),
        "start_time_utc":          act.get("startTimeGMT"),
        "start_time_local":        act.get("startTimeLocal"),
        "timezone":                act.get("timeZoneId"),
        "duration_s":              act.get("duration"),
        "elapsed_time_s":          act.get("elapsedDuration"),
        "moving_time_s":           act.get("movingDuration"),
        "distance_m":              act.get("distance"),
        "elevation_gain_m":        act.get("elevationGain"),
        "elevation_loss_m":        act.get("elevationLoss"),
        "min_elevation_m":         act.get("minElevation"),
        "max_elevation_m":         act.get("maxElevation"),
        "avg_speed_ms":            act.get("averageSpeed"),
        "max_speed_ms":            act.get("maxSpeed"),
        "avg_hr":                  act.get("averageHR"),
        "max_hr":                  act.get("maxHR"),
        "resting_hr":              act.get("restingHeartRate"),
        "avg_power_w":             act.get("averagePower"),
        "max_power_w":             act.get("maxPower"),
        "normalized_power_w":      act.get("normPower") or act.get("normalizedPower"),
        "avg_cadence":             _cadence(act),
        "max_cadence":             _max_cadence(act),
        "avg_stride_length_m":     act.get("avgStrideLength"),
        "avg_vertical_osc_cm":     act.get("avgVerticalOscillation"),
        "avg_ground_contact_ms":   act.get("avgGroundContactTime"),
        "aerobic_training_effect": act.get("aerobicTrainingEffect"),
        "training_stress_score":   act.get("trainingStressScore"),
        "vo2max_estimate":         act.get("vo2MaxValue"),
        "intensity_factor":        act.get("intensityFactor"),
        "calories":                act.get("calories"),
        "steps":                   act.get("steps"),
        "avg_temperature_c":       act.get("averageTemperature"),
        "max_temperature_c":       act.get("maxTemperature"),
        "start_lat":               act.get("startLatitude"),
        "start_lon":               act.get("startLongitude"),
        "raw_json":                json.dumps(act, ensure_ascii=False),
        "gpx_path":                None,
        "fit_path":                None,
        "source":                  "GarminNoStra",
        "caldav_pushed":           0,
        "mastodon_posted":         0,
        "synced_at":               datetime.now(timezone.utc).isoformat(),
    }


class ActivityStore:
    def __init__(self, db_path: str, gpx_dir: str, map_dir: str, token_dir: str, fit_dir: str = "/data/fit") -> None:
        self.gpx_dir   = Path(gpx_dir)
        self.fit_dir   = Path(fit_dir)
        self.map_dir   = Path(map_dir)
        self.token_dir = Path(token_dir)
        for d in (self.gpx_dir, self.fit_dir, self.map_dir, self.token_dir):
            d.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent readers (e.g. manual sqlite3 queries)
        # while the sync process is writing.  Silently skip if the DB is
        # not yet writable (e.g. first-run before ownership is fixed).
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.OperationalError as exc:
            logger.warning("Could not enable WAL/foreign_keys: %s", exc)
        self._conn.executescript(_DDL)
        self._migrate()
        self._conn.commit()
        logger.info("Storage ready. DB: %s", db_path)

    def _migrate(self) -> None:
        """Apply incremental schema changes to existing databases."""
        for col, definition in [
            ("fit_path", "TEXT"),
            ("source", "TEXT"),
            ("mastodon_status_id", "TEXT"),
            ("wahoo_activity_id", "TEXT"),
            ("wahoo_synced_to_garmin", "INTEGER DEFAULT 0"),
        ]:
            try:
                self._conn.execute(f"ALTER TABLE activities ADD COLUMN {col} {definition}")
                self._conn.commit()
                logger.info("Migration: added %s column to activities.", col)
            except sqlite3.OperationalError:
                pass  # Column already exists
        # Backfill source for activities imported before the column existed
        self._conn.execute(
            "UPDATE activities SET source = 'GarminNoStra' WHERE source IS NULL"
        )
        self._conn.commit()

    # ── Users ────────────────────────────────────────────────────────────────

    def upsert_user(self, cfg: dict) -> int:
        """Insert or update user from config block. Returns user_id.

        garmin_username is optional for Wahoo-only users (stored as empty string).
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO users (name, garmin_username, mastodon_handle, caldav_enabled, created_at)
            VALUES (:name, :garmin_username, :mastodon_handle, :caldav_enabled, :created_at)
            ON CONFLICT(name) DO UPDATE SET
                garmin_username = excluded.garmin_username,
                mastodon_handle = excluded.mastodon_handle,
                caldav_enabled  = excluded.caldav_enabled
            """,
            {
                "name":            cfg["name"],
                "garmin_username": cfg.get("garmin_username", ""),
                "mastodon_handle": cfg.get("mastodon_handle"),
                "caldav_enabled":  1 if cfg.get("caldav_enabled") else 0,
                "created_at":      now,
            },
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM users WHERE name = ?", (cfg["name"],)
        ).fetchone()
        return row["id"]

    # ── Activity tracking ────────────────────────────────────────────────────

    def get_activity(self, user_id: int, garmin_activity_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM activities WHERE user_id = ? AND garmin_activity_id = ?",
            (user_id, garmin_activity_id),
        ).fetchone()
        return dict(row) if row else None

    def get_activity_near_time(
        self, user_id: int, start_time_utc: str, window_s: int = 120, source: str | None = None
    ) -> dict | None:
        """Return any activity for user_id whose start_time_utc is within window_s seconds.

        Used for cross-source dedup: detects when the same physical workout
        exists in both Garmin and Wahoo (e.g. Wahoo auto-synced to Garmin).
        Pass source='WahooNoStra' to restrict to Wahoo-origin activities.
        """
        query = (
            "SELECT * FROM activities "
            "WHERE user_id = ? "
            "AND ABS(CAST(strftime('%s', start_time_utc) AS INTEGER) "
            "      - CAST(strftime('%s', ?) AS INTEGER)) <= ?"
        )
        params: list = [user_id, start_time_utc, window_s]
        if source:
            query += " AND source = ?"
            params.append(source)
        query += " LIMIT 1"
        row = self._conn.execute(query, params).fetchone()
        return dict(row) if row else None

    def save_activity(
        self,
        user_id: int,
        raw_activity: dict,
        gpx_path: Path | None = None,
        fit_path: Path | None = None,
        name_prefix: str | None = None,
    ) -> dict:
        """
        Insert activity into DB (ignore if already exists).
        Returns the mapped row dict (usable for message formatting).
        *name_prefix* (e.g. "[Garmin] ") is prepended to activity_name when set.
        """
        row = _map_activity(user_id, raw_activity)
        if name_prefix:
            row["activity_name"] = name_prefix + (row.get("activity_name") or "Activity")
        if gpx_path:
            row["gpx_path"] = str(gpx_path)
        if fit_path:
            row["fit_path"] = str(fit_path)

        cols   = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        self._conn.execute(
            f"INSERT OR IGNORE INTO activities ({cols}) VALUES ({placeholders})", row
        )
        self._conn.commit()
        logger.debug("Activity %s saved.", row["garmin_activity_id"])
        return row

    def mark_caldav_pushed(self, user_id: int, garmin_activity_id: str) -> None:
        self._conn.execute(
            "UPDATE activities SET caldav_pushed = 1 WHERE user_id = ? AND garmin_activity_id = ?",
            (user_id, garmin_activity_id),
        )
        self._conn.commit()

    def mark_mastodon_posted(
        self, user_id: int, garmin_activity_id: str, status_id: str | None = None
    ) -> None:
        self._conn.execute(
            """
            UPDATE activities
               SET mastodon_posted = 1, mastodon_status_id = COALESCE(?, mastodon_status_id)
             WHERE user_id = ? AND garmin_activity_id = ?
            """,
            (status_id, user_id, garmin_activity_id),
        )
        self._conn.commit()

    # ── Wahoo activity helpers ───────────────────────────────────────────

    def get_wahoo_activity(self, user_id: int, wahoo_id: str) -> dict | None:
        """Look up an activity by its Wahoo workout ID.

        Wahoo IDs are stored in garmin_activity_id (the source-neutral ID
        column) with source='WahooNoStra'.
        """
        row = self._conn.execute(
            "SELECT * FROM activities WHERE user_id = ? AND garmin_activity_id = ? AND source = 'WahooNoStra'",
            (user_id, wahoo_id),
        ).fetchone()
        return dict(row) if row else None

    def save_wahoo_activity(
        self, user_id: int, mapped_row: dict, gpx_path: Path | None = None, fit_path: Path | None = None
    ) -> dict:
        """Insert a Wahoo activity into DB (ignore if already exists).

        *mapped_row* must be the output of wahoo.map_wahoo_activity().
        """
        if gpx_path:
            mapped_row["gpx_path"] = str(gpx_path)
        if fit_path:
            mapped_row["fit_path"] = str(fit_path)
        mapped_row["wahoo_activity_id"] = mapped_row["garmin_activity_id"]

        cols = ", ".join(mapped_row.keys())
        placeholders = ", ".join(f":{k}" for k in mapped_row.keys())
        self._conn.execute(
            f"INSERT OR IGNORE INTO activities ({cols}) VALUES ({placeholders})", mapped_row
        )
        self._conn.commit()
        logger.debug("Wahoo activity %s saved.", mapped_row["garmin_activity_id"])
        return mapped_row

    def mark_wahoo_synced_to_garmin(self, user_id: int, wahoo_id: str) -> None:
        """Mark a Wahoo-sourced activity as successfully uploaded to Garmin."""
        self._conn.execute(
            "UPDATE activities SET wahoo_synced_to_garmin = 1 WHERE user_id = ? AND garmin_activity_id = ? AND source = 'WahooNoStra'",
            (user_id, wahoo_id),
        )
        self._conn.commit()

    # ── Kudos tracking ───────────────────────────────────────────────────────

    def get_activities_for_kudos(
        self, user_id: int, max_age_days: int | None = None
    ) -> list[dict]:
        """Return posted activities that have a mastodon_status_id, optionally age-limited."""
        query = (
            "SELECT garmin_activity_id, mastodon_status_id, start_time_utc"
            " FROM activities"
            " WHERE user_id = ? AND mastodon_posted = 1 AND mastodon_status_id IS NOT NULL"
        )
        params: list = [user_id]
        if max_age_days is not None:
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            query += " AND start_time_utc >= ?"
            params.append(cutoff)
        rows = self._conn.execute(query, params).fetchall()
        if not rows:
            # Help diagnose why: how many are posted but lack a status_id?
            total_posted = self._conn.execute(
                "SELECT COUNT(*) FROM activities WHERE user_id = ? AND mastodon_posted = 1",
                (user_id,),
            ).fetchone()[0]
            missing_id = self._conn.execute(
                "SELECT COUNT(*) FROM activities WHERE user_id = ? AND mastodon_posted = 1 AND mastodon_status_id IS NULL",
                (user_id,),
            ).fetchone()[0]
            logger.debug(
                "get_activities_for_kudos: 0 eligible for user_id=%d "
                "(total mastodon_posted=%d, missing status_id=%d).",
                user_id, total_posted, missing_id,
            )
        return [dict(r) for r in rows]

    def is_kudos_sent(self, status_id: str, account_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM kudos_sent WHERE status_id = ? AND account_id = ?",
            (status_id, account_id),
        ).fetchone()
        return row is not None

    def mark_kudos_sent(self, status_id: str, account_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO kudos_sent (status_id, account_id, sent_at) VALUES (?, ?, ?)",
            (status_id, account_id, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def get_last_sync_time(self, user_id: int) -> datetime:
        """Return start_time_utc of the most recent activity for *user_id*, or epoch."""
        row = self._conn.execute(
            "SELECT start_time_utc FROM activities WHERE user_id = ? "
            "ORDER BY start_time_utc DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row and row[0]:
            try:
                return datetime.fromisoformat(
                    row[0].replace(" ", "T")
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return datetime(1970, 1, 1, tzinfo=timezone.utc)

    # ── GPX / map storage ────────────────────────────────────────────────────

    def save_gpx(self, user_name: str, activity_id: str, gpx_data: bytes) -> Path:
        path = self.gpx_dir / user_name / f"{activity_id}.gpx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gpx_data)
        logger.info("GPX saved: %s", path)
        return path

    def save_fit(self, user_name: str, activity_id: str, fit_data: bytes) -> Path:
        path = self.fit_dir / user_name / f"{activity_id}.fit"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fit_data)
        logger.info("FIT saved: %s", path)
        return path

    def map_path(self, user_name: str, activity_id: str) -> Path:
        return self.map_dir / user_name / f"{activity_id}.png"

    # ── Sync audit log ───────────────────────────────────────────────────────

    def start_sync_run(self, user_id: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO sync_runs (user_id, started_at) VALUES (?, ?)",
            (user_id, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def finish_sync_run(
        self,
        run_id: int,
        found: int,
        processed: int,
        status: str,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE sync_runs SET
                completed_at         = ?,
                activities_found     = ?,
                activities_processed = ?,
                status               = ?,
                error_message        = ?
            WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                found, processed, status, error, run_id,
            ),
        )
        self._conn.commit()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()
