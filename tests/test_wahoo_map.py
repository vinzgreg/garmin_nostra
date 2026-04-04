"""Tests for wahoo.py — mapping and type-resolution functions."""

from __future__ import annotations

import pytest

from wahoo import (
    map_wahoo_activity,
    wahoo_activity_type,
    _safe_float,
    _safe_int,
)


# ── Type mapping ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("type_id, expected", [
    (0,  "cycling"),
    (1,  "running"),
    (5,  "treadmill_running"),
    (12, "indoor_cycling"),
    (13, "mountain_biking"),
    (15, "road_biking"),
    (61, "indoor_cycling"),
    (42, "strength_training"),
    (66, "yoga"),
])
def test_wahoo_activity_type_known_ids(type_id, expected):
    assert wahoo_activity_type(type_id) == expected


def test_wahoo_activity_type_unknown_falls_back():
    assert wahoo_activity_type(9999) == "workout"


def test_wahoo_activity_type_none_falls_back():
    assert wahoo_activity_type(None) == "workout"


# ── Safe type conversions ─────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    ("139.0",  139.0),
    ("34750.5", 34750.5),
    (222.0,    222.0),
    (None,     None),
    ("",       None),
    ("n/a",    None),
])
def test_safe_float(value, expected):
    assert _safe_float(value) == expected


@pytest.mark.parametrize("value, expected", [
    ("81",  81),
    ("105", 105),
    (82.9,  82),
    (None,  None),
])
def test_safe_int(value, expected):
    assert _safe_int(value) == expected


# ── map_wahoo_activity — outdoor cycling ─────────────────────────────────────

def test_map_wahoo_cycling_schema_keys(wahoo_workout_cycling, wahoo_summary_cycling):
    """Mapped row must contain all columns expected by the storage schema."""
    row = map_wahoo_activity(1, wahoo_workout_cycling, wahoo_summary_cycling)
    required = {
        "user_id", "garmin_activity_id", "activity_name", "activity_type",
        "start_time_utc", "start_time_local", "duration_s", "distance_m",
        "avg_hr", "avg_power_w", "source", "caldav_pushed", "mastodon_posted",
        "raw_json", "synced_at",
    }
    assert required <= set(row.keys())


def test_map_wahoo_cycling_values(wahoo_workout_cycling, wahoo_summary_cycling):
    row = map_wahoo_activity(1, wahoo_workout_cycling, wahoo_summary_cycling)

    assert row["user_id"] == 1
    assert row["garmin_activity_id"] == str(wahoo_workout_cycling["id"])
    assert row["activity_type"] == "cycling"
    assert row["source"] == "WahooNoStra"
    assert row["caldav_pushed"] == 0
    assert row["mastodon_posted"] == 0
    assert row["distance_m"] == pytest.approx(34750.5, rel=1e-3)
    assert row["avg_hr"] == 145
    assert row["avg_power_w"] is None          # no power in the outdoor fixture
    assert row["duration_s"] == pytest.approx(8820.0, rel=1e-3)
    assert row["elevation_gain_m"] == pytest.approx(420.0, rel=1e-3)


def test_map_wahoo_cycling_activity_name_prefix(wahoo_workout_cycling, wahoo_summary_cycling):
    row = map_wahoo_activity(1, wahoo_workout_cycling, wahoo_summary_cycling)
    assert row["activity_name"].startswith("[Wahoo]")


def test_map_wahoo_cycling_timezone(wahoo_workout_cycling, wahoo_summary_cycling):
    row = map_wahoo_activity(1, wahoo_workout_cycling, wahoo_summary_cycling)
    assert row["timezone"] == "Europe/Vienna"
    # Local time must differ from UTC for Central European time
    assert row["start_time_local"] != row["start_time_utc"]


def test_map_wahoo_cycling_utc_start_time(wahoo_workout_cycling, wahoo_summary_cycling):
    row = map_wahoo_activity(1, wahoo_workout_cycling, wahoo_summary_cycling)
    assert row["start_time_utc"] == "2023-06-26 06:40:00"


def test_map_wahoo_cycling_raw_json_contains_both(wahoo_workout_cycling, wahoo_summary_cycling):
    import json
    row = map_wahoo_activity(1, wahoo_workout_cycling, wahoo_summary_cycling)
    raw = json.loads(row["raw_json"])
    assert "workout" in raw
    assert "summary" in raw


# ── map_wahoo_activity — indoor cycling with power ────────────────────────────

def test_map_wahoo_indoor_values(wahoo_workout_indoor, wahoo_summary_indoor):
    row = map_wahoo_activity(1, wahoo_workout_indoor, wahoo_summary_indoor)

    assert row["activity_type"] == "indoor_cycling"
    assert row["avg_power_w"] == pytest.approx(139.0)
    assert row["normalized_power_w"] == pytest.approx(146.0)
    assert row["training_stress_score"] == pytest.approx(55.0)
    assert row["avg_hr"] == 105
    assert row["avg_cadence"] == 81
    assert row["duration_s"] == pytest.approx(2985.0, rel=1e-3)  # active
    assert row["elapsed_time_s"] == pytest.approx(3000.0, rel=1e-3)  # total


def test_map_wahoo_indoor_timezone_berlin(wahoo_workout_indoor, wahoo_summary_indoor):
    row = map_wahoo_activity(1, wahoo_workout_indoor, wahoo_summary_indoor)
    assert row["timezone"] == "Europe/Berlin"
    # UTC start: 2025-07-27T08:41:00Z → local Europe/Berlin (CEST +2) = 10:41
    assert "10:41" in row["start_time_local"]


def test_map_wahoo_indoor_power_fields_present(wahoo_workout_indoor, wahoo_summary_indoor):
    """All power-related fields must be present in the mapped row."""
    row = map_wahoo_activity(1, wahoo_workout_indoor, wahoo_summary_indoor)
    assert row["avg_power_w"] is not None
    assert row["normalized_power_w"] is not None
    assert row["training_stress_score"] is not None


def test_map_wahoo_indoor_no_gps(wahoo_workout_indoor, wahoo_summary_indoor):
    row = map_wahoo_activity(1, wahoo_workout_indoor, wahoo_summary_indoor)
    assert row["start_lat"] is None
    assert row["start_lon"] is None


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_map_wahoo_missing_starts_graceful():
    workout = {"id": "99", "name": "Test", "workout_type_id": 0}
    summary = {}
    row = map_wahoo_activity(1, workout, summary)
    assert row["start_time_utc"] is None
    assert row["start_time_local"] is None


def test_map_wahoo_unknown_timezone_falls_back_to_utc(wahoo_workout_cycling):
    summary = {
        "distance_accum": "1000.0",
        "duration_active_accum": "360.0",
        "duration_total_accum": "360.0",
        "time_zone": "Unknown/Nowhere",
    }
    row = map_wahoo_activity(1, wahoo_workout_cycling, summary)
    # Should not raise; local time falls back to UTC
    assert row["start_time_local"] is not None
    assert row["start_time_utc"] is not None
