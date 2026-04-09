"""garmin-nostra — main sync orchestrator (multi-user)."""

from __future__ import annotations

import fcntl
import fnmatch
import logging
import os
import socket
import sys
import time

import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-reattr]

from garmin import GarminClient, GarminRateLimitError
from wahoo import WahooAuthError, WahooClient, map_wahoo_activity
from storage import ActivityStore
from caldav_push import CalDAVPusher
from mastodon_bot import MastodonBot
from kudos_machine import KudosMachine
from map_render import fit_to_gpx, render_map

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT,
)
logger = logging.getLogger("sync")

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _set_log_level(level_str: str) -> None:
    level = _LOG_LEVELS.get(level_str.lower())
    if level is None:
        logger.warning("Unknown log_level '%s', using INFO.", level_str)
        return
    logging.getLogger().setLevel(level)


def _configure_log_file(path: str) -> None:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    logging.getLogger().addHandler(handler)


_LOCK_FILE = "/tmp/garmin-nostra-sync.lock"


def _acquire_lock() -> int:
    """Open and exclusively lock a file. Returns the fd, or exits if already locked."""
    fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("Another sync is already running — skipping.")
        os.close(fd)
        sys.exit(0)
    return fd


def _resolve_env_vars(obj):
    """Recursively resolve 'env:VAR_NAME' strings to environment variable values.

    This allows sensitive config values (passwords, tokens) to be pulled
    from the environment or Docker secrets instead of being stored in
    plaintext in config.toml.

    Example:  mastodon_access_token = "env:MASTODON_TOKEN"
    """
    if isinstance(obj, str) and obj.startswith("env:"):
        var_name = obj[4:]
        value = os.environ.get(var_name)
        if value is None:
            raise ValueError(
                f"Config references env:{var_name} but the environment "
                f"variable {var_name} is not set."
            )
        return value
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(item) for item in obj]
    return obj


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    return _resolve_env_vars(cfg)


def _build_caldav_pusher(cfg: dict, timeout: int = 30) -> CalDAVPusher | None:
    caldav_cfg = cfg.get("caldav", {})
    if not caldav_cfg.get("url"):
        return None
    return CalDAVPusher(
        url=caldav_cfg["url"],
        username=caldav_cfg["username"],
        password=caldav_cfg["password"],
        calendar_name=caldav_cfg.get("calendar_name", "Fitness"),
        timeout=timeout,
    )


def _parse_extra_mentions(value) -> list[str]:
    """Parse mastodon_add_mention config into a list of handles.

    Accepts a string like "@user2@inst.social , @user3@inst.social" or a list.
    Returns an empty list when value is falsy.
    """
    if not value:
        return []
    if isinstance(value, list):
        return [h.strip() for h in value if h.strip()]
    return [h.strip() for h in str(value).replace(",", " ").split() if h.strip()]


def _mastodon_visibility(mastodon_public) -> str:
    """Map mastodon_public config value to a Mastodon visibility string.

    true    → "public"   (on public timeline)
    "listed" → "unlisted" (followers + link, not on timeline)
    false   → "direct"   (DM — only the mentioned user sees it)
    """
    if mastodon_public is True:
        return "public"
    if mastodon_public == "listed":
        return "unlisted"
    return "direct"


