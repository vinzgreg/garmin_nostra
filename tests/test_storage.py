"""Tests for storage.py — ActivityStore with isolated temp DB."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from storage import ActivityStore, _map_activity


# ── Schema integrity ──────────────────────────────────────────────────────────

def test_store_creates_tables(store):
    conn = store._conn
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert {"users", "activities", "sync_runs", "kudos_sent", "wahoo_skipped"} <= tables


def test_wal_mode_enabled(store):
    row = store._conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_foreign_keys_enabled(store):
    row = store._conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1


# ── User upsert ───────────────────────────────────────────────────────────────

def test_upsert_user_returns_stable_id(store, user_cfg):
    id1 = store.upsert_user(user_cfg)
    id2 = store.upsert_user(user_cfg)
    assert id1 == id2


def test_upsert_user_updates_handle(store, user_cfg):
    store.upsert_user(user_cfg)
    user_cfg["mastodon_handle"] = "@new@example.invalid"
    uid = store.upsert_user(user_cfg)
    row = store._conn.execute(
        "SELECT mastodon_handle FROM users WHERE id = ?", (uid,)
    ).fetchone()
    assert row[0] == "@new@example.invalid"


# ── Garmin activity save / get ────────────────────────────────────────────────

def test_save_and_get_running(store, user_id, garmin_running):
    row = store.save_activity(user_id, garmin_running)
    fetched = store.get_activity(user_id, str(garmin_running["activityId"]))

    assert fetched is not None
    assert fetched["activity_type"] == "running"
    assert fetched["distance_m"] == pytest.approx(9587.0, rel=1e-3)
    assert fetched["avg_hr"] == 160
    assert fetched["max_hr"] == 174
    assert fetched["calories"] == 670
    assert fetched["avg_cadence"] == 190
    assert fetched["max_cadence"] == 201
    assert fetched["source"] == "GarminNoStra"
    assert fetched["caldav_pushed"] == 0
    assert fetched["mastodon_posted"] == 0


def test_save_and_get_outdoor_cycling(store, user_id, garmin_cycling):
    store.save_activity(user_id, garmin_cycling)
    fetched = store.get_activity(user_id, str(garmin_cycling["activityId"]))

    assert fetched is not None
    assert fetched["activity_type"] == "cycling"
    assert fetched["distance_m"] == pytest.approx(50531.0, rel=1e-3)
    assert fetched["avg_hr"] == 103
    assert fetched["avg_cadence"] == 85


def test_save_indoor_cycling_power_from_avgPower(store, user_id, garmin_indoor_cycling):
    """Garmin activity-list API returns power as 'avgPower'. The mapping reads
    both 'averagePower' and 'avgPower' so power is stored on the initial save."""
    store.save_activity(user_id, garmin_indoor_cycling)
    fetched = store.get_activity(user_id, str(garmin_indoor_cycling["activityId"]))

    assert fetched is not None
    assert fetched["activity_type"] == "indoor_cycling"
    assert fetched["avg_power_w"] == pytest.approx(222.0)
    assert fetched["max_power_w"] == pytest.approx(380.0)
    assert fetched["avg_cadence"] == 88
    assert fetched["max_cadence"] == 105


def test_save_indoor_cycling_prefers_averagePower_over_avgPower(store, user_id, garmin_indoor_cycling):
    """If both 'averagePower' and 'avgPower' are present, 'averagePower' wins
    (it's checked first in the or-chain)."""
    act = dict(garmin_indoor_cycling)
    act["averagePower"] = 200.0  # takes precedence
    act["avgPower"] = 222.0      # fallback
    store.save_activity(user_id, act)
    fetched = store.get_activity(user_id, str(act["activityId"]))

    assert fetched["avg_power_w"] == pytest.approx(200.0)


# ── Backfill ──────────────────────────────────────────────────────────────────

def test_backfill_fills_null_power(store, user_id, garmin_indoor_cycling):
    """After initial save without power, a backfill call fills it.
    Remove avgPower/maxPower/normPower from the fixture to simulate Garmin
    not having computed power yet at first sync."""
    act = dict(garmin_indoor_cycling)
    del act["avgPower"]
    del act["maxPower"]
    del act["normPower"]
    store.save_activity(user_id, act)
    gid = str(act["activityId"])
    assert store.get_activity(user_id, gid)["avg_power_w"] is None

    # Simulate Garmin returning power on the next sync
    store.backfill_activity_metrics(user_id, gid, {"avgPower": 222.0, "maxPower": 380.0})

    fetched = store.get_activity(user_id, gid)
    assert fetched["avg_power_w"] == pytest.approx(222.0)
    assert fetched["max_power_w"] == pytest.approx(380.0)


def test_backfill_does_not_overwrite_existing_value(store, user_id, garmin_indoor_cycling):
    """backfill_activity_metrics never overwrites a column that already has a value."""
    store.save_activity(user_id, garmin_indoor_cycling)
    gid = str(garmin_indoor_cycling["activityId"])
    # avgPower=222 in fixture → stored
    assert store.get_activity(user_id, gid)["avg_power_w"] == pytest.approx(222.0)

    # Attempt backfill with a different value — must not overwrite
    store.backfill_activity_metrics(user_id, gid, {"avgPower": 999.0})
    assert store.get_activity(user_id, gid)["avg_power_w"] == pytest.approx(222.0)


def test_backfill_fills_hr_alongside_power(store, user_id, garmin_indoor_cycling):
    """backfill_activity_metrics fills HR columns too."""
    act = dict(garmin_indoor_cycling)
    del act["averageHR"]
    del act["avgPower"]
    store.save_activity(user_id, act)
    gid = str(act["activityId"])

    store.backfill_activity_metrics(user_id, gid, {"avgPower": 222.0, "averageHR": 138})
    fetched = store.get_activity(user_id, gid)
    assert fetched["avg_hr"] == 138
    assert fetched["avg_power_w"] == pytest.approx(222.0)


# ── Deduplication — no double-insert ─────────────────────────────────────────

def test_duplicate_garmin_activity_is_ignored(store, user_id, garmin_running):
    store.save_activity(user_id, garmin_running)
    store.save_activity(user_id, garmin_running)  # second save — must be a no-op
    count = store._conn.execute(
        "SELECT COUNT(*) FROM activities WHERE user_id = ? AND garmin_activity_id = ?",
        (user_id, str(garmin_running["activityId"])),
    ).fetchone()[0]
    assert count == 1


def test_get_activity_returns_none_for_unknown(store, user_id):
    assert store.get_activity(user_id, "99999999999") is None


# ── Wahoo activity save / get ─────────────────────────────────────────────────

def test_save_and_get_wahoo_cycling(store, user_id, wahoo_workout_cycling, wahoo_summary_cycling):
    from wahoo import map_wahoo_activity
    row = map_wahoo_activity(user_id, wahoo_workout_cycling, wahoo_summary_cycling)
    store.save_wahoo_activity(user_id, row)

    wahoo_id = str(wahoo_workout_cycling["id"])
    fetched = store.get_wahoo_activity(user_id, wahoo_id)

    assert fetched is not None
    assert fetched["source"] == "WahooNoStra"
    assert fetched["activity_type"] == "cycling"
    assert fetched["distance_m"] == pytest.approx(34750.5, rel=1e-3)
    assert fetched["avg_hr"] == 145
    assert fetched["caldav_pushed"] == 0
    assert fetched["mastodon_posted"] == 0


def test_save_and_get_wahoo_indoor_cycling(store, user_id, wahoo_workout_indoor, wahoo_summary_indoor):
    from wahoo import map_wahoo_activity
    row = map_wahoo_activity(user_id, wahoo_workout_indoor, wahoo_summary_indoor)
    store.save_wahoo_activity(user_id, row)

    wahoo_id = str(wahoo_workout_indoor["id"])
    fetched = store.get_wahoo_activity(user_id, wahoo_id)

    assert fetched is not None
    assert fetched["activity_type"] == "indoor_cycling"
    assert fetched["avg_power_w"] == pytest.approx(139.0)
    assert fetched["normalized_power_w"] == pytest.approx(146.0)
    assert fetched["avg_hr"] == 105
    assert fetched["avg_cadence"] == 81


def test_duplicate_wahoo_activity_is_ignored(store, user_id, wahoo_workout_cycling, wahoo_summary_cycling):
    from wahoo import map_wahoo_activity
    row = map_wahoo_activity(user_id, wahoo_workout_cycling, wahoo_summary_cycling)
    store.save_wahoo_activity(user_id, dict(row))
    store.save_wahoo_activity(user_id, dict(row))  # second save — must be a no-op
    count = store._conn.execute(
        "SELECT COUNT(*) FROM activities WHERE user_id = ? AND garmin_activity_id = ?",
        (user_id, str(wahoo_workout_cycling["id"])),
    ).fetchone()[0]
    assert count == 1


# ── Cross-source dedup (Wahoo/Garmin overlap) ─────────────────────────────────

def _make_garmin_act(activity_id: str, start_utc: str, duration_s: float, type_key: str = "cycling") -> dict:
    return {
        "activityId": activity_id,
        "activityName": "Test Ride",
        "startTimeGMT": start_utc,
        "startTimeLocal": start_utc,
        "activityType": {"typeKey": type_key},
        "duration": duration_s,
        "distance": 30000.0,
        "averageHR": 130,
    }


def _make_wahoo_row(user_id: int, wahoo_id: str, start_utc: str, duration_s: float) -> dict:
    return {
        "user_id": user_id,
        "garmin_activity_id": wahoo_id,
        "activity_name": "[Wahoo] Test Ride",
        "activity_type": "cycling",
        "sport_type": "cycling",
        "start_time_utc": start_utc,
        "start_time_local": start_utc,
        "timezone": "Europe/Berlin",
        "duration_s": duration_s,
        "elapsed_time_s": duration_s,
        "moving_time_s": duration_s,
        "distance_m": 30000.0,
        "elevation_gain_m": None, "elevation_loss_m": None,
        "min_elevation_m": None, "max_elevation_m": None,
        "avg_speed_ms": None, "max_speed_ms": None,
        "avg_hr": 130, "max_hr": None, "resting_hr": None,
        "avg_power_w": None, "max_power_w": None, "normalized_power_w": None,
        "avg_cadence": None, "max_cadence": None,
        "avg_stride_length_m": None, "avg_vertical_osc_cm": None,
        "avg_ground_contact_ms": None, "aerobic_training_effect": None,
        "training_stress_score": None, "vo2max_estimate": None,
        "intensity_factor": None, "calories": None, "steps": None,
        "avg_temperature_c": None, "max_temperature_c": None,
        "start_lat": None, "start_lon": None,
        "raw_json": "{}",
        "gpx_path": None, "fit_path": None,
        "source": "WahooNoStra",
        "caldav_pushed": 0,
        "mastodon_posted": 0,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def test_garmin_activity_suppressed_when_wahoo_overlap_exists(store, user_id):
    """A Garmin activity that overlaps an existing Wahoo activity must be suppressed."""
    start = "2023-06-25 08:00:00"
    duration = 3600.0

    wahoo_row = _make_wahoo_row(user_id, "W001", start, duration)
    store.save_wahoo_activity(user_id, wahoo_row)

    garmin_act = _make_garmin_act("G001", start, duration)
    saved = store.save_activity(user_id, garmin_act)

    assert saved["suppressed"] == "wahoo_garmin_duplicate"
    fetched = store.get_activity(user_id, "G001")
    assert fetched["suppressed"] == "wahoo_garmin_duplicate"


def test_garmin_activity_not_suppressed_without_wahoo_overlap(store, user_id):
    start_wahoo = "2023-06-25 08:00:00"
    start_garmin = "2023-06-25 10:00:00"  # 2 hours later — no overlap
    duration = 3600.0

    wahoo_row = _make_wahoo_row(user_id, "W002", start_wahoo, duration)
    store.save_wahoo_activity(user_id, wahoo_row)

    garmin_act = _make_garmin_act("G002", start_garmin, duration)
    saved = store.save_activity(user_id, garmin_act)

    assert saved.get("suppressed") is None


def test_wahoo_save_suppresses_existing_garmin_overlap(store, user_id):
    """Saving a Wahoo activity retroactively suppresses an already-stored Garmin activity
    if their time windows overlap."""
    start = "2023-06-25 08:00:00"
    duration = 3600.0

    # Garmin arrives first
    garmin_act = _make_garmin_act("G003", start, duration)
    store.save_activity(user_id, garmin_act)
    assert store.get_activity(user_id, "G003")["suppressed"] is None

    # Wahoo arrives later — should retroactively suppress G003
    wahoo_row = _make_wahoo_row(user_id, "W003", start, duration)
    store.save_wahoo_activity(user_id, wahoo_row)

    assert store.get_activity(user_id, "G003")["suppressed"] == "wahoo_garmin_duplicate"


def test_suppression_does_not_affect_different_users(store):
    """Suppression is scoped per user — a Wahoo activity for user A must not
    suppress a Garmin activity for user B."""
    cfg_a = {"name": "user_a", "garmin_username": "a@example.invalid"}
    cfg_b = {"name": "user_b", "garmin_username": "b@example.invalid"}
    uid_a = store.upsert_user(cfg_a)
    uid_b = store.upsert_user(cfg_b)

    start = "2023-06-25 08:00:00"
    duration = 3600.0

    wahoo_row = _make_wahoo_row(uid_a, "W_A", start, duration)
    store.save_wahoo_activity(uid_a, wahoo_row)

    garmin_act = _make_garmin_act("G_B", start, duration)
    saved = store.save_activity(uid_b, garmin_act)

    assert saved.get("suppressed") is None


# ── get_activity_near_time ────────────────────────────────────────────────────

def test_get_activity_near_time_within_window(store, user_id, garmin_running):
    store.save_activity(user_id, garmin_running)
    # Same time → within 120s window
    result = store.get_activity_near_time(
        user_id, "2026-03-04 06:28:23", window_s=120
    )
    assert result is not None


def test_get_activity_near_time_outside_window(store, user_id, garmin_running):
    store.save_activity(user_id, garmin_running)
    # 200s after start — outside 120s window
    result = store.get_activity_near_time(
        user_id, "2026-03-04 06:31:43", window_s=120
    )
    assert result is None


def test_get_activity_near_time_source_filter(store, user_id, wahoo_workout_cycling, wahoo_summary_cycling):
    from wahoo import map_wahoo_activity
    row = map_wahoo_activity(user_id, wahoo_workout_cycling, wahoo_summary_cycling)
    store.save_wahoo_activity(user_id, row)

    # Searching only for GarminNoStra — should not find the Wahoo activity
    result = store.get_activity_near_time(
        user_id, "2023-06-26 06:40:00", window_s=120, source="GarminNoStra"
    )
    assert result is None

    # Searching for WahooNoStra — should find it
    result = store.get_activity_near_time(
        user_id, "2023-06-26 06:40:00", window_s=120, source="WahooNoStra"
    )
    assert result is not None


# ── Mark helpers ──────────────────────────────────────────────────────────────

def test_mark_mastodon_posted(store, user_id, garmin_running):
    store.save_activity(user_id, garmin_running)
    gid = str(garmin_running["activityId"])

    store.mark_mastodon_posted(user_id, gid, status_id="STATUS123")
    fetched = store.get_activity(user_id, gid)
    assert fetched["mastodon_posted"] == 1
    assert fetched["mastodon_status_id"] == "STATUS123"


def test_mark_mastodon_posted_does_not_overwrite_status_id(store, user_id, garmin_running):
    store.save_activity(user_id, garmin_running)
    gid = str(garmin_running["activityId"])

    store.mark_mastodon_posted(user_id, gid, status_id="FIRST")
    store.mark_mastodon_posted(user_id, gid, status_id=None)  # called again without id
    assert store.get_activity(user_id, gid)["mastodon_status_id"] == "FIRST"


def test_mark_caldav_pushed(store, user_id, garmin_running):
    store.save_activity(user_id, garmin_running)
    gid = str(garmin_running["activityId"])
    store.mark_caldav_pushed(user_id, gid)
    assert store.get_activity(user_id, gid)["caldav_pushed"] == 1


# ── Sync run audit log ────────────────────────────────────────────────────────

def test_sync_run_lifecycle(store, user_id):
    run_id = store.start_sync_run(user_id)
    assert run_id is not None

    store.finish_sync_run(run_id, found=3, processed=2, status="success")
    row = store._conn.execute(
        "SELECT * FROM sync_runs WHERE id = ?", (run_id,)
    ).fetchone()

    assert row["status"] == "success"
    assert row["activities_found"] == 3
    assert row["activities_processed"] == 2
    assert row["completed_at"] is not None


def test_sync_run_records_error(store, user_id):
    run_id = store.start_sync_run(user_id)
    store.finish_sync_run(run_id, 0, 0, "failed", "timeout")
    row = store._conn.execute(
        "SELECT error_message FROM sync_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["error_message"] == "timeout"


# ── Wahoo skipped ─────────────────────────────────────────────────────────────

def test_mark_and_check_wahoo_skipped(store, user_id):
    assert not store.is_wahoo_skipped(user_id, "W999")
    store.mark_wahoo_skipped(user_id, "W999", "401 Unauthorized")
    assert store.is_wahoo_skipped(user_id, "W999")


def test_wahoo_skipped_is_per_user(store):
    cfg_a = {"name": "user_a", "garmin_username": "a@example.invalid"}
    cfg_b = {"name": "user_b", "garmin_username": "b@example.invalid"}
    uid_a = store.upsert_user(cfg_a)
    uid_b = store.upsert_user(cfg_b)

    store.mark_wahoo_skipped(uid_a, "W999", "401")
    assert not store.is_wahoo_skipped(uid_b, "W999")


# ── get_last_sync_time ────────────────────────────────────────────────────────

def test_get_last_sync_time_returns_epoch_when_empty(store, user_id):
    t = store.get_last_sync_time(user_id)
    assert t == datetime(1970, 1, 1, tzinfo=timezone.utc)


def test_get_last_sync_time_returns_most_recent(store, user_id, garmin_running, garmin_cycling):
    store.save_activity(user_id, garmin_running)   # 2026-03-04
    store.save_activity(user_id, garmin_cycling)   # 2023-06-25 — older
    t = store.get_last_sync_time(user_id)
    assert t.year == 2026
