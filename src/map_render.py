"""Render a GPX track to a PNG map image using OpenStreetMap tiles."""

from __future__ import annotations

import io
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import fitparse
import gpxpy
from PIL import Image, ImageDraw, ImageFont
from staticmap import CircleMarker, Line, StaticMap

logger = logging.getLogger(__name__)

_OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_MAP_WIDTH  = 800
_MAP_HEIGHT = 600
_TRACK_COLOR = "#3b82f6"   # blue
_START_COLOR = "#22c55e"   # green
_END_COLOR   = "#ef4444"   # red

_PROFILE_WIDTH       = 800
_PROFILE_HEIGHT      = 800
_PROFILE_PADDING     = 90
_PROFILE_BG_COLOR    = "#15464a"   # dark teal — garminnostra_avatar.png background
_PROFILE_FILL_COLOR  = "#b34d04"   # muted orange, semi-opaque fill under the line
_PROFILE_LINE_COLOR  = "#f66b00"   # bright orange — avatar's suit colour
_PROFILE_GRID_COLOR  = "#2e6166"   # lighter teal grid lines
_PROFILE_TEXT_COLOR  = "#f2e9dc"   # warm off-white — avatar highlights
_PROFILE_SPEED_COLOR = (242, 233, 220, 130)   # translucent off-white — speed line, 2nd y-axis
_PROFILE_SPEED_LABEL_COLOR = "#f2e9dc"        # solid — speed axis labels/legend stay readable
_PROFILE_SPEED_SMOOTHING_PTS = 11  # rolling-average window over GPX points
_PROFILE_MIN_AXIS_SPAN_M = 500.0   # y-axis always spans at least this many metres
_PROFILE_GRID_STEP_M     = 100.0   # horizontal grid-line / label interval
_PROFILE_AXIS_FILL       = 0.8     # data peak sits at ~80% of axis height (20% headroom)


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


_SEMICIRCLES_TO_DEG = 180.0 / 2**31


