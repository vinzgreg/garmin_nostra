"""Render a GPX track to a PNG map image using OpenStreetMap tiles."""

from __future__ import annotations

import logging
from pathlib import Path

import gpxpy
from PIL import ImageDraw, ImageFont
from staticmap import CircleMarker, Line, StaticMap

logger = logging.getLogger(__name__)

_OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_MAP_WIDTH  = 800
_MAP_HEIGHT = 600
_TRACK_COLOR = "#3b82f6"   # blue
_START_COLOR = "#22c55e"   # green
_END_COLOR   = "#ef4444"   # red


def _add_osm_attribution(image) -> None:
    """Stamp a tiny OSM attribution notice in the bottom-right corner."""
    text = "© OpenStreetMap contributors"
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = image.width - w - 4
    y = image.height - h - 4
    draw.rectangle((x - 2, y - 1, x + w + 2, y + h + 2), fill=(255, 255, 255, 180))
    draw.text((x, y), text, font=font, fill=(80, 80, 80))


def render_map(gpx_data: bytes, output_path: Path, timeout: int = 30) -> Path | None:
    """
    Parse *gpx_data* and render the track to a PNG at *output_path*.

    Returns the path on success, None if there is no track or rendering fails.
    """
    try:
        gpx = gpxpy.parse(gpx_data.decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.warning("GPX parse error: %s", exc)
        return None

    # Collect (lon, lat) tuples from all track segments
    points: list[tuple[float, float]] = []
    for track in gpx.tracks:
        for segment in track.segments:
            for pt in segment.points:
                points.append((pt.longitude, pt.latitude))

    if len(points) < 2:
        logger.debug("GPX has fewer than 2 points — skipping map render.")
        return None

    try:
        m = StaticMap(
            _MAP_WIDTH,
            _MAP_HEIGHT,
            padding_x=20,
            padding_y=20,
            url_template=_OSM_TILE_URL,
            tile_request_timeout=timeout,
        )
        m.add_line(Line(points, _TRACK_COLOR, 3))
        m.add_marker(CircleMarker(points[0],  _START_COLOR, 12))
        m.add_marker(CircleMarker(points[-1], _END_COLOR,   12))

        image = m.render()
        _add_osm_attribution(image)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(str(output_path), "PNG")
        logger.info("Map saved: %s", output_path)
        return output_path
    except Exception as exc:
        logger.warning("Map rendering failed: %s", exc)
        return None
