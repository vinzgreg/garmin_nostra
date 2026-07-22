#!/usr/bin/env python3
"""One-off backfill: compute activity_insights for existing activities.

Usage (inside the garmin-nostra container):
    python3 /app/scripts/backfill_insights.py /app/config.toml [--dry-run] [--limit N]

Safe to re-run: only processes activities with no row yet in
activity_insights (a LEFT JOIN ... WHERE activity_id IS NULL), so a second
run touches 0 rows. Never modifies the `activities` table -- every write
here is additive, into activity_insights only.

Unlike backfill_track_cells.py, this does NOT route Wahoo/FIT-only
activities through map_render.fit_to_gpx() -- that conversion only carries
lat/lon/ele/time through and silently drops heart-rate/cadence/power, which
is exactly the data insights needs. Rows with a gpx_path are parsed via
insights.compute_insights_from_gpx(); rows with only a fit_path are parsed
via insights.compute_insights_from_fit() directly, after the same
gzip-fallback used by backfill_track_cells.py for legacy .fit.gz files.

Every row is handled in its own try/except: a single corrupt or missing
file is logged and skipped, never aborts the run.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from storage import ActivityStore  # noqa: E402
from sync import load_config  # noqa: E402
from insights import SCHEMA_VERSION, compute_insights_from_fit, compute_insights_from_gpx  # noqa: E402
from _backfill_common import maybe_gunzip, resolve_path  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_insights")


def _pending_rows(store: ActivityStore) -> list[dict]:
    """Activities needing insights: no row yet, or one at an older schema.

    Includes rows whose stored schema_version is behind the current
    SCHEMA_VERSION so that bumping it actually forces reprocessing (save_insights
    does INSERT OR REPLACE). Without this, the constant would be inert — the
    documented "bump to reprocess" upgrade path would silently no-op.
    """
    rows = store._conn.execute(
        """
        SELECT a.id, a.garmin_activity_id, a.source, a.gpx_path, a.fit_path
        FROM activities a
        LEFT JOIN activity_insights i ON i.activity_id = a.id
        WHERE (i.activity_id IS NULL OR i.schema_version < ?)
          AND a.suppressed IS NULL
          AND (a.gpx_path IS NOT NULL OR a.fit_path IS NOT NULL)
        ORDER BY a.id
        """,
        (SCHEMA_VERSION,),
    ).fetchall()
    return [dict(r) for r in rows]


def _compute_for(row: dict, gpx_dir: Path, fit_dir: Path) -> dict | None:
    """Return an insights result for one activity row, or None."""
    if row["gpx_path"]:
        path = resolve_path(row["gpx_path"], gpx_dir)
        if path is None:
            logger.warning("id=%s: gpx_path not found (tried stored path and gpx_dir fallback): %s",
                            row["id"], row["gpx_path"])
            return None
        try:
            gpx_bytes = path.read_bytes()
        except OSError as exc:
            logger.warning("id=%s: could not read %s: %s", row["id"], path, exc)
            return None
        return compute_insights_from_gpx(gpx_bytes)

    if row["fit_path"]:
        path = resolve_path(row["fit_path"], fit_dir)
        if path is None:
            logger.warning("id=%s: fit_path not found (tried stored path and fit_dir fallback): %s",
                            row["id"], row["fit_path"])
            return None
        try:
            fit_bytes = path.read_bytes()
        except OSError as exc:
            logger.warning("id=%s: could not read %s: %s", row["id"], path, exc)
            return None
        try:
            fit_bytes = maybe_gunzip(fit_bytes)
        except OSError as exc:
            logger.warning("id=%s: could not decompress %s: %s", row["id"], path, exc)
            return None
        return compute_insights_from_fit(fit_bytes)

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
            "%d activities pending insights%s.",
            len(rows), " (dry run -- nothing will be written)" if dry_run else "",
        )

        computed = skipped = failed = 0
        sample: list[str] = []
        for row in rows:
            try:
                result = _compute_for(row, gpx_dir, fit_dir)
                if result is None:
                    skipped += 1
                    continue
                if not dry_run:
                    store.save_insights(
                        row["id"], SCHEMA_VERSION,
                        result["source_format"], result["splits_json"],
                        result["hr_drift_pct"], result["negative_split"],
                        result["has_hr"], result["has_cadence"], result["has_power"],
                    )
                computed += 1
                if len(sample) < 5:
                    n_splits = len(result["splits_json"]["splits"])
                    sample.append(
                        f"id={row['id']} source={result['source_format']} splits={n_splits} "
                        f"has_hr={result['has_hr']} has_cadence={result['has_cadence']} "
                        f"has_power={result['has_power']}"
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
