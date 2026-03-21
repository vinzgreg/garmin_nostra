"""German-language formatting helpers for garmin-nostra activity messages."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# ── Localisation tables ──────────────────────────────────────────────────────

MONTHS_DE = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]

WEEKDAYS_SHORT_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

# (German label, emoji, hashtag)
ACTIVITY_TYPES: dict[str, tuple[str, str, str]] = {
    "running":                  ("Laufen",                  "🏃",  "#Laufen"),
    "trail_running":            ("Trailrunning",             "🏔️", "#Trailrunning"),
    "treadmill_running":        ("Laufband",                 "🏃",  "#Laufen"),
    "cycling":                  ("Radfahren",               "🚴",  "#Radfahren"),
    "mountain_biking":          ("Mountainbiken",            "🚵",  "#Mountainbike"),
    "indoor_cycling":           ("Radfahren (Indoor)",       "🚴",  "#Radfahren"),
    "road_biking":              ("Rennrad",                  "🚴",  "#Radfahren"),
    "gravel_cycling":           ("Gravel",                   "🚴",  "#Radfahren"),
    "e_bike_fitness":           ("E-Bike",                   "🚴",  "#Radfahren"),
    "fahrrad":                  ("Radfahren",               "🚴",  "#Radfahren"),
    "radfahrt":                 ("Radfahrt",                "🚴",  "#Radfahren"),
    "virtual_ride":             ("Virtuelle Ausfahrt",       "🚴",  "#Radfahren"),
    "virtuelle radfahrt":       ("Virtuelle Radfahrt",       "🚴",  "#Radfahren"),
    "swimming":                 ("Schwimmen",               "🏊",  "#Schwimmen"),
    "open_water_swimming":      ("Freiwasserschwimmen",      "🏊",  "#Schwimmen"),
    "lap_swimming":             ("Bahnschwimmen",            "🏊",  "#Schwimmen"),
    "hiking":                   ("Wandern",                 "🥾",  "#Wandern"),
    "walking":                  ("Gehen",                   "🚶",  "#Gehen"),
    "strength_training":        ("Krafttraining",           "🏋️", "#Krafttraining"),
    "yoga":                     ("Yoga",                    "🧘",  "#Yoga"),
    "rowing":                   ("Rudern",                  "🚣",  "#Rudern"),
    "indoor_rowing":            ("Rudern (Indoor)",          "🚣",  "#Rudern"),
    "elliptical":               ("Ellipsentrainer",          "🏃",  "#Fitness"),
    "cross_country_skiing":     ("Skilanglauf",              "⛷️", "#Skilanglauf"),
    "skiing":                   ("Skifahren",               "⛷️", "#Skifahren"),
    "snowboarding":             ("Snowboarden",              "🏂",  "#Snowboard"),
    "stand_up_paddleboarding":  ("Stand-Up-Paddling",        "🏄",  "#SUP"),
    "tennis":                   ("Tennis",                  "🎾",  "#Tennis"),
    "golf":                     ("Golf",                    "⛳",  "#Golf"),
    "workout":                  ("Training",                "💪",  "#Fitness"),
}

_SPEED_TYPES = {
    "cycling", "mountain_biking", "indoor_cycling", "road_biking",
    "gravel_cycling", "e_bike_fitness", "fahrrad", "radfahrt",
    "virtual_ride", "virtuelle radfahrt", "stand_up_paddleboarding",
}


def _is_speed_type(activity_type: str) -> bool:
    return activity_type in _SPEED_TYPES or "cycling" in activity_type or "biking" in activity_type
_PACE_TYPES = {
    "running", "trail_running", "treadmill_running",
    "hiking", "walking",
}


def activity_meta(activity_type: str) -> tuple[str, str, str]:
    """Return (German label, emoji, hashtag) for an activity type key."""
    key = activity_type.lower().strip()
    if key in ACTIVITY_TYPES:
        return ACTIVITY_TYPES[key]
    label = key.replace("_", " ").title()
    return label, "🏅", f"#{label.replace(' ', '')}"


# ── Number / time formatting ─────────────────────────────────────────────────

def fmt_num(value: float, decimals: int = 1) -> str:
    """German decimal comma: 8.50 → '8,50'."""
    return f"{value:.{decimals}f}".replace(".", ",")


def fmt_date(dt: datetime) -> str:
    """Di., 04. März 2025"""
    wd = WEEKDAYS_SHORT_DE[dt.weekday()]
    return f"{wd}., {dt.day:02d}. {MONTHS_DE[dt.month]} {dt.year}"


def fmt_time(dt: datetime) -> str:
    """08:32"""
    return dt.strftime("%H:%M")


def fmt_duration(seconds: float) -> str:
    """1:23:45 or 45:32"""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_pace(duration_s: float, distance_m: float) -> str:
    """5:21 min/km"""
    if distance_m <= 0 or duration_s <= 0:
        return "–"
    pace_s = duration_s / (distance_m / 1000.0)
    m, s = divmod(int(pace_s), 60)
    return f"{m}:{s:02d} min/km"


def fmt_speed(distance_m: float, duration_s: float) -> str:
    """31,6 km/h"""
    if duration_s <= 0 or distance_m <= 0:
        return "–"
    kmh = (distance_m / 1000.0) / (duration_s / 3600.0)
    return f"{fmt_num(kmh, 1)} km/h"


# ── Mastodon message builder ─────────────────────────────────────────────────

def build_mastodon_message(handle: str, activity: dict[str, Any]) -> str:
    """
    Build a German-language Mastodon direct-mention message.

    *activity* uses the storage schema column names:
    activity_type, activity_name, start_time_utc, start_time_local,
    duration_s, distance_m, elevation_gain_m, avg_hr, avg_power_w, etc.
    """
    activity_type = (activity.get("activity_type") or "workout").lower()
    label, emoji, hashtag = activity_meta(activity_type)
    name = activity.get("activity_name") or label

    # Parse start time
    start_str = activity.get("start_time_local") or activity.get("start_time_utc", "")
    try:
        dt = datetime.fromisoformat(start_str.replace(" ", "T"))
    except (ValueError, AttributeError):
        dt = datetime.now(timezone.utc)

    duration_s  = float(activity.get("duration_s")       or 0)
    distance_m  = float(activity.get("distance_m")       or 0)
    elev_m      = float(activity.get("elevation_gain_m") or 0)
    avg_hr      = activity.get("avg_hr")
    avg_power   = activity.get("avg_power_w")
    max_power   = activity.get("max_power_w")

    # Extract display name from handle: "@vinz@social.hever.de" → "Vinz"
    local_part = handle.lstrip("@").split("@")[0]
    display_name = local_part.capitalize()

    lines: list[str] = []

    # Header
    lines.append(f"{display_name}, {emoji} {name} – {fmt_date(dt)}, {fmt_time(dt)} Uhr")

    # Primary stats
    primary: list[str] = []
    if duration_s > 0:
        primary.append(f"⏱ {fmt_duration(duration_s)}")
    if distance_m > 0:
        primary.append(f"📏 {fmt_num(distance_m / 1000.0, 2)} km")
    if activity_type in _PACE_TYPES and distance_m > 0 and duration_s > 0:
        primary.append(f"💨 {fmt_pace(duration_s, distance_m)}")
    elif _is_speed_type(activity_type) and distance_m > 0 and duration_s > 0:
        primary.append(f"💨 {fmt_speed(distance_m, duration_s)}")
    if primary:
        lines.append("  ".join(primary))

    # Secondary stats
    secondary: list[str] = []
    if elev_m > 0:
        secondary.append(f"📈 {int(elev_m)} m Anstieg")
    if avg_power and _is_speed_type(activity_type):
        power_str = f"⚡ Ø {int(avg_power)} W"
        if max_power:
            power_str += f" / Max {int(max_power)} W"
        secondary.append(power_str)
    elif avg_power:
        secondary.append(f"⚡ Ø {int(avg_power)} W")
    if avg_hr:
        secondary.append(f"❤️ Ø {int(avg_hr)} bpm")
    if secondary:
        lines.append("  ".join(secondary))

    lines.append("")
    lines.append(f"{hashtag} #GarminNoStra {handle}")

    return "\n".join(lines)