def process_user(
    user_cfg: dict,
    store: ActivityStore,
    bot: MastodonBot,
    caldav_pusher: CalDAVPusher | None,
    lookback_days: int,
    request_timeout: int = 30,
    gpx_max_age_days: int | None = None,
    fit_max_age_days: int | None = None,
    mastodon_max_age_days: int | None = None,
    mastodon_post_delay_s: float = 2.0,
    kudos_machine: KudosMachine | None = None,
    tag_source: bool = False,
    dedup_with_wahoo: bool = False,
    skip_kudos: bool = False,
) -> None:
    name   = user_cfg["name"]
    handle = user_cfg.get("mastodon_handle")
    caldav_enabled = user_cfg.get("caldav_enabled", False)

    user_id = store.upsert_user(user_cfg)

    # Determine sync window
    since    = store.get_last_sync_time(user_id)
    earliest = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    since    = max(since, earliest)
    # Always look back at least 2 hours so that recently-saved activities
    # are re-fetched — Garmin may not have finished computing all metrics
    # (e.g. averagePower for trainer workouts) at initial sync time.
    metrics_cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    since = min(since, metrics_cutoff)
    logger.info("[%s] Syncing activities since %s.", name, since.isoformat())

    garmin = GarminClient(
        username=user_cfg["garmin_username"],
        password=user_cfg["garmin_password"],
        tokenstore=store.token_dir / name,
        timeout=request_timeout,
    )

    run_id = store.start_sync_run(user_id)
    found = processed = 0

    try:
        activities = garmin.get_activities_since(since, timeout=request_timeout * 4)
        found = len(activities)

        # Process oldest-first so last_sync advances monotonically
        activities.sort(
            key=lambda a: a.get("startTimeGMT") or a.get("startTimeLocal") or ""
        )

        for idx, act in enumerate(activities, 1):
            garmin_id = str(act["activityId"])
            logger.info("[%s] Activity %d/%d: %s", name, idx, found, garmin_id)

            # Check DB for this activity
            logger.debug("[%s] DB lookup for %s", name, garmin_id)
            existing = store.get_activity(user_id, garmin_id)
            logger.debug("[%s] DB lookup done: %s", name, "exists" if existing else "new")

            start_str = act.get("startTimeGMT") or act.get("startTimeLocal", "")
            try:
                act_time = datetime.fromisoformat(
                    start_str.replace(" ", "T")
                ).replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                act_time = None

            # Skip very recent activities — let Garmin finish processing them
            if existing is None and act_time is not None:
                age = datetime.now(timezone.utc) - act_time
                if age < timedelta(minutes=10):
                    logger.info(
                        "[%s] Skipping activity %s (only %ds old, waiting for next cycle).",
                        name, garmin_id, int(age.total_seconds()),
                    )
                    continue

            if existing is None:
                # ── Cross-source dedup: skip if a Wahoo activity with the
                #    same start time is already in the DB (e.g. Wahoo
                #    auto-synced this workout to Garmin and we already
                #    processed it from the Wahoo side). ───────────────────────
                if dedup_with_wahoo and act_time is not None:
                    start_str_db = act_time.strftime("%Y-%m-%d %H:%M:%S")
                    dupe = store.get_activity_near_time(
                        user_id, start_str_db, window_s=120, source="WahooNoStra"
                    )
                    if dupe:
                        logger.info(
                            "[%s] Skipping Garmin activity %s — already imported as "
                            "Wahoo activity %s.",
                            name, garmin_id, dupe["garmin_activity_id"],
                        )
                        continue

                # ── New activity — download, store, then integrate ──────────
                gpx_data = None
                gpx_path = None
                fit_path = None

                skip_gpx = (
                    gpx_max_age_days is not None
                    and act_time is not None
                    and act_time < datetime.now(timezone.utc) - timedelta(days=gpx_max_age_days)
                )
                logger.debug("[%s] skip_gpx=%s act_time=%s", name, skip_gpx, act_time)

                if skip_gpx:
                    logger.debug("[%s] Skipping GPX for old activity %s.", name, garmin_id)
                else:
                    logger.debug("[%s] Downloading GPX for %s", name, garmin_id)
                    try:
                        gpx_data = garmin.get_gpx(garmin_id, timeout=request_timeout)
                        gpx_path = store.save_gpx(name, garmin_id, gpx_data)
                    except Exception as exc:
                        logger.warning("[%s] GPX download failed for %s: %s", name, garmin_id, exc)

                skip_fit = (
                    fit_max_age_days is not None
                    and act_time is not None
                    and act_time < datetime.now(timezone.utc) - timedelta(days=fit_max_age_days)
                )
                logger.debug("[%s] skip_fit=%s act_time=%s", name, skip_fit, act_time)

                if skip_fit:
                    logger.debug("[%s] Skipping FIT for old activity %s.", name, garmin_id)
                else:
                    logger.debug("[%s] Downloading FIT for %s", name, garmin_id)
                    try:
                        fit_data = garmin.get_fit(garmin_id, timeout=request_timeout)
                        fit_path = store.save_fit(name, garmin_id, fit_data)
                    except Exception as exc:
                        logger.warning("[%s] FIT download failed for %s: %s", name, garmin_id, exc)

                map_path = None
                if gpx_data:
                    map_path = store.map_path(name, garmin_id)
                    map_path = render_map(gpx_data, map_path, timeout=request_timeout)

                logger.debug("[%s] Saving activity %s to DB", name, garmin_id)
                activity_row = store.save_activity(
                    user_id, act, gpx_path, fit_path,
                    name_prefix="[Garmin] " if tag_source else None,
                )
                logger.info("[%s] New activity saved: %s", name, garmin_id)
                if activity_row.get("suppressed"):
                    logger.info(
                        "[%s] Activity %s suppressed (%s), skipping integrations.",
                        name, garmin_id, activity_row["suppressed"],
                    )
                    continue
                # For indoor cycling, defer integrations to the next sync cycle
                # — Garmin may not have finished computing averagePower at
                # upload time.  The 2-hour lookback window ensures this
                # activity is re-fetched on the next run.
                if activity_row.get("activity_type") == "indoor_cycling":
                    logger.info("[%s] Activity %s saved — deferring integrations to next sync (indoor cycling).", name, garmin_id)
                    continue
            else:
                # ── Known activity — check if any integration needs retry ───
                activity_row = existing
                logger.debug("[%s] Activity %s already known.", name, garmin_id)
                map_path = store.map_path(name, garmin_id)
                # Skip suppressed activities and those already fully processed
                if existing.get("suppressed") or (existing["caldav_pushed"] and existing["mastodon_posted"]):
                    continue
                # Backfill metrics that Garmin may have computed since the
                # initial sync (e.g. averagePower for trainer workouts).
                # Only fills NULLs — never overwrites existing values.
                store.backfill_activity_metrics(user_id, garmin_id, act)
                activity_row = store.get_activity(user_id, garmin_id)

            # ── CalDAV (optional) ──────────────────────────────────────────
            if caldav_enabled and caldav_pusher and not activity_row.get("caldav_pushed"):
                logger.debug("[%s] Pushing CalDAV for %s", name, garmin_id)
                try:
                    caldav_pusher.push(activity_row)
                    store.mark_caldav_pushed(user_id, garmin_id)
                    logger.info("[%s] CalDAV event created: %s", name, garmin_id)
                except Exception as exc:
                    logger.error("[%s] CalDAV push failed for %s: %s", name, garmin_id, exc)

            # ── Mastodon DM ────────────────────────────────────────────────
            skip_mastodon = (
                mastodon_max_age_days is not None
                and act_time is not None
                and act_time < datetime.now(timezone.utc) - timedelta(days=mastodon_max_age_days)
            )
            suppress_patterns = [p.lower() for p in user_cfg.get("mastodon_suppress_types", [])]
            act_type = (activity_row.get("activity_type") or "").lower()
            if suppress_patterns and any(fnmatch.fnmatch(act_type, p) for p in suppress_patterns):
                if not activity_row.get("mastodon_posted"):
                    logger.info(
                        "[%s] Mastodon post suppressed for activity type '%s' (%s).",
                        name, act_type, garmin_id,
                    )
                    store.mark_mastodon_posted(user_id, garmin_id, status_id=None)
                skip_mastodon = True
            if handle and not activity_row.get("mastodon_posted") and not skip_mastodon:
                logger.debug("[%s] Posting Mastodon for %s", name, garmin_id)
                try:
                    status_id = bot.post_activity(handle, activity_row, map_path,
                                                  visibility=_mastodon_visibility(user_cfg.get("mastodon_public", False)),
                                                  extra_mentions=_parse_extra_mentions(user_cfg.get("mastodon_add_mention")))
                    store.mark_mastodon_posted(user_id, garmin_id, status_id=status_id)
                    if mastodon_post_delay_s > 0:
                        time.sleep(mastodon_post_delay_s)
                except Exception as exc:
                    logger.error("[%s] Mastodon post failed for %s: %s", name, garmin_id, exc)
            logger.debug("[%s] Activity %s done", name, garmin_id)

            processed += 1

        store.finish_sync_run(run_id, found, processed, "success")
        logger.info("[%s] Sync complete. %d found / %d processed.", name, found, processed)

    except GarminRateLimitError as exc:
        store.finish_sync_run(run_id, found, processed, "failed", str(exc))
        logger.warning("[%s] Garmin sync skipped: %s", name, exc)
    except Exception as exc:
        store.finish_sync_run(run_id, found, processed, "failed", str(exc))
        logger.error("[%s] Sync failed: %s", name, exc, exc_info=True)

    # ── KudosMachine — runs after every invocation, even if Garmin sync failed ─
    # skip_kudos=True when called from "both" mode — Wahoo side already ran it
    _visibility = _mastodon_visibility(user_cfg.get("mastodon_public", False))
    if kudos_machine is not None and handle and not skip_kudos and not user_cfg.get("suppressKudos", False) and _visibility != "direct":
        logger.debug("[%s] Running KudosMachine.", name)
        try:
            kudos_machine.process_user(
                user_id, handle, store, max_age_days=mastodon_max_age_days,
                visibility=_visibility,
            )
        except Exception as exc:
            logger.error("[%s] KudosMachine failed: %s", name, exc)


