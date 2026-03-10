"""garmin-nostra — main sync orchestrator (multi-user)."""

from __future__ import annotations

import fcntl
import logging
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-reattr]

from garmin import GarminClient
from storage import ActivityStore
from caldav_push import CalDAVPusher
from mastodon_bot import MastodonBot
from map_render import render_map

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"

logging.basicConfig(
    level=logging.DEBUG,
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT,
)
logger = logging.getLogger("sync")


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


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


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


def process_user(
    user_cfg: dict,
    store: ActivityStore,
    bot: MastodonBot,
    caldav_pusher: CalDAVPusher | None,
    lookback_days: int,
    request_timeout: int = 30,
    gpx_max_age_days: int | None = None,
) -> None:
    name   = user_cfg["name"]
    handle = user_cfg.get("mastodon_handle")
    caldav_enabled = user_cfg.get("caldav_enabled", False)

    user_id = store.upsert_user(user_cfg)

    # Determine sync window
    since    = store.get_last_sync_time(user_id)
    earliest = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    since    = min(since, earliest)
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

            if existing is None:
                # ── New activity — download, store, then integrate ──────────
                gpx_data = None
                gpx_path = None

                start_str = act.get("startTimeGMT") or act.get("startTimeLocal", "")
                try:
                    act_time = datetime.fromisoformat(
                        start_str.replace(" ", "T")
                    ).replace(tzinfo=timezone.utc)
                except (ValueError, AttributeError):
                    act_time = None

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

                map_path = None
                if gpx_data:
                    map_path = store.map_path(name, garmin_id)
                    map_path = render_map(gpx_data, map_path, timeout=request_timeout)

                logger.debug("[%s] Saving activity %s to DB", name, garmin_id)
                activity_row = store.save_activity(user_id, act, gpx_path)
                logger.info("[%s] Neue Aktivität gespeichert: %s", name, garmin_id)
            else:
                # ── Known activity — check if any integration needs retry ───
                activity_row = existing
                logger.debug("[%s] Aktivität %s bereits bekannt.", name, garmin_id)
                map_path = Path(existing["gpx_path"]).with_suffix(".png") \
                    if existing.get("gpx_path") else None
                # Only retry if not yet done
                if existing["caldav_pushed"] and existing["mastodon_posted"]:
                    continue

            # ── CalDAV (optional) ──────────────────────────────────────────
            if caldav_enabled and caldav_pusher and not activity_row.get("caldav_pushed"):
                logger.debug("[%s] Pushing CalDAV for %s", name, garmin_id)
                try:
                    caldav_pusher.push(activity_row)
                    store.mark_caldav_pushed(user_id, garmin_id)
                    logger.info("[%s] CalDAV-Eintrag erstellt: %s", name, garmin_id)
                except Exception as exc:
                    logger.error("[%s] CalDAV fehlgeschlagen für %s: %s", name, garmin_id, exc)

            # ── Mastodon DM ────────────────────────────────────────────────
            if handle and not activity_row.get("mastodon_posted"):
                logger.debug("[%s] Posting Mastodon for %s", name, garmin_id)
                try:
                    bot.post_activity(handle, activity_row, map_path,
                                      public=user_cfg.get("mastodon_public", False))
                    store.mark_mastodon_posted(user_id, garmin_id)
                except Exception as exc:
                    logger.error("[%s] Mastodon fehlgeschlagen für %s: %s", name, garmin_id, exc)
            logger.debug("[%s] Activity %s done", name, garmin_id)

            processed += 1

        store.finish_sync_run(run_id, found, processed, "success")
        logger.info("[%s] Sync abgeschlossen. %d neu / %d verarbeitet.", name, found, processed)

    except Exception as exc:
        store.finish_sync_run(run_id, found, processed, "failed", str(exc))
        logger.error("[%s] Sync fehlgeschlagen: %s", name, exc, exc_info=True)


def run(config_path: str) -> None:
    lock_fd = _acquire_lock()
    cfg = load_config(config_path)

    # Apply a process-wide socket timeout so every HTTP call (Garmin, CalDAV,
    # OSM tiles, Mastodon) is capped at the configured value.  This works at
    # the OS level and is more reliable than SIGALRM with httpx/requests.
    socket.setdefaulttimeout(cfg.get("sync", {}).get("request_timeout_s", 30))

    storage_cfg = cfg.get("storage", {})
    if log_file := storage_cfg.get("log_file"):
        _configure_log_file(log_file)

    store = ActivityStore(
        db_path=storage_cfg.get("db_path",   "/data/garmin_nostra.db"),
        gpx_dir=storage_cfg.get("gpx_dir",   "/data/gpx"),
        map_dir=storage_cfg.get("map_dir",   "/data/maps"),
        token_dir=storage_cfg.get("token_dir", "/data/tokens"),
    )

    sync_cfg        = cfg.get("sync", {})
    lookback_days   = sync_cfg.get("lookback_days", 30)
    request_timeout = sync_cfg.get("request_timeout_s", 30)
    gpx_max_age_days = sync_cfg.get("gpx_max_age_days", None)

    bot_cfg = cfg["bot"]
    bot = MastodonBot(
        api_base_url=bot_cfg["mastodon_api_base_url"],
        access_token=bot_cfg["mastodon_access_token"],
        request_timeout=request_timeout,
    )

    caldav_pusher = _build_caldav_pusher(cfg, timeout=request_timeout)

    users = cfg.get("users", [])
    if not users:
        logger.warning("Keine Benutzer in der Konfiguration gefunden.")

    for user_cfg in users:
        try:
            process_user(user_cfg, store, bot, caldav_pusher, lookback_days, request_timeout, gpx_max_age_days)
        except Exception as exc:
            logger.error("Unbehandelter Fehler bei Benutzer %s: %s", user_cfg.get("name"), exc)

    store.close()
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/app/config.toml"
    run(config_path)
