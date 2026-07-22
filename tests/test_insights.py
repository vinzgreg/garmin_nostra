"""Tests for insights.py -- per-km pace/HR/cadence split computation."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from insights import compute_insights_from_gpx  # noqa: E402


def _gpx(n_points: int, step_m: float = 3.0, start_hr: int = 120, hr_ramp: float = 0.05,
         cadence: int = 90, include_extensions: bool = True) -> bytes:
    """Build a synthetic GPX track heading due north at a small, fixed step.

    Varying latitude (not longitude) keeps the metres-per-degree conversion
    constant regardless of location -- ~1 degree of latitude is ~111320m
    everywhere, unlike longitude, which shrinks by cos(latitude). Steps are
    tiny so n_points * step_m metres accumulate over the track, one point
    per second.
    """
    start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    deg_per_m = 1.0 / 111320.0
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" xmlns:ns3="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
             "<trk><trkseg>"]
    for i in range(n_points):
        lat = 48.1000 + i * step_m * deg_per_m
        t = (start + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        hr = int(start_hr + i * hr_ramp)
        lines.append(f'<trkpt lat="{lat:.8f}" lon="11.6000"><ele>500</ele><time>{t}</time>')
        if include_extensions:
            lines.append(
                f"<extensions><ns3:TrackPointExtension>"
                f"<ns3:hr>{hr}</ns3:hr><ns3:cad>{cadence}</ns3:cad>"
                f"</ns3:TrackPointExtension></extensions>"
            )
        lines.append("</trkpt>")
    lines += ["</trkseg></trk></gpx>"]
    return "\n".join(lines).encode()


def test_short_track_returns_none():
    # 200 points * 3m = 600m, under one full km.
    assert compute_insights_from_gpx(_gpx(200)) is None


def test_full_splits_and_partial():
    # 600 points * 3m = 1800m -> 1 full split + 800m partial.
    result = compute_insights_from_gpx(_gpx(600))
    assert result is not None
    assert result["source_format"] == "gpx"
    splits = result["splits_json"]["splits"]
    assert len(splits) == 1
    # A split ends at the first point reaching/exceeding the boundary, so it
    # slightly overshoots 1000m (by less than one point-to-point step)
    # rather than landing exactly on it.
    assert 1000.0 <= splits[0]["distance_m"] < 1003.0
    assert result["splits_json"]["partial_last_split_m"] > 0


def test_hr_and_cadence_present():
    result = compute_insights_from_gpx(_gpx(700))
    assert result["has_hr"] is True
    assert result["has_cadence"] is True
    assert result["has_power"] is False
    split = result["splits_json"]["splits"][0]
    assert split["avg_hr"] is not None
    assert split["avg_cadence"] == 90


def test_no_sensor_data_degrades_gracefully():
    result = compute_insights_from_gpx(_gpx(700, include_extensions=False))
    assert result is not None
    assert result["has_hr"] is False
    assert result["has_cadence"] is False
    assert result["hr_drift_pct"] is None
    for split in result["splits_json"]["splits"]:
        assert split["avg_hr"] is None
        assert split["avg_cadence"] is None
        assert split["pace_s_per_km"] is not None  # distance/time still work


def test_negative_split_and_hr_drift_need_enough_data():
    # Only 1 full split -- not enough to compute a trend.
    result = compute_insights_from_gpx(_gpx(600))
    assert result["negative_split"] is None
    assert result["hr_drift_pct"] is None


def test_hr_drift_detected_with_enough_splits():
    # ~4200m -> 4 full splits, HR ramping up throughout.
    result = compute_insights_from_gpx(_gpx(1400, hr_ramp=0.1))
    splits = result["splits_json"]["splits"]
    assert len(splits) == 4
    assert result["hr_drift_pct"] is not None
    assert result["hr_drift_pct"] > 0  # HR rose over the activity


def test_malformed_gpx_returns_none():
    assert compute_insights_from_gpx(b"not xml at all") is None


def test_empty_gpx_returns_none():
    empty = '<?xml version="1.0"?><gpx version="1.1"><trk><trkseg></trkseg></trk></gpx>'.encode()
    assert compute_insights_from_gpx(empty) is None