def process_user_wahoo(
    user_cfg: dict,
    store: ActivityStore,
    bot: MastodonBot,
    caldav_pusher: CalDAVPusher | None,
    lookback_days: int,
    request_timeout: int = 30,
    fit_max_age_days: int | None = None,
    mastodon_max_age_days: int | None = None,
    mastodon_post_delay_s: float = 2.0,
    kudos_machine: KudosMachine | None = None,
) -> None:
    """Sync activities from Wahoo Cloud API for a single user."""
    name   = user_cfg["name"]
    handle = user_cfg.get("mastodon_handle")
    caldav_enabled = user_cfg.get("caldav_enabled", False)

    user_id = store.upsert_user(user_cfg)

    # Determine sync window
    since    = store.get_last_sync_time(user_id)
    earliest = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    since    = max(since, earliest)
    logger.info("[%s] (Wahoo) Syncing workouts since %s.", name, since.isoformat())

    wahoo = WahooClient(
        client_id=user_cfg["wahoo_client_id"],
        client_secret=user_cfg["wahoo_client_secret"],
        refresh_token=user_cfg["wahoo_refresh_token"],
        token_path=store.token_dir / name / "wahoo_tokens.json",
        timeout=request_timeout,
    )

    # Optional: Garmin client for wahoo→garmin sync
    garmin_for_upload = None
    if user_cfg.get("wahoo_sync_to_garmin"):
        if not user_cfg.get("garmin_username") or not user_cfg.get("garmin_password"):
            logger.warning(
                "[%s] wahoo_sync_to_garmin is enabled but garmin_username/garmin_password "
                "is missing — Garmin upload will be skipped.",
                name,
            )
        else:
            garmin_for_upload = GarminClient(
                username=user_cfg["garmin_username"],
                password=user_cfg["garmin_password"],
                tokenstore=store.token_dir / name,
                timeout=request_timeout,
            )

    run_id = store.start_sync_run(user_id)
    found = processed = 0

    try:
        workouts = wahoo.get_workouts_since(since, timeout=request_timeout * 4)
        found = len(workouts)

        # Process oldest-first so last_sync advances monotonically
        workouts.sort(key=lambda w: w.get("starts") or "")

        skipped_401 = 0
        for idx, workout in enumerate(workouts, 1):
            wahoo_id = str(workout["id"])

            # Skip workouts permanently marked as inaccessible
            if store.is_wahoo_skipped(user_id, wahoo_id):
                skipped_401 += 1
                continue

            logger.info("[%s] Wahoo workout %d/%d: %s", name, idx, found, wahoo_id)

            existing = store.get_wahoo_activity(user_id, wahoo_id)

            starts_str = workout.get("starts") or ""
            try:
                act_time = datetime.fromisoformat(
                    starts_str.replace("Z", "+00:00")
                )
                if act_time.tzinfo is None:
                    act_time = act_time.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                act_time = None

            # Skip very recent activities — let Wahoo finish processing
            if existing is None and act_time is not None:
                age = datetime.now(timezone.utc) - act_time
                if age < timedelta(minutes=10):
                    logger.info(
                        "[%s] Skipping workout %s (only %ds old, waiting for next cycle).",
                        name, wahoo_id, int(age.total_seconds()),
                    )
                    continue

            if existing is None:
                # ── New workout — fetch summary, download FIT, store ──────
                try:
                    summary = wahoo.get_workout_summary(wahoo_id, timeout=request_timeout)
                except requests.exceptions.HTTPError as exc:
                    if exc.response is not None and exc.response.status_code == 401:
                        store.mark_wahoo_skipped(user_id, wahoo_id, "401 Unauthorized")
                        skipped_401 += 1
                        logger.warning("[%s] Workout %s inaccessible (401), permanently skipped.", name, wahoo_id)
                        continue
                    logger.error("[%s] Failed to fetch summary for %s: %s", name, wahoo_id, exc)
                    continue
                except Exception as exc:
                    logger.error("[%s] Failed to fetch summary for %s: %s", name, wahoo_id, exc)
                    continue

                activity_row = map_wahoo_activity(user_id, workout, summary)

                fit_path = None
                fit_data = None
                skip_fit = (
                    fit_max_age_days is not None
                    and act_time is not None
                    and act_time < datetime.now(timezone.utc) - timedelta(days=fit_max_age_days)
                )

                if not skip_fit:
                    try:
                        fit_data = wahoo.get_fit(wahoo_id, timeout=request_timeout, summary=summary)
                        if fit_data:
                            fit_path = store.save_fit(name, wahoo_id, fit_data)
                    except Exception as exc:
                        logger.warning("[%s] FIT download failed for %s: %s", name, wahoo_id, exc)
                        fit_data = None

                map_path = None
                if fit_data:
                    gpx_data = fit_to_gpx(fit_data)
                    if gpx_data:
                        map_path = store.map_path(name, wahoo_id)
                        map_path = render_map(gpx_data, map_path, timeout=request_timeout)

                activity_row = store.save_wahoo_activity(user_id, activity_row, fit_path=fit_path)
                logger.info("[%s] New Wahoo workout saved: %s", name, wahoo_id)

                # ── Wahoo → Garmin sync ───────────────────────────────────
                if garmin_for_upload and fit_data and not activity_row.get("wahoo_synced_to_garmin"):
                    try:
                        garmin_for_upload.upload_fit(fit_data, timeout=request_timeout)
                        store.mark_wahoo_synced_to_garmin(user_id, wahoo_id)
                        logger.info("[%s] Wahoo workout %s uploaded to Garmin.", name, wahoo_id)
                    except Exception as exc:
                        # Garmin may reject duplicates if Wahoo already synced
                        # the same activity — log as warning, not error
                        if "duplicate" in str(exc).lower() or "conflict" in str(exc).lower():
                            logger.warning("[%s] Garmin upload skipped for %s (likely duplicate): %s", name, wahoo_id, exc)
                            store.mark_wahoo_synced_to_garmin(user_id, wahoo_id)
                        else:
                            logger.error("[%s] Garmin upload failed for %s: %s", name, wahoo_id, exc)

            else:
                # ── Known activity — check if any integration needs retry ─
                activity_row = existing
                logger.debug("[%s] Wahoo workout %s already known.", name, wahoo_id)
                map_path = None
                # Retry wahoo→garmin if still pending (independent of mastodon/caldav)
                if (
                    garmin_for_upload
                    and not existing.get("wahoo_synced_to_garmin")
                    and existing.get("fit_path")
                ):
                    try:
                        fit_data = Path(existing["fit_path"]).read_bytes()
                        garmin_for_upload.upload_fit(fit_data, timeout=request_timeout)
                        store.mark_wahoo_synced_to_garmin(user_id, wahoo_id)
                        logger.info("[%s] Wahoo workout %s uploaded to Garmin (retry).", name, wahoo_id)
                    except Exception as exc:
                        if "duplicate" in str(exc).lower() or "conflict" in str(exc).lower():
                            logger.warning("[%s] Garmin upload skipped for %s (likely duplicate): %s", name, wahoo_id, exc)
                            store.mark_wahoo_synced_to_garmin(user_id, wahoo_id)
                        else:
                            logger.error("[%s] Garmin upload retry failed for %s: %s", name, wahoo_id, exc)

                if existing["caldav_pushed"] and existing["mastodon_posted"]:
                    continue

            # ── CalDAV (optional) ─────────────────────────────────────────
            if caldav_enabled and caldav_pusher and not activity_row.get("caldav_pushed"):
                try:
                    caldav_pusher.push(activity_row)
                    store.mark_caldav_pushed(user_id, wahoo_id)
                    logger.info("[%s] CalDAV event created: %s", name, wahoo_id)
                except Exception as exc:
                    logger.error("[%s] CalDAV push failed for %s: %s", name, wahoo_id, exc)

            # ── Mastodon DM ───────────────────────────────────────────────
            skip_mastodon = (
                mastodon_max_age_days is not None
                and act_time is not None
                and act_time < datetime.now(timezone.utc) - timedelta(days=mastodon_max_age_days)
            )
            suppress_patterns = [p.lower() for p in user_cfg.get("mastodon_suppress_types", [])]
            act_type = (activity_row.get("activity_type") or "").lower()
            if suppress_patterns and any(fnmatch.fnmatch(act_type, p) for p in suppress_patterns):
                if not activity_row.get("mastodon_posted"):
                    logger.info(
                        "[%s] Mastodon post suppressed for activity type '%s' (%s).",
                        name, act_type, wahoo_id,
                    )
                    store.mark_mastodon_posted(user_id, wahoo_id, status_id=None)
                skip_mastodon = True
            if handle and not activity_row.get("mastodon_posted") and not skip_mastodon:
                try:
                    status_id = bot.post_activity(handle, activity_row, map_path,
                                                  visibility=_mastodon_visibility(user_cfg.get("mastodon_public", False)),
                                                  extra_mentions=_parse_extra_mentions(user_cfg.get("mastodon_add_mention")))
                    store.mark_mastodon_posted(user_id, wahoo_id, status_id=status_id)
                    if mastodon_post_delay_s > 0:
                        time.sleep(mastodon_post_delay_s)
                except Exception as exc:
                    logger.error("[%s] Mastodon post failed for %s: %s", name, wahoo_id, exc)

            processed += 1

        store.finish_sync_run(run_id, found, processed, "success")
        if skipped_401:
            logger.info("[%s] Wahoo sync complete. %d found / %d processed / %d permanently skipped (401).", name, found, processed, skipped_401)
        else:
            logger.info("[%s] Wahoo sync complete. %d found / %d processed.", name, found, processed)

    except WahooAuthError as exc:
        store.finish_sync_run(run_id, found, processed, "failed", str(exc))
        logger.error(
            "[%s] Wahoo authentication failed permanently — the refresh token may be expired "
            "or revoked. Re-run wahoo_auth.py to generate a new token. Error: %s",
            name, exc,
        )
    except Exception as exc:
        store.finish_sync_run(run_id, found, processed, "failed", str(exc))
        logger.error("[%s] Wahoo sync failed: %s", name, exc, exc_info=True)
    finally:
        wahoo.close()

    # ── KudosMachine — runs after every invocation ────────────────────────
    _visibility = _mastodon_visibility(user_cfg.get("mastodon_public", False))
    if kudos_machine is not None and handle and not user_cfg.get("suppressKudos", False) and _visibility != "direct":
        try:
            kudos_machine.process_user(
                user_id, handle, store, max_age_days=mastodon_max_age_days,
                visibility=_visibility,
            )
        except Exception as exc:
            logger.error("[%s] KudosMachine failed: %s", name, exc)


