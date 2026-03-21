"""garmin-nostra — main sync orchestrator (multi-user)."""

from __future__ import annotations

import fcntl
import fnmatch
import logging
import os
import socket
import sys
import time
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
from kudos_machine import KudosMachine
from map_render import render_map

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT,
)
logger = logging.getLogger("sync")

_LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "error": logging.ERROR}


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
) -> None:
    name   = user_cfg["name"]
    handle = user_cfg.get("mastodon_handle")
    caldav_enabled = user_cfg.get("caldav_enabled", False)

    user_id = store.upsert_user(user_cfg)

    # Determine sync window
    since    = store.get_last_sync_time(user_id)
    earliest = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    since    = max(since, earliest)
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
                activity_row = store.save_activity(user_id, act, gpx_path, fit_path)
                logger.info("[%s] Neue Aktivität gespeichert: %s", name, garmin_id)
            else:
                # ── Known activity — check if any integration needs retry ───
                activity_row = existing
                logger.debug("[%s] Aktivität %s bereits bekannt.", name, garmin_id)
                map_path = store.map_path(name, garmin_id)
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
                        "[%s] Mastodon-Post unterdrückt für Aktivitätstyp '%s' (%s).",
                        name, act_type, garmin_id,
                    )
                    store.mark_mastodon_posted(user_id, garmin_id, status_id=None)
                skip_mastodon = True
            if handle and not activity_row.get("mastodon_posted") and not skip_mastodon:
                logger.debug("[%s] Posting Mastodon for %s", name, garmin_id)
                try:
                    status_id = bot.post_activity(handle, activity_row, map_path,
                                                  public=user_cfg.get("mastodon_public", False))
                    store.mark_mastodon_posted(user_id, garmin_id, status_id=status_id)
                    if mastodon_post_delay_s > 0:
                        time.sleep(mastodon_post_delay_s)
                except Exception as exc:
                    logger.error("[%s] Mastodon fehlgeschlagen für %s: %s", name, garmin_id, exc)
            logger.debug("[%s] Activity %s done", name, garmin_id)

            processed += 1

        store.finish_sync_run(run_id, found, processed, "success")
        logger.info("[%s] Sync abgeschlossen. %d gefunden / %d verarbeitet.", name, found, processed)

    except Exception as exc:
        store.finish_sync_run(run_id, found, processed, "failed", str(exc))
        logger.error("[%s] Sync fehlgeschlagen: %s", name, exc, exc_info=True)

    # ── KudosMachine — runs after every invocation, even if Garmin sync failed ─
    if kudos_machine is not None and handle and not user_cfg.get("suppressKudos", False):
        logger.debug("[%s] Running KudosMachine.", name)
        try:
            kudos_machine.process_user(
                user_id, handle, store, max_age_days=mastodon_max_age_days,
                public=user_cfg.get("mastodon_public", False),
            )
        except Exception as exc:
            logger.error("[%s] KudosMachine fehlgeschlagen: %s", name, exc)


def run(config_path: str) -> None:
    lock_fd = _acquire_lock()
    cfg = load_config(config_path)

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

    store = ActivityStore(
        db_path=storage_cfg.get("db_path",   "/data/garmin_nostra.db"),
        gpx_dir=storage_cfg.get("gpx_dir",   "/data/gpx"),
        fit_dir=storage_cfg.get("fit_dir",   "/data/fit"),
        map_dir=storage_cfg.get("map_dir",   "/data/maps"),
        token_dir=storage_cfg.get("token_dir", "/data/tokens"),
    )

    sync_cfg        = cfg.get("sync", {})
    lookback_days   = sync_cfg.get("lookback_days", 30)
    request_timeout = sync_cfg.get("request_timeout_s", 30)
    gpx_max_age_days        = sync_cfg.get("gpx_max_age_days", None)
    fit_max_age_days        = sync_cfg.get("fit_max_age_days", None)
    mastodon_max_age_days   = sync_cfg.get("mastodon_max_age_days", None)
    mastodon_post_delay_s   = float(sync_cfg.get("mastodon_post_delay_s", 2.0))

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
        logger.warning("Keine Benutzer in der Konfiguration gefunden.")

    for user_cfg in users:
        try:
            process_user(user_cfg, store, bot, caldav_pusher, lookback_days, request_timeout, gpx_max_age_days, fit_max_age_days, mastodon_max_age_days, mastodon_post_delay_s, kudos_machine)
        except Exception as exc:
            logger.error("Unbehandelter Fehler bei Benutzer %s: %s", user_cfg.get("name"), exc)

    store.close()
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/app/config.toml"
    run(config_path)