def fit_to_gpx(fit_bytes: bytes) -> bytes | None:
    """Convert raw FIT bytes to minimal GPX bytes.

    Each track point also carries <ele> (from the record's ``altitude``,
    falling back to ``enhanced_altitude``) and <time> when present, so the
    GPX is usable for elevation/speed profiles and not just the map.

    Returns None if the FIT file contains fewer than 2 GPS points
    (e.g. indoor activities without GPS).
    """
    try:
        fit = fitparse.FitFile(io.BytesIO(fit_bytes))
        points: list[tuple[float, float, float | None, str | None]] = []
        for record in fit.get_messages("record"):
            data = {f.name: f.value for f in record}
            lat = data.get("position_lat")
            lon = data.get("position_long")
            if lat is None or lon is None:
                continue
            ele = data.get("altitude")
            if ele is None:
                ele = data.get("enhanced_altitude")
            ts = data.get("timestamp")
            time_str = None
            if isinstance(ts, datetime):
                # FIT timestamps are UTC; emit a Z-suffixed ISO string so
                # gpxpy parses every point as timezone-aware and consistent.
                if ts.tzinfo is not None:
                    ts = ts.astimezone(timezone.utc)
                time_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            points.append((lat * _SEMICIRCLES_TO_DEG, lon * _SEMICIRCLES_TO_DEG, ele, time_str))
    except Exception as exc:
        logger.warning("FIT parse error: %s", exc)
        return None

    if len(points) < 2:
        logger.debug("FIT has fewer than 2 GPS points — skipping GPX conversion.")
        return None

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="garmin-nostra">',
        "  <trk><trkseg>",
    ]
    for lat, lon, ele, time_str in points:
        if ele is None and time_str is None:
            lines.append(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"/>')
            continue
        lines.append(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}">')
        if ele is not None:
            lines.append(f"      <ele>{ele:.1f}</ele>")
        if time_str is not None:
            lines.append(f"      <time>{time_str}</time>")
        lines.append("    </trkpt>")
    lines += ["  </trkseg></trk>", "</gpx>"]
    return "\n".join(lines).encode()


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


def _smoothed_speeds_kmh(points_data) -> list[float] | None:
    """
    Compute a speed (km/h) for each GPX point, smoothed with a rolling
    average to cancel out GPS jitter. Returns None if the track has no
    timestamps.
    """
    raw: list[float] = [0.0]
    for prev, cur in zip(points_data, points_data[1:]):
        t_prev, t_cur = prev.point.time, cur.point.time
        if t_prev is None or t_cur is None:
            return None
        dt = (t_cur - t_prev).total_seconds()
        dd = cur.distance_from_start - prev.distance_from_start
        raw.append((dd / dt) * 3.6 if dt > 0 else raw[-1])

    window = _PROFILE_SPEED_SMOOTHING_PTS
    half = window // 2
    smoothed = []
    for i in range(len(raw)):
        lo, hi = max(0, i - half), min(len(raw), i + half + 1)
        chunk = raw[lo:hi]
        smoothed.append(sum(chunk) / len(chunk))
    return smoothed


def render_elevation_profile(
    gpx_data: bytes,
    output_path: Path,
) -> Path | None:
    """
    Parse *gpx_data* and render an elevation profile to a PNG at *output_path*.

    Skipped (returns None) if there is no elevation data or fewer than 2 points.
    """
    try:
        gpx = gpxpy.parse(gpx_data.decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.warning("GPX parse error: %s", exc)
        return None

    points_data = [p for p in gpx.get_points_data() if p.point.elevation is not None]
    if len(points_data) < 2:
        logger.debug("GPX has fewer than 2 elevation points — skipping profile render.")
        return None

    uphill, _ = gpx.get_uphill_downhill()

    distances = [p.distance_from_start / 1000.0 for p in points_data]   # km
    elevations = [p.point.elevation for p in points_data]
    speeds = _smoothed_speeds_kmh(points_data)

    min_ele, max_ele = min(elevations), max(elevations)
    # Y-axis: anchor the bottom on a 100 m boundary below the lowest point,
    # then give the peak ~20% headroom (data tops out at ~80% of the height).
    # Enforce a minimum span so gently rolling rides aren't exaggerated, and
    # snap the span up to whole 100 m steps for clean grid-line delimiters.
    axis_min = math.floor(min_ele / _PROFILE_GRID_STEP_M) * _PROFILE_GRID_STEP_M
    axis_span = max((max_ele - axis_min) / _PROFILE_AXIS_FILL, _PROFILE_MIN_AXIS_SPAN_M)
    axis_span = math.ceil(axis_span / _PROFILE_GRID_STEP_M) * _PROFILE_GRID_STEP_M
    ele_span = axis_span
    min_ele = axis_min
    max_dist = max(distances) or 1.0
    max_speed = max(speeds) if speeds else 0.0
    speed_axis_max = max(max_speed * 1.1, 1.0)

    plot_w = _PROFILE_WIDTH - 2 * _PROFILE_PADDING
    plot_h = _PROFILE_HEIGHT - 2 * _PROFILE_PADDING

    def to_xy(dist_km: float, ele_m: float) -> tuple[float, float]:
        x = _PROFILE_PADDING + (dist_km / max_dist) * plot_w
        y = _PROFILE_PADDING + plot_h - ((ele_m - min_ele) / ele_span) * plot_h
        return x, y

    def speed_to_xy(dist_km: float, speed_kmh: float) -> tuple[float, float]:
        x = _PROFILE_PADDING + (dist_km / max_dist) * plot_w
        y = _PROFILE_PADDING + plot_h - (speed_kmh / speed_axis_max) * plot_h
        return x, y

    try:
        image = Image.new("RGB", (_PROFILE_WIDTH, _PROFILE_HEIGHT), _PROFILE_BG_COLOR)
        draw = ImageDraw.Draw(image)
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
            title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        except OSError:
            font = title_font = ImageFont.load_default()

        # Horizontal grid lines with elevation labels, one per 100 m delimiter
        num_lines = int(round(ele_span / _PROFILE_GRID_STEP_M)) + 1
        for i in range(num_lines):
            ele = axis_min + i * _PROFILE_GRID_STEP_M
            _, y = to_xy(0, ele)
            draw.line((_PROFILE_PADDING, y, _PROFILE_WIDTH - _PROFILE_PADDING, y), fill=_PROFILE_GRID_COLOR)
            draw.text((6, y - 12), f"{int(ele)} m", font=font, fill=_PROFILE_TEXT_COLOR)

        # Filled area under the elevation line
        baseline_y = _PROFILE_PADDING + plot_h
        polygon = [to_xy(d, e) for d, e in zip(distances, elevations)]
        polygon = [(_PROFILE_PADDING, baseline_y)] + polygon + [(polygon[-1][0], baseline_y)]
        draw.polygon(polygon, fill=_PROFILE_FILL_COLOR)
        draw.line(polygon[1:-1], fill=_PROFILE_LINE_COLOR, width=4)

        # Speed line on the 2nd y-axis (right side) — drawn translucent so it
        # doesn't fight visually with the elevation fill underneath.
        if speeds:
            speed_line = [speed_to_xy(d, s) for d, s in zip(distances, speeds)]
            overlay_draw.line(speed_line, fill=_PROFILE_SPEED_COLOR, width=2)
            for i in range(5):
                speed = speed_axis_max * i / 4
                _, y = speed_to_xy(0, speed)
                draw.text((_PROFILE_WIDTH - _PROFILE_PADDING + 10, y - 12), f"{speed:.0f}", font=font, fill=_PROFILE_SPEED_LABEL_COLOR)

        # X-axis distance labels
        for i in range(5):
            dist = max_dist * i / 4
            x, _ = to_xy(dist, min_ele)
            draw.text((x - 24, _PROFILE_HEIGHT - _PROFILE_PADDING + 14), f"{dist:.1f} km", font=font, fill=_PROFILE_TEXT_COLOR)

        draw.text(
            (_PROFILE_PADDING, 28),
            f"Höhenprofil — {int(uphill)} m Anstieg",
            font=title_font,
            fill=_PROFILE_TEXT_COLOR,
        )

        if speeds:
            legend_y = 64
            draw.line((_PROFILE_PADDING, legend_y + 8, _PROFILE_PADDING + 30, legend_y + 8), fill=_PROFILE_LINE_COLOR, width=4)
            draw.text((_PROFILE_PADDING + 38, legend_y), "Höhe", font=font, fill=_PROFILE_TEXT_COLOR)
            draw.line((_PROFILE_PADDING + 130, legend_y + 8, _PROFILE_PADDING + 160, legend_y + 8), fill=_PROFILE_SPEED_LABEL_COLOR, width=2)
            draw.text((_PROFILE_PADDING + 168, legend_y), "Tempo (km/h)", font=font, fill=_PROFILE_TEXT_COLOR)

        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(str(output_path), "PNG")
        logger.info("Elevation profile saved: %s", output_path)
        return output_path
    except Exception as exc:
        logger.warning("Elevation profile rendering failed: %s", exc)
        return None
