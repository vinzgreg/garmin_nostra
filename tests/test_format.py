"""Tests for format.py — German-language formatting helpers."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from format import (
    fmt_num,
    fmt_date,
    fmt_time,
    fmt_duration,
    fmt_pace,
    fmt_speed,
    activity_meta,
    build_mastodon_message,
)


# ── fmt_num ───────────────────────────────────────────────────────────────────

def test_fmt_num_decimal_comma():
    assert fmt_num(8.5, 1) == "8,5"


def test_fmt_num_two_decimals():
    assert fmt_num(9.587, 2) == "9,59"


def test_fmt_num_integer():
    assert fmt_num(42.0, 0) == "42"


# ── fmt_date / fmt_time ───────────────────────────────────────────────────────

def test_fmt_date_tuesday_march():
    dt = datetime(2026, 3, 4, 7, 28, tzinfo=timezone.utc)
    assert fmt_date(dt) == "Mi., 04. März 2026"


def test_fmt_date_monday():
    dt = datetime(2025, 1, 6, 0, 0, tzinfo=timezone.utc)
    assert fmt_date(dt) == "Mo., 06. Januar 2025"


def test_fmt_time():
    dt = datetime(2026, 3, 4, 7, 28, tzinfo=timezone.utc)
    assert fmt_time(dt) == "07:28"


# ── fmt_duration ──────────────────────────────────────────────────────────────

def test_fmt_duration_with_hours():
    assert fmt_duration(3661) == "1:01:01"


def test_fmt_duration_under_one_hour():
    assert fmt_duration(305) == "5:05"


def test_fmt_duration_exactly_one_hour():
    assert fmt_duration(3600) == "1:00:00"


def test_fmt_duration_zero():
    assert fmt_duration(0) == "0:00"


# ── fmt_pace ──────────────────────────────────────────────────────────────────

def test_fmt_pace_normal():
    # 5 min/km: 5000m in 1500s → 1500/5 = 300s/km = 5:00
    assert fmt_pace(1500.0, 5000.0) == "5:00 min/km"


def test_fmt_pace_real_run():
    # 9587m in 2979s → 310.7s/km = 5:10 min/km
    result = fmt_pace(2979.0, 9587.0)
    assert result.endswith("min/km")
    mins = int(result.split(":")[0])
    assert 5 <= mins <= 5  # ~5:10


def test_fmt_pace_zero_distance():
    assert fmt_pace(300.0, 0.0) == "–"


def test_fmt_pace_zero_duration():
    assert fmt_pace(0.0, 5000.0) == "–"


# ── fmt_speed ─────────────────────────────────────────────────────────────────

def test_fmt_speed_normal():
    # 36 km/h: 10000m in 1000s
    assert fmt_speed(10000.0, 1000.0) == "36,0 km/h"


def test_fmt_speed_real_ride():
    # 50531m in 8822s ≈ 20.6 km/h
    result = fmt_speed(50531.0, 8822.0)
    assert result.endswith("km/h")
    val = float(result.replace(" km/h", "").replace(",", "."))
    assert 20.0 < val < 21.5


def test_fmt_speed_zero_duration():
    assert fmt_speed(50000.0, 0.0) == "–"


def test_fmt_speed_zero_distance():
    assert fmt_speed(0.0, 3600.0) == "–"


# ── activity_meta ─────────────────────────────────────────────────────────────

def test_activity_meta_running():
    label, emoji, hashtag = activity_meta("running")
    assert label == "Laufen"
    assert "#" in hashtag


def test_activity_meta_indoor_cycling():
    label, emoji, hashtag = activity_meta("indoor_cycling")
    assert "Indoor" in label


def test_activity_meta_unknown_falls_back():
    label, emoji, hashtag = activity_meta("unicycling")
    assert label == "Unicycling"
    assert hashtag == "#Unicycling"
    assert emoji == "🏅"


def test_activity_meta_case_insensitive():
    assert activity_meta("Running") == activity_meta("running")


# ── build_mastodon_message ────────────────────────────────────────────────────

def _activity_row(overrides: dict) -> dict:
    base = {
        "activity_type": "running",
        "activity_name": "Morning Run",
        "start_time_local": "2026-03-04 07:28:23",
        "start_time_utc": "2026-03-04 06:28:23",
        "duration_s": 2979.0,
        "distance_m": 9587.0,
        "elevation_gain_m": 20.0,
        "avg_hr": 160,
        "max_hr": 174,
        "avg_power_w": None,
        "max_power_w": None,
    }
    base.update(overrides)
    return base


def test_message_running_contains_handle(garmin_running):
    row = _activity_row({})
    msg = build_mastodon_message("@testuser@social.example.invalid", row)
    assert "@testuser@social.example.invalid" in msg


def test_message_running_shows_pace(garmin_running):
    row = _activity_row({})
    msg = build_mastodon_message("@testuser@social.example.invalid", row)
    assert "min/km" in msg
    assert "km/h" not in msg


def test_message_running_shows_distance():
    row = _activity_row({})
    msg = build_mastodon_message("@testuser@social.example.invalid", row)
    assert "km" in msg
    assert "9," in msg  # 9.587 km → "9,59 km"


def test_message_running_shows_hr():
    row = _activity_row({"avg_hr": 160})
    msg = build_mastodon_message("@testuser@social.example.invalid", row)
    assert "160" in msg
    assert "bpm" in msg


def test_message_cycling_shows_speed_not_pace():
    row = _activity_row({
        "activity_type": "cycling",
        "activity_name": "Outdoor Ride",
        "distance_m": 50531.0,
        "duration_s": 8822.0,
        "avg_power_w": None,
    })
    msg = build_mastodon_message("@testuser@social.example.invalid", row)
    assert "km/h" in msg
    assert "min/km" not in msg


def test_message_indoor_cycling_shows_power():
    row = _activity_row({
        "activity_type": "indoor_cycling",
        "activity_name": "KICKR Session",
        "distance_m": 22093.0,
        "duration_s": 2985.0,
        "avg_power_w": 139,
        "max_power_w": None,
    })
    msg = build_mastodon_message("@testuser@social.example.invalid", row)
    assert "139" in msg
    assert "W" in msg


def test_message_indoor_cycling_shows_power_and_max():
    row = _activity_row({
        "activity_type": "indoor_cycling",
        "activity_name": "KICKR Session",
        "distance_m": 22093.0,
        "duration_s": 2985.0,
        "avg_power_w": 222,
        "max_power_w": 380,
    })
    msg = build_mastodon_message("@testuser@social.example.invalid", row)
    assert "222" in msg
    assert "380" in msg


def test_message_no_power_no_power_line():
    row = _activity_row({"avg_power_w": None, "max_power_w": None})
    msg = build_mastodon_message("@testuser@social.example.invalid", row)
    assert " W" not in msg


def test_message_contains_hashtag():
    row = _activity_row({})
    msg = build_mastodon_message("@testuser@social.example.invalid", row)
    assert "#Laufen" in msg
    assert "#GarminNoStra" in msg


def test_message_display_name_capitalised():
    row = _activity_row({})
    msg = build_mastodon_message("@testuser@social.example.invalid", row)
    assert msg.startswith("Testuser,")


def test_message_zero_distance_no_pace():
    row = _activity_row({"distance_m": 0, "duration_s": 3600.0})
    msg = build_mastodon_message("@testuser@social.example.invalid", row)
    assert "min/km" not in msg
