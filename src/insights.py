"""Compute per-kilometre pace/heart-rate/cadence splits for an activity.

Used by nostra-mcp's get_activity_insights tool to answer "how did each km
go" without that server ever reading a raw GPX/FIT file itself: computed
here, once, at ingest time, and stored in the activity_insights table.

Two entry points, because the two source formats carry different data:

* Garmin's native GPX embeds heart-rate/cadence per point via a vendor
  TrackPointExtension (element local names "hr"/"cad", matched by local
  name rather than namespace URI -- the schema URI varies by GPS-device
  vendor/exporter even though the element names are conventional).
* Wahoo activities (and old FIT-only Garmin ones) have no native GPX, and
  map_render.fit_to_gpx() -- built for map/elevation rendering -- only
  carries lat/lon/ele/time through, silently dropping heart-rate/cadence/
  power. So those are parsed directly from FIT `record` messages instead,
  never via fit_to_gpx().

Every metric is independently optional: a device with no paired HR/cadence/
power sensor simply produces null values on every split, never an error.
Only a track under one full kilometre returns None -- there is nothing to
split.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import fitparse
import gpxpy

logger = logging.getLogger(__name__)

SPLIT_DISTANCE_M = 1000.0

# Bump when the computation logic changes shape (e.g. a new derived field,
# a different split boundary rule) so a re-run of scripts/backfill_insights.py
# can force reprocessing of already-computed rows instead of skipping them.
SCHEMA_VERSION = 1

# Minimum number of full splits before negative_split/hr_drift_pct are
# considered meaningful rather than noise from too little data.
_MIN_SPLITS_FOR_TREND = 2


def _safe_number(text, cast):
    try:
        return cast(text)
    except (TypeError, ValueError):
        return None


def _extract_gpx_points(gpx_bytes: bytes) -> list[dict] | None:
    try:
        gpx = gpxpy.parse(gpx_bytes.decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.warning("Insights: GPX parse error: %s", exc)
        return None

    points = []
    for pd in gpx.get_points_data():
        pt = pd.point
        hr = cadence = power = None
        for ext in pt.extensions:
            for child in ext:
                tag = child.tag.rsplit("}", 1)[-1].lower()
                if tag == "hr":
                    hr = _safe_number(child.text, int)
                elif tag == "cad":
                    cadence = _safe_number(child.text, int)
                elif tag == "power":
                    power = _safe_number(child.text, float)
        points.append({
            "distance_m": pd.distance_from_start,
            "time": pt.time,
            "elevation": pt.elevation,
            "hr": hr, "cadence": cadence, "power": power,
        })
    return points


def _extract_fit_points(fit_bytes: bytes) -> list[dict] | None:
    try:
        fit = fitparse.FitFile(io.BytesIO(fit_bytes))
        records = list(fit.get_messages("record"))
    except Exception as exc:
        logger.warning("Insights: FIT parse error: %s", exc)
        return None

    points = []
    for record in records:
        data = {f.name: f.value for f in record}
        dist = data.get("distance")
        if dist is None:
            continue
        ts = data.get("timestamp")
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ele = data.get("altitude")
        if ele is None:
            ele = data.get("enhanced_altitude")
        points.append({
            "distance_m": float(dist),
            "time": ts,
            "elevation": ele,
            "hr": data.get("heart_rate"),
            "cadence": data.get("cadence"),
            "power": data.get("power"),
        })
    return points


def _trend_flags(splits: list[dict]) -> tuple[bool | None, float | None]:
    """(negative_split, hr_drift_pct) from full splits, or (None, None)."""
    if len(splits) < _MIN_SPLITS_FOR_TREND:
        return None, None

    mid = len(splits) // 2
    first_half, second_half = splits[:mid], splits[mid:]

    negative_split = None
    first_paces = [s["pace_s_per_km"] for s in first_half if s["pace_s_per_km"] is not None]
    second_paces = [s["pace_s_per_km"] for s in second_half if s["pace_s_per_km"] is not None]
    if first_paces and second_paces:
        negative_split = (sum(second_paces) / len(second_paces)) < (sum(first_paces) / len(first_paces))

    hr_drift_pct = None
    first_hrs = [s["avg_hr"] for s in first_half if s["avg_hr"] is not None]
    second_hrs = [s["avg_hr"] for s in second_half if s["avg_hr"] is not None]
    if first_hrs and second_hrs:
        first_avg = sum(first_hrs) / len(first_hrs)
        second_avg = sum(second_hrs) / len(second_hrs)
        if first_avg > 0:
            hr_drift_pct = round((second_avg - first_avg) / first_avg * 100, 1)

    return negative_split, hr_drift_pct


def _build_result(points: list[dict]) -> dict | None:
    if len(points) < 2:
        return None

    total_distance = points[-1]["distance_m"]
    n_full_splits = int(total_distance // SPLIT_DISTANCE_M)
    if n_full_splits < 1:
        logger.debug("Insights: track under 1km (%.0fm) -- nothing to split.", total_distance)
        return None

    splits: list[dict] = []
    has_hr = has_cadence = has_power = False
    start_idx = 0

    for split_i in range(1, n_full_splits + 1):
        boundary = split_i * SPLIT_DISTANCE_M
        end_idx = start_idx
        while end_idx < len(points) - 1 and points[end_idx]["distance_m"] < boundary:
            end_idx += 1
        chunk = points[start_idx:end_idx + 1]
        if len(chunk) < 2:
            break  # not enough point resolution to close out another split

        start, end = chunk[0], chunk[-1]
        duration_s = (
            (end["time"] - start["time"]).total_seconds()
            if start["time"] and end["time"] else None
        )
        # A non-positive split time (identical timestamps, or clock skew across
        # a device pause) is not usable — treat it as missing rather than
        # emitting a zero/negative pace. `if duration_s` alone would wrongly
        # drop a legitimate 0.0 and let a negative through.
        valid_duration = duration_s is not None and duration_s > 0
        distance_this_split = end["distance_m"] - start["distance_m"]
        hrs = [p["hr"] for p in chunk if p["hr"] is not None]
        cads = [p["cadence"] for p in chunk if p["cadence"] is not None]
        pows = [p["power"] for p in chunk if p["power"] is not None]
        eles = [p["elevation"] for p in chunk if p["elevation"] is not None]
        has_hr = has_hr or bool(hrs)
        has_cadence = has_cadence or bool(cads)
        has_power = has_power or bool(pows)

        splits.append({
            "index": len(splits) + 1,
            "distance_m": round(distance_this_split, 1),
            "duration_s": round(duration_s, 1) if valid_duration else None,
            "pace_s_per_km": (
                round(duration_s * 1000.0 / distance_this_split, 1)
                if valid_duration and distance_this_split > 0 else None
            ),
            "avg_hr": round(sum(hrs) / len(hrs)) if hrs else None,
            "avg_cadence": round(sum(cads) / len(cads)) if cads else None,
            "avg_power_w": round(sum(pows) / len(pows), 1) if pows else None,
            "elev_gain_m": (
                round(sum(max(0.0, b - a) for a, b in zip(eles, eles[1:])), 1)
                if len(eles) >= 2 else None
            ),
        })
        start_idx = end_idx

    if not splits:
        return None

    negative_split, hr_drift_pct = _trend_flags(splits)

    return {
        "splits_json": {
            "unit": "km",
            "splits": splits,
            "partial_last_split_m": round(total_distance - len(splits) * SPLIT_DISTANCE_M, 1),
        },
        "negative_split": negative_split,
        "hr_drift_pct": hr_drift_pct,
        "has_hr": has_hr,
        "has_cadence": has_cadence,
        "has_power": has_power,
        "point_count": len(points),
    }


def compute_insights_from_gpx(gpx_bytes: bytes) -> dict | None:
    """Return an insights result from a GPX track, or None if unusable."""
    points = _extract_gpx_points(gpx_bytes)
    if not points:
        return None
    result = _build_result(points)
    if result is not None:
        result["source_format"] = "gpx"
    return result


def compute_insights_from_fit(fit_bytes: bytes) -> dict | None:
    """Return an insights result from FIT record messages, or None if unusable."""
    points = _extract_fit_points(fit_bytes)
    if not points:
        return None
    result = _build_result(points)
    if result is not None:
        result["source_format"] = "fit"
    return result
