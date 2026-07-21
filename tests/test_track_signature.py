"""Tests for track_signature.py -- grid-cell signature computation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from track_signature import compute_track_cells  # noqa: E402


def _gpx(points: list[tuple[float, float]]) -> bytes:
    trkpts = "".join(f'<trkpt lat="{lat}" lon="{lon}"/>' for lat, lon in points)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<gpx version=\"1.1\"><trk><trkseg>" + trkpts + "</trkseg></trk></gpx>"
    ).encode()


_TRACK_A = [(48.1351 + i * 0.0001, 11.5820 + i * 0.0001) for i in range(20)]


def test_deterministic():
    a = compute_track_cells(_gpx(_TRACK_A))
    b = compute_track_cells(_gpx(_TRACK_A))
    assert a == b


def test_identical_tracks_have_jaccard_one():
    cells_a, _ = compute_track_cells(_gpx(_TRACK_A))
    cells_b, _ = compute_track_cells(_gpx(_TRACK_A))
    set_a, set_b = set(cells_a.split(",")), set(cells_b.split(","))
    jaccard = len(set_a & set_b) / len(set_a | set_b)
    assert jaccard == pytest.approx(1.0)


def test_distant_tracks_have_no_overlap():
    track_b = [(48.20 + i * 0.0001, 11.60 + i * 0.0001) for i in range(20)]
    cells_a, _ = compute_track_cells(_gpx(_TRACK_A))
    cells_b, _ = compute_track_cells(_gpx(track_b))
    set_a, set_b = set(cells_a.split(",")), set(cells_b.split(","))
    assert not (set_a & set_b)


def test_empty_gpx_returns_none():
    empty = '<?xml version="1.0"?><gpx version="1.1"><trk><trkseg></trkseg></trk></gpx>'.encode()
    assert compute_track_cells(empty) is None


def test_malformed_gpx_returns_none():
    assert compute_track_cells(b"not xml at all") is None


def test_point_count_matches_input():
    _, point_count = compute_track_cells(_gpx(_TRACK_A))
    assert point_count == len(_TRACK_A)
