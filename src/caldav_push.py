"""Push Garmin activities as VEVENT entries to a CalDAV calendar."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import caldav
from icalendar import Calendar, Event, vText

from format import activity_meta, fmt_duration, fmt_num, fmt_pace, fmt_speed

logger = logging.getLogger(__name__)

_SPEED_TYPES = {"cycling", "mountain_biking", "indoor_cycling", "road_biking"}
_PACE_TYPES  = {"running", "trail_running", "treadmill_running", "hiking", "walking"}


def _build_vevent(activity: dict[str, Any]) -> bytes:
    """Build an iCalendar VEVENT from a storage-schema activity dict."""
    act_type = (activity.get("activity_type") or "workout").lower()
    label, emoji, _ = activity_meta(act_type)
    name = activity.get("activity_name") or label

    start_str = activity.get("start_time_utc") or activity.get("start_time_local", "")
    try:
        dtstart = datetime.fromisoformat(
            start_str.replace(" ", "T")
        ).replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        dtstart = datetime.now(timezone.utc)

    duration_s = float(activity.get("duration_s") or 0)
    distance_m = float(activity.get("distance_m") or 0)
    elev_m     = float(activity.get("elevation_gain_m") or 0)
    avg_hr     = activity.get("avg_hr")
    avg_power  = activity.get("avg_power_w")

    dtend = dtstart + timedelta(seconds=duration_s)

    cal   = Calendar()
    cal.add("prodid", "-//garmin-nostra//DE")
    cal.add("version", "2.0")

    event = Event()
    event.add("uid",    str(uuid.uuid4()))
    event.add("dtstart", dtstart)
    event.add("dtend",   dtend)
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("summary", vText(f"{emoji} {name}"))

    desc_lines = [f"Typ: {label}"]
    if duration_s > 0:
        desc_lines.append(f"Dauer: {fmt_duration(duration_s)}")
    if distance_m > 0:
        desc_lines.append(f"Distanz: {fmt_num(distance_m / 1000.0, 2)} km")
    if act_type in _PACE_TYPES and distance_m > 0 and duration_s > 0:
        desc_lines.append(f"Pace: {fmt_pace(duration_s, distance_m)}")
    elif act_type in _SPEED_TYPES and distance_m > 0 and duration_s > 0:
        desc_lines.append(f"Geschwindigkeit: {fmt_speed(distance_m, duration_s)}")
    if elev_m > 0:
        desc_lines.append(f"Anstieg: {int(elev_m)} m")
    if avg_power:
        desc_lines.append(f"Ø Leistung: {int(avg_power)} W")
    if avg_hr:
        desc_lines.append(f"Ø Herzfrequenz: {int(avg_hr)} bpm")

    event.add("description", vText("\n".join(desc_lines)))
    event.add("categories", [label])

    cal.add_component(event)
    return cal.to_ical()


class CalDAVPusher:
    def __init__(
        self, url: str, username: str, password: str, calendar_name: str, timeout: int = 30
    ) -> None:
        self._url           = url
        self._username      = username
        self._password      = password
        self._calendar_name = calendar_name
        self._timeout       = timeout
        self._calendar: caldav.Calendar | None = None

    def _get_calendar(self) -> caldav.Calendar:
        if self._calendar is not None:
            return self._calendar
        client    = caldav.DAVClient(url=self._url, username=self._username, password=self._password, timeout=self._timeout)
        principal = client.principal()
        calendars = principal.calendars()
        for cal in calendars:
            if cal.name == self._calendar_name:
                self._calendar = cal
                logger.info("CalDAV calendar found: %s", self._calendar_name)
                return self._calendar
        available = [c.name for c in calendars]
        raise ValueError(
            f"Calendar '{self._calendar_name}' not found. "
            f"Available: {available}"
        )

    def push(self, activity: dict[str, Any]) -> None:
        """Push a storage-schema activity dict as a VEVENT.

        On connection errors the cached calendar handle is cleared so the
        next call will reconnect instead of failing repeatedly.
        """
        gid = activity.get("garmin_activity_id", "unknown")
        logger.info("Pushing activity %s to CalDAV …", gid)
        ical_bytes = _build_vevent(activity)
        try:
            self._get_calendar().save_event(ical_bytes.decode())
        except (OSError, ConnectionError, caldav.lib.error.DAVError) as exc:
            self._calendar = None
            logger.warning("CalDAV connection lost, will reconnect on next push: %s", exc)
            raise
        logger.info("CalDAV event saved for activity %s.", gid)


# ── Standalone test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.DEBUG)
    fake = {
        "garmin_activity_id": "99999",
        "activity_name":      "Abendlauf",
        "activity_type":      "running",
        "start_time_utc":     "2025-03-04 17:30:00",
        "duration_s":         3420.0,
        "distance_m":         10050.0,
        "elevation_gain_m":   80.0,
        "avg_hr":             152,
        "avg_power_w":        None,
    }
    print(_build_vevent(fake).decode())
