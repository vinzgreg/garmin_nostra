"""Shared helpers for the activity_track_signatures / activity_insights
backfill scripts.

Both need to locate the real GPX/FIT file for a stored path (tolerating a
known host-vs-container path storage quirk) and transparently decompress
gzip-wrapped legacy FIT files. Kept here once both backfills needed it,
rather than duplicated a second time.
"""

from __future__ import annotations

import gzip
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_GZIP_MAGIC = b"\x1f\x8b"


def maybe_gunzip(data: bytes) -> bytes:
    """Transparently decompress gzip-wrapped FIT bytes, if that's what this is."""
    if data[:2] == _GZIP_MAGIC:
        return gzip.decompress(data)
    return data


def resolve_path(stored_path: str, expected_dir: Path) -> Path | None:
    """Locate the real file for a stored path, tolerating a known old bug.

    A subset of rows have gpx_path/fit_path stored as a *host* absolute path
    (/home/<user>/data/garminnostra/gpx/...) instead of the *container* path
    (/data/gpx/...) every other row uses -- a pre-existing data quirk, not
    something introduced or fixed here (activities is not touched by either
    backfill script). The container only ever sees /data, so the stored
    path is unreadable as-is for those rows. Rather than skip them, fall
    back to reconstructing <expected_dir>/<user-dir>/<filename> from the
    stored path's last two components, which is stable regardless of what
    absolute prefix the path was originally written with.
    """
    direct = Path(stored_path)
    if direct.is_file():
        return direct
    fallback = expected_dir / direct.parent.name / direct.name
    if fallback.is_file():
        return fallback
    return None
