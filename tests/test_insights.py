"""Tests for insights.py -- per-km pace/HR/cadence split computation."""

from __future__ import annotations

import sys
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from insights import (  # noqa: E402
    compute_insights_from_fit,
    compute_insights_from_gpx,
    _build_result,
)


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


# ── FIT path (Wahoo / FIT-only Garmin) ───────────────────────────────────────
#
# fitparse only decodes, so there is no way to hand-write a real FIT binary in
# a test. Mock fitparse.FitFile to return synthetic `record` messages instead:
# this still exercises insights' own FIT field extraction (the hr/cadence/power
# names it reads, altitude fallback, tz-normalisation), which is the entire
# reason compute_insights_from_fit exists apart from the GPX path.

_Field = namedtuple("_Field", ["name", "value"])


def _fit_records(n_points: int, step_m: float = 3.0, start_hr: int = 120,
                 hr_ramp: float = 0.1, cadence: int = 85, power: int | None = None):
    """Build fake FIT `record` messages, one per second heading a fixed step.

    Each 'record' is just an iterable of _Field(name, value) — matching how
    insights._extract_fit_points consumes them ({f.name: f.value for f in rec}).
    """
    base = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    records = []
    for i in range(n_points):
        fields = [
            _Field("distance", i * step_m),
            _Field("timestamp", base + timedelta(seconds=i)),
            _Field("altitude", 500.0),
            _Field("heart_rate", int(start_hr + i * hr_ramp)),
            _Field("cadence", cadence),
        ]
        if power is not None:
            fields.append(_Field("power", power))
        records.append(fields)
    return records


class _FakeFit:
    def __init__(self, records):
        self._records = records

    def get_messages(self, name):
        assert name == "record"
        return list(self._records)


@patch("insights.fitparse.FitFile")
def test_compute_insights_from_fit_full(MockFitFile):
    MockFitFile.return_value = _FakeFit(_fit_records(700, power=180))
    result = compute_insights_from_fit(b"ignored-bytes")
    assert result is not None
    assert result["source_format"] == "fit"
    assert result["has_hr"] is True
    assert result["has_cadence"] is True
    assert result["has_power"] is True
    split = result["splits_json"]["splits"][0]
    assert split["avg_cadence"] == 85
    assert split["avg_power_w"] == pytest.approx(180.0)
    assert split["pace_s_per_km"] is not None


@patch("insights.fitparse.FitFile")
def test_compute_insights_from_fit_without_power(MockFitFile):
    MockFitFile.return_value = _FakeFit(_fit_records(700, power=None))
    result = compute_insights_from_fit(b"ignored-bytes")
    assert result is not None
    assert result["has_power"] is False
    assert result["has_hr"] is True
    for split in result["splits_json"]["splits"]:
        assert split["avg_power_w"] is None


@patch("insights.fitparse.FitFile")
def test_compute_insights_from_fit_short_track_returns_none(MockFitFile):
    # 200 points * 3m = 600m, under one full km.
    MockFitFile.return_value = _FakeFit(_fit_records(200))
    assert compute_insights_from_fit(b"ignored-bytes") is None


@patch("insights.fitparse.FitFile", side_effect=Exception("corrupt FIT"))
def test_compute_insights_from_fit_parse_error_returns_none(MockFitFile):
    assert compute_insights_from_fit(b"garbage") is None


# ── Non-positive split durations (clock skew / paused device) ────────────────

def _flat_points(n: int, time_fn, step_m: float = 3.0) -> list[dict]:
    return [
        {"distance_m": i * step_m, "time": time_fn(i),
         "elevation": 500.0, "hr": None, "cadence": None, "power": None}
        for i in range(n)
    ]


def test_zero_duration_split_yields_none_pace():
    """Identical timestamps across a split -> duration/pace reported as None,
    not 0.0 (the old `if duration_s` truthiness check dropped a legit 0.0)."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    points = _flat_points(400, lambda i: base)  # every point same time
    result = _build_result(points)
    assert result is not None
    for split in result["splits_json"]["splits"]:
        assert split["duration_s"] is None
        assert split["pace_s_per_km"] is None


def test_negative_duration_split_yields_none_pace():
    """Decreasing timestamps (clock skew) must not produce a negative pace."""
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    points = _flat_points(400, lambda i: base - timedelta(seconds=i))
    result = _build_result(points)
    assert result is not None
    for split in result["splits_json"]["splits"]:
        assert split["pace_s_per_km"] is None
