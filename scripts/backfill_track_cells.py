#!/usr/bin/env python3
"""One-off backfill: compute activity_track_signatures for existing activities.

Usage (inside the garmin-nostra container):
    python3 /app/scripts/backfill_track_cells.py /app/config.toml [--dry-run] [--limit N]

Safe to re-run: only processes activities with no row yet in
activity_track_signatures (a LEFT JOIN ... WHERE activity_id IS NULL), so a
second run touches 0 rows. Never modifies the `activities` table -- every
write here is additive, into activity_track_signatures only.

Garmin activities have a native gpx_path on disk and are read directly.
Wahoo activities have no gpx_path (see track_signature.py's module
docstring) -- their track only exists in the .fit file, so it is re-derived
via map_render.fit_to_gpx() before computing the signature. Activities with
neither file (e.g. indoor workouts) are skipped -- there is no track to
compare.

Every row is handled in its own try/except: a single corrupt or missing
file is logged and skipped, never aborts the run.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from map_render import fit_to_gpx  # noqa: E402
from storage import ActivityStore  # noqa: E402
from sync import load_config  # noqa: E402
from track_signature import DEFAULT_CELL_SIZE_M, compute_track_cells  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_track_cells")


def _pending_rows(store: ActivityStore) -> list[dict]:
    """Activities with no signature yet and at least one usable track file."""
    rows = store._conn.execute(
        """
        SELECT a.id, a.garmin_activity_id, a.source, a.gpx_path, a.fit_path
        FROM activities a
        LEFT JOIN activity_track_signatures s ON s.activity_id = a.id
        WHERE s.activity_id IS NULL
          AND a.suppressed IS NULL
          AND (a.gpx_path IS NOT NULL OR a.fit_path IS NOT NULL)
        ORDER BY a.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _resolve_path(stored_path: str, expected_dir: Path) -> Path | None:
    """Locate the real file for a stored path, tolerating a known old bug.

    A subset of rows (515 as of this writing, mostly one user) have
    gpx_path/fit_path stored as a *host* absolute path
    (/home/<user>/data/garminnostra/gpx/...) instead of the *container* path
    (/data/gpx/...) every other row uses -- a pre-existing data quirk, not
    something introduced or fixed here (activities is not touched by this
    script). The container only ever sees /data, so the stored path is
    unreadable as-is for those rows. Rather than skip them, fall back to
    reconstructing <expected_dir>/<user-dir>/<filename> from the stored
    path's last two components, which is stable regardless of what absolute
    prefix the path was originally written with.
    """
    direct = Path(stored_path)
    if direct.is_file():
        return direct
    fallback = expected_dir / direct.parent.name / direct.name
    if fallback.is_file():
        return fallback
    return None


def _gpx_bytes_for(row: dict, gpx_dir: Path, fit_dir: Path) -> tuple[bytes, str] | None:
    """Return (gpx_bytes, source_format) for one activity row, or None."""
    if row["gpx_path"]:
        path = _resolve_path(row["gpx_path"], gpx_dir)
        if path is None:
            logger.warning("id=%s: gpx_path not found (tried stored path and gpx_dir fallback): %s",
                            row["id"], row["gpx_path"])
            return None
        try:
            return path.read_bytes(), "gpx"
        except OSError as exc:
            logger.warning("id=%s: could not read %s: %s", row["id"], path, exc)
            return None

    if row["fit_path"]:
        path = _resolve_path(row["fit_path"], fit_dir)
        if path is None:
            logger.warning("id=%s: fit_path not found (tried stored path and fit_dir fallback): %s",
                            row["id"], row["fit_path"])
            return None
        try:
            fit_bytes = path.read_bytes()
        except OSError as exc:
            logger.warning("id=%s: could not read %s: %s", row["id"], path, exc)
            return None
        gpx_bytes = fit_to_gpx(fit_bytes)
        if gpx_bytes is None:
            logger.debug("id=%s: fit_to_gpx produced no track (indoor/no-GPS).", row["id"])
            return None
        return gpx_bytes, "fit"

    return None


def run(config_path: str, dry_run: bool, limit: int | None) -> None:
    cfg = load_config(config_path)
    storage_cfg = cfg.get("storage", {})
    gpx_dir = Path(storage_cfg.get("gpx_dir", "/data/gpx"))
    fit_dir = Path(storage_cfg.get("fit_dir", "/data/fit"))
    store = ActivityStore(
        db_path=storage_cfg.get("db_path", "/data/garmin_nostra.db"),
        gpx_dir=str(gpx_dir),
        fit_dir=str(fit_dir),
        map_dir=storage_cfg.get("map_dir", "/data/maps"),
        token_dir=storage_cfg.get("token_dir", "/data/tokens"),
    )
    try:
        rows = _pending_rows(store)
        if limit is not None:
            rows = rows[:limit]
        logger.info(
            "%d activities pending a track signature%s.",
            len(rows), " (dry run -- nothing will be written)" if dry_run else "",
        )

        computed = skipped = failed = 0
        sample: list[str] = []
        for row in rows:
            try:
                found = _gpx_bytes_for(row, gpx_dir, fit_dir)
                if found is None:
                    skipped += 1
                    continue
                gpx_bytes, source_format = found
                signature = compute_track_cells(gpx_bytes)
                if signature is None:
                    skipped += 1
                    continue
                cells, point_count = signature
                if not dry_run:
                    store.save_track_signature(
                        row["id"], DEFAULT_CELL_SIZE_M, cells, point_count, source_format,
                    )
                computed += 1
                if len(sample) < 5:
                    sample.append(
                        f"id={row['id']} source={source_format} points={point_count} "
                        f"cells={cells.count(',') + 1}"
                    )
            except Exception as exc:
                failed += 1
                logger.error("id=%s (%s): unexpected error, skipping: %s", row["id"], row["source"], exc)

        for line in sample:
            logger.info("sample: %s", line)
        logger.info(
            "Done. computed=%d skipped=%d failed=%d%s",
            computed, skipped, failed, " (dry run)" if dry_run else "",
        )
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="Path to config.toml")
    parser.add_argument("--dry-run", action="store_true", help="Compute and report, write nothing")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N activities (testing)")
    args = parser.parse_args()
    run(args.config, args.dry_run, args.limit)
