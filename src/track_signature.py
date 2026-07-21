"""Compute a compact, comparable "track signature" for a GPS track.

Used by nostra-mcp's find_similar_activities tool to answer "did I ride this
same route before" without that server ever reading a raw GPX/FIT file
itself: the signature is precomputed here, once, at ingest time, and stored
in the activity_track_signatures table as a plain derived summary -- like
distance_m or elevation_gain_m already are.

Approach: quantize every trackpoint to a grid cell (~35m, via a simple
equirectangular projection) and keep the *set* of unique cells the track
touches. Comparing two signatures is then a Jaccard similarity
(|A n B| / |A u B|) on two small sets of "x:y" strings -- order-independent
(handles a loop ridden in either direction) and naturally tolerant of
partial overlap (an early turnaround just shrinks the intersection).
"""

from __future__ import annotations

import logging
import math

import gpxpy

logger = logging.getLogger(__name__)

# Earth radius, metres -- adequate for a local equirectangular projection at
# this precision; no need for a more exact ellipsoid model at 35m cells.
_EARTH_RADIUS_M = 6371000.0

# Canonical cell size. Changing this invalidates every stored signature --
# old and new cells are not comparable at different sizes, so a change here
# requires a full re-run of scripts/backfill_track_cells.py.
DEFAULT_CELL_SIZE_M = 35.0


def compute_track_cells(
    gpx_bytes: bytes, cell_size_m: float = DEFAULT_CELL_SIZE_M
) -> tuple[str, int] | None:
    """Return (cells, point_count) for a GPX track, or None if unusable.

    *cells* is a sorted, comma-joined "x:y" string -- deterministic and
    diffable, cheap to re-split into a set on the read side. Returns None on
    a GPX parse failure or a track with zero points, mirroring the
    None-on-no-data convention already used by map_render.fit_to_gpx /
    render_map / render_elevation_profile.
    """
    try:
        gpx = gpxpy.parse(gpx_bytes.decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.warning("Track signature: GPX parse error: %s", exc)
        return None

    cells: set[str] = set()
    point_count = 0
    for track in gpx.tracks:
        for segment in track.segments:
            for pt in segment.points:
                point_count += 1
                x = math.radians(pt.longitude) * math.cos(math.radians(pt.latitude)) * _EARTH_RADIUS_M
                y = math.radians(pt.latitude) * _EARTH_RADIUS_M
                cells.add(f"{int(x // cell_size_m)}:{int(y // cell_size_m)}")

    if point_count == 0:
        logger.debug("Track signature: GPX has zero trackpoints -- nothing to compute.")
        return None

    return ",".join(sorted(cells)), point_count
