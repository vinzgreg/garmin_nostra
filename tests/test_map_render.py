"""Tests for map_render.py — FIT→GPX conversion and elevation/speed profile."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import map_render
from map_render import _SEMICIRCLES_TO_DEG, _smoothed_speeds_kmh, fit_to_gpx, render_elevation_profile


# ── FIT → GPX helpers ──────────────────────────────────────────────────────────

def _deg_to_semicircles(deg: float) -> int:
    return int(deg / _SEMICIRCLES_TO_DEG)


def _record(**fields):
    """A fitparse 'record' is iterable, yielding field objects with name/value."""
    return [SimpleNamespace(name=k, value=v) for k, v in fields.items()]


def _patch_fit(monkeypatch, records):
    class _FakeFit:
        def get_messages(self, kind):
            assert kind == "record"
            return records

    monkeypatch.setattr(map_render.fitparse, "FitFile", lambda *a, **k: _FakeFit())


# ── fit_to_gpx ──────────────────────────────────────────────────────────────────

def test_fit_to_gpx_emits_ele_and_time(monkeypatch):
    records = [
        _record(
            position_lat=_deg_to_semicircles(47.0 + i * 0.001),
            position_long=_deg_to_semicircles(11.0),
            altitude=500.0 + i,
            timestamp=datetime(2026, 3, 3, 8, 0, i, tzinfo=timezone.utc),
        )
        for i in range(3)
    ]
    _patch_fit(monkeypatch, records)

    gpx = fit_to_gpx(b"ignored")
    assert gpx is not None
    text = gpx.decode()
    assert text.count("<trkpt") == 3
    assert "<ele>500.0</ele>" in text
    assert "<time>2026-03-03T08:00:00Z</time>" in text


def test_fit_to_gpx_falls_back_to_enhanced_altitude(monkeypatch):
    records = [
        _record(
            position_lat=_deg_to_semicircles(47.0 + i * 0.001),
            position_long=_deg_to_semicircles(11.0),
            altitude=None,
            enhanced_altitude=600.0 + i,
            timestamp=datetime(2026, 3, 3, 8, 0, i, tzinfo=timezone.utc),
        )
        for i in range(2)
    ]
    _patch_fit(monkeypatch, records)

    gpx = fit_to_gpx(b"ignored").decode()
    assert "<ele>600.0</ele>" in gpx
    assert "<ele>601.0</ele>" in gpx


def test_fit_to_gpx_naive_timestamp_gets_z_suffix(monkeypatch):
    # FIT timestamps from fitparse are naive UTC — must still be Z-suffixed.
    records = [
        _record(
            position_lat=_deg_to_semicircles(47.0 + i * 0.001),
            position_long=_deg_to_semicircles(11.0),
            altitude=500.0,
            timestamp=datetime(2026, 3, 3, 8, 0, i),
        )
        for i in range(2)
    ]
    _patch_fit(monkeypatch, records)

    gpx = fit_to_gpx(b"ignored").decode()
    assert "<time>2026-03-03T08:00:00Z</time>" in gpx


def test_fit_to_gpx_skips_points_without_position(monkeypatch):
    records = [
        _record(altitude=500.0, timestamp=datetime(2026, 3, 3, 8, 0, 0, tzinfo=timezone.utc)),
        _record(
            position_lat=_deg_to_semicircles(47.0),
            position_long=_deg_to_semicircles(11.0),
            altitude=500.0,
            timestamp=datetime(2026, 3, 3, 8, 0, 1, tzinfo=timezone.utc),
        ),
        _record(
            position_lat=_deg_to_semicircles(47.001),
            position_long=_deg_to_semicircles(11.0),
            altitude=501.0,
            timestamp=datetime(2026, 3, 3, 8, 0, 2, tzinfo=timezone.utc),
        ),
    ]
    _patch_fit(monkeypatch, records)

    gpx = fit_to_gpx(b"ignored").decode()
    assert gpx.count("<trkpt") == 2


def test_fit_to_gpx_too_few_points_returns_none(monkeypatch):
    records = [
        _record(
            position_lat=_deg_to_semicircles(47.0),
            position_long=_deg_to_semicircles(11.0),
            altitude=500.0,
        )
    ]
    _patch_fit(monkeypatch, records)
    assert fit_to_gpx(b"ignored") is None


def test_fit_to_gpx_point_without_ele_or_time_is_self_closing(monkeypatch):
    records = [
        _record(position_lat=_deg_to_semicircles(47.0 + i * 0.001), position_long=_deg_to_semicircles(11.0))
        for i in range(2)
    ]
    _patch_fit(monkeypatch, records)

    gpx = fit_to_gpx(b"ignored").decode()
    assert "<trkpt" in gpx
    assert "<ele>" not in gpx
    assert "/>" in gpx  # self-closing when no ele/time


# ── render_elevation_profile ────────────────────────────────────────────────────

def _synthetic_gpx(gain_m: float, *, with_time: bool = True, points: int = 60) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="test">',
        "  <trk><trkseg>",
    ]
    base_ele = 500.0
    for i in range(points):
        lat = 47.0 + i * 0.001
        ele = base_ele + gain_m * (i / (points - 1))
        lines.append(f'    <trkpt lat="{lat:.7f}" lon="11.0000000">')
        lines.append(f"      <ele>{ele:.1f}</ele>")
        if with_time:
            t = datetime(2026, 3, 3, 8, 0, 0, tzinfo=timezone.utc).replace(second=0)
            lines.append(f'      <time>2026-03-03T08:{i // 60:02d}:{i % 60:02d}Z</time>')
        lines.append("    </trkpt>")
    lines += ["  </trkseg></trk>", "</gpx>"]
    return "\n".join(lines).encode()


def test_render_elevation_profile_renders_for_small_gain(tmp_path):
    # A nearly flat ride still renders — the y-axis is forced to a minimum span.
    out = tmp_path / "profile.png"
    result = render_elevation_profile(_synthetic_gpx(gain_m=50.0), out)
    assert result == out
    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_elevation_profile_renders_png(tmp_path):
    out = tmp_path / "profile.png"
    result = render_elevation_profile(_synthetic_gpx(gain_m=200.0), out)
    assert result == out
    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_elevation_profile_works_without_timestamps(tmp_path):
    # No <time> tags → speed cannot be computed, but the profile must still render.
    out = tmp_path / "profile.png"
    result = render_elevation_profile(_synthetic_gpx(gain_m=200.0, with_time=False), out)
    assert result == out
    assert out.exists()


def test_render_elevation_profile_bad_gpx_returns_none(tmp_path):
    out = tmp_path / "profile.png"
    assert render_elevation_profile(b"not xml at all", out) is None
    assert not out.exists()


# ── _smoothed_speeds_kmh ─────────────────────────────────────────────────────────

def test_smoothed_speeds_none_without_timestamps():
    import gpxpy

    gpx = gpxpy.parse(_synthetic_gpx(gain_m=200.0, with_time=False).decode())
    pts = gpx.get_points_data()
    assert _smoothed_speeds_kmh(pts) is None


def test_smoothed_speeds_returns_values_with_timestamps():
    import gpxpy

    gpx = gpxpy.parse(_synthetic_gpx(gain_m=200.0, with_time=True).decode())
    pts = [p for p in gpx.get_points_data() if p.point.elevation is not None]
    speeds = _smoothed_speeds_kmh(pts)
    assert speeds is not None
    assert len(speeds) == len(pts)
    assert all(s >= 0 for s in speeds)