def run(config_path: str) -> None:
    lock_fd = _acquire_lock()
    try:
        _run_inner(config_path)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _is_paused(sync_cfg: dict) -> bool:
    """Return True if the current local time falls inside the configured pause window."""
    pause_start = sync_cfg.get("pause_start")
    pause_end = sync_cfg.get("pause_end")
    if not pause_start or not pause_end:
        return False

    now = datetime.now().time()
    start = datetime.strptime(pause_start, "%H:%M").time()
    end = datetime.strptime(pause_end, "%H:%M").time()

    if start <= end:
        # e.g. 08:00–12:00
        return start <= now < end
    else:
        # wraps midnight, e.g. 22:00–06:00
        return now >= start or now < end


def _run_inner(config_path: str) -> None:
    cfg = load_config(config_path)

    sync_cfg_early = cfg.get("sync", {})
    if _is_paused(sync_cfg_early):
        logger.info(
            "Sync paused (pause window %s–%s). Skipping this cycle.",
            sync_cfg_early["pause_start"], sync_cfg_early["pause_end"],
        )
        return

    # Safety ceiling: cap any socket that lacks its own timeout so a hung
    # connection cannot block the process forever.  Set higher than the
    # per-client timeout to avoid interfering with legitimate slow operations
    # (e.g. large GPX downloads).
    request_timeout_cfg = cfg.get("sync", {}).get("request_timeout_s", 30)
    socket.setdefaulttimeout(max(request_timeout_cfg * 4, 120))

    storage_cfg = cfg.get("storage", {})
    if level_str := storage_cfg.get("log_level"):
        _set_log_level(level_str)
    if log_file := storage_cfg.get("log_file"):
        _configure_log_file(log_file)

    sync_cfg        = cfg.get("sync", {})
    lookback_days   = sync_cfg.get("lookback_days", 30)
    request_timeout = sync_cfg.get("request_timeout_s", 30)
    gpx_max_age_days        = sync_cfg.get("gpx_max_age_days", None)
    fit_max_age_days        = sync_cfg.get("fit_max_age_days", None)
    mastodon_max_age_days   = sync_cfg.get("mastodon_max_age_days", None)
    mastodon_post_delay_s   = float(sync_cfg.get("mastodon_post_delay_s", 2.0))

    store: ActivityStore | None = None
    try:
        store = ActivityStore(
            db_path=storage_cfg.get("db_path",   "/data/garmin_nostra.db"),
            gpx_dir=storage_cfg.get("gpx_dir",   "/data/gpx"),
            fit_dir=storage_cfg.get("fit_dir",   "/data/fit"),
            map_dir=storage_cfg.get("map_dir",   "/data/maps"),
            token_dir=storage_cfg.get("token_dir", "/data/tokens"),
        )
        bot_cfg = cfg["bot"]
        bot = MastodonBot(
            api_base_url=bot_cfg["mastodon_api_base_url"],
            access_token=bot_cfg["mastodon_access_token"],
            request_timeout=request_timeout,
        )

        kudos_machine = KudosMachine(
            bot=bot,
            custom_template=bot_cfg.get("kudosCustom") or None,
            post_delay_s=mastodon_post_delay_s,
        )

        caldav_pusher = _build_caldav_pusher(cfg, timeout=request_timeout)

        users = cfg.get("users", [])
        if not users:
            logger.warning("No users found in configuration.")

        for user_cfg in users:
            source = user_cfg.get("source", "garmin").lower()
            try:
                if source == "wahoo":
                    missing = [
                        k for k in ("wahoo_client_id", "wahoo_client_secret", "wahoo_refresh_token")
                        if not user_cfg.get(k)
                    ]
                    if missing:
                        logger.error(
                            "User %s has source='wahoo' but is missing: %s",
                            user_cfg.get("name"), ", ".join(missing),
                        )
                        continue
                    process_user_wahoo(
                        user_cfg, store, bot, caldav_pusher, lookback_days,
                        request_timeout, fit_max_age_days, mastodon_max_age_days,
                        mastodon_post_delay_s, kudos_machine,
                    )
                elif source == "both":
                    # Validate both credential sets
                    wahoo_missing = [
                        k for k in ("wahoo_client_id", "wahoo_client_secret", "wahoo_refresh_token")
                        if not user_cfg.get(k)
                    ]
                    garmin_missing = [
                        k for k in ("garmin_username", "garmin_password")
                        if not user_cfg.get(k)
                    ]
                    if wahoo_missing or garmin_missing:
                        logger.error(
                            "User %s has source='%s' but is missing credentials: %s",
                            user_cfg.get("name"), source,
                            ", ".join(wahoo_missing + garmin_missing),
                        )
                        continue
                    # Wahoo runs first so its activities are in the DB before
                    # Garmin dedup checks run. Activities are tagged [Wahoo] /
                    # [Garmin] to make the source visible in Mastodon posts.
                    # Garmin skips any activity whose start time matches a
                    # Wahoo entry already in the DB (cross-source dedup).
                    # Each side is isolated — a Wahoo failure must not prevent
                    # the Garmin sync from running.
                    try:
                        process_user_wahoo(
                            user_cfg, store, bot, caldav_pusher, lookback_days,
                            request_timeout, fit_max_age_days, mastodon_max_age_days,
                            mastodon_post_delay_s, kudos_machine,
                        )
                    except Exception as exc:
                        logger.error("[%s] Wahoo sync failed, continuing with Garmin: %s",
                                     user_cfg.get("name"), exc)
                    process_user(
                        user_cfg, store, bot, caldav_pusher, lookback_days,
                        request_timeout, gpx_max_age_days, fit_max_age_days,
                        mastodon_max_age_days, mastodon_post_delay_s, kudos_machine,
                        tag_source=True, dedup_with_wahoo=True, skip_kudos=True,
                    )
                elif source == "garmin":
                    process_user(
                        user_cfg, store, bot, caldav_pusher, lookback_days,
                        request_timeout, gpx_max_age_days, fit_max_age_days,
                        mastodon_max_age_days, mastodon_post_delay_s, kudos_machine,
                    )
                else:
                    logger.error(
                        "User %s has unrecognised source=%r — valid values are "
                        "'garmin', 'wahoo', 'both'. Skipping.",
                        user_cfg.get("name"), source,
                    )
            except Exception as exc:
                logger.error("Unhandled error for user %s: %s", user_cfg.get("name"), exc)
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/app/config.toml"
    run(config_path)
