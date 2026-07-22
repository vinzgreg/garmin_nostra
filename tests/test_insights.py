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
    _PROFILE_GRADE_PCT,
    _ROLLING_TOTAL_CHANGE_M,
)


def _gpx(n_points: int, step_m: float = 3.0, start_hr: int = 120, hr_ramp: float = 0.05,
         cadence: int = 90, include_extensions: bool = True,
         ele_start: float = 500.0, ele_per_point: float = 0.0) -> bytes:
    """Build a synthetic GPX track heading due north at a small, fixed step.

    Varying latitude (not longitude) keeps the metres-per-degree conversion
    constant regardless of location -- ~1 degree of latitude is ~111320m
    everywhere, unlike longitude, which shrinks by cos(latitude). Steps are
    tiny so n_points * step_m metres accumulate over the track, one point
    per second. ``ele_per_point`` ramps elevation by a fixed amount each
    point (positive = climbing) so terrain/grade output can be exercised.
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
        ele = ele_start + i * ele_per_point
        lines.append(f'<trkpt lat="{lat:.8f}" lon="11.6000"><ele>{ele:.2f}</ele><time>{t}</time>')
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


# ── Terrain / grade (v2) ─────────────────────────────────────────────────────

def test_climb_profile_and_grade():
    # 400 pts * 3m = 1200m, +0.15m elevation/point == a steady 5% climb.
    result = compute_insights_from_gpx(_gpx(400, ele_per_point=0.15))
    split = result["splits_json"]["splits"][0]
    assert split["profile"] == "climb"
    assert split["avg_grade_pct"] == pytest.approx(5.0, abs=0.3)
    assert split["max_grade_pct"] == pytest.approx(5.0, abs=0.5)
    assert split["elev_gain_m"] > 45
    assert split["elev_loss_m"] == 0.0
    # GPX gives no reliable elevation provenance.
    assert result["splits_json"]["elev_source"] == "unknown"


def test_descent_profile_and_negative_grade():
    result = compute_insights_from_gpx(_gpx(400, ele_per_point=-0.15))
    split = result["splits_json"]["splits"][0]
    assert split["profile"] == "descent"
    assert split["avg_grade_pct"] == pytest.approx(-5.0, abs=0.3)
    assert split["elev_loss_m"] > 45
    assert split["elev_gain_m"] == 0.0


def test_flat_profile():
    result = compute_insights_from_gpx(_gpx(400))  # constant elevation
    split = result["splits_json"]["splits"][0]
    assert split["profile"] == "flat"
    assert split["avg_grade_pct"] == pytest.approx(0.0, abs=0.1)
    assert split["elev_gain_m"] == 0.0
    assert split["elev_loss_m"] == 0.0


def test_rolling_profile_low_net_high_churn():
    # Elevation climbs then returns: near-zero net grade, but lots of up+down.
    base = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    points = [
        {"distance_m": i * 3.0, "time": base + timedelta(seconds=i),
         "elevation": 500.0 + min(i, 400 - i) * 0.15,
         "hr": None, "cadence": None, "power": None}
        for i in range(400)
    ]
    result = _build_result(points)
    split = result["splits_json"]["splits"][0]
    assert split["profile"] == "rolling"
    assert abs(split["avg_grade_pct"]) < _PROFILE_GRADE_PCT
    assert split["elev_gain_m"] > _ROLLING_TOTAL_CHANGE_M / 2
    assert split["elev_loss_m"] > 0.0


def test_no_elevation_yields_null_terrain():
    base = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    points = [
        {"distance_m": i * 3.0, "time": base + timedelta(seconds=i),
         "elevation": None, "hr": None, "cadence": None, "power": None}
        for i in range(400)
    ]
    split = _build_result(points)["splits_json"]["splits"][0]
    assert split["elev_gain_m"] is None
    assert split["avg_grade_pct"] is None
    assert split["max_grade_pct"] is None
    assert split["profile"] is None


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
                 hr_ramp: float = 0.1, cadence: int = 85, power: int | None = None,
                 enhanced_altitude: bool = False):
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
        if enhanced_altitude:
            fields.append(_Field("enhanced_altitude", 500.0))
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
def test_fit_elev_source_barometric_when_enhanced_present(MockFitFile):
    MockFitFile.return_value = _FakeFit(_fit_records(700, enhanced_altitude=True))
    result = compute_insights_from_fit(b"ignored-bytes")
    assert result["splits_json"]["elev_source"] == "barometric"


@patch("insights.fitparse.FitFile")
def test_fit_elev_source_unknown_without_enhanced(MockFitFile):
    MockFitFile.return_value = _FakeFit(_fit_records(700, enhanced_altitude=False))
    result = compute_insights_from_fit(b"ignored-bytes")
    assert result["splits_json"]["elev_source"] == "unknown"


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
