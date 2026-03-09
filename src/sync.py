"""garmin-nostra — main sync orchestrator (multi-user)."""

from __future__ import annotations

import logging
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("sync")


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _build_caldav_pusher(cfg: dict) -> CalDAVPusher | None:
    caldav_cfg = cfg.get("caldav", {})
    if not caldav_cfg.get("url"):
        return None
    return CalDAVPusher(
        url=caldav_cfg["url"],
        username=caldav_cfg["username"],
        password=caldav_cfg["password"],
        calendar_name=caldav_cfg.get("calendar_name", "Fitness"),
    )


def process_user(
    user_cfg: dict,
    store: ActivityStore,
    bot: MastodonBot,
    caldav_pusher: CalDAVPusher | None,
    lookback_days: int,
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
    )

    run_id = store.start_sync_run(user_id)
    found = processed = 0

    try:
        activities = garmin.get_activities_since(since)
        found = len(activities)

        # Process oldest-first so last_sync advances monotonically
        activities.sort(
            key=lambda a: a.get("startTimeGMT") or a.get("startTimeLocal") or ""
        )

        for act in activities:
            garmin_id = str(act["activityId"])

            # Check DB for this activity
            existing = store.get_activity(user_id, garmin_id)

            if existing is None:
                # ── New activity — download, store, then integrate ──────────
                gpx_data = None
                gpx_path = None
                try:
                    gpx_data = garmin.get_gpx(garmin_id)
                    gpx_path = store.save_gpx(name, garmin_id, gpx_data)
                except Exception as exc:
                    logger.warning("[%s] GPX download failed for %s: %s", name, garmin_id, exc)

                map_path = None
                if gpx_data:
                    map_path = store.map_path(name, garmin_id)
                    map_path = render_map(gpx_data, map_path)

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
                try:
                    caldav_pusher.push(activity_row)
                    store.mark_caldav_pushed(user_id, garmin_id)
                    logger.info("[%s] CalDAV-Eintrag erstellt: %s", name, garmin_id)
                except Exception as exc:
                    logger.error("[%s] CalDAV fehlgeschlagen für %s: %s", name, garmin_id, exc)

            # ── Mastodon DM ────────────────────────────────────────────────
            if handle and not activity_row.get("mastodon_posted"):
                try:
                    bot.post_activity(handle, activity_row, map_path)
                    store.mark_mastodon_posted(user_id, garmin_id)
                except Exception as exc:
                    logger.error("[%s] Mastodon fehlgeschlagen für %s: %s", name, garmin_id, exc)

            processed += 1

        store.finish_sync_run(run_id, found, processed, "success")
        logger.info("[%s] Sync abgeschlossen. %d neu / %d verarbeitet.", name, found, processed)

    except Exception as exc:
        store.finish_sync_run(run_id, found, processed, "failed", str(exc))
        logger.error("[%s] Sync fehlgeschlagen: %s", name, exc, exc_info=True)


def run(config_path: str) -> None:
    cfg = load_config(config_path)

    storage_cfg = cfg.get("storage", {})
    store = ActivityStore(
        db_path=storage_cfg.get("db_path",   "/data/garmin_nostra.db"),
        gpx_dir=storage_cfg.get("gpx_dir",   "/data/gpx"),
        map_dir=storage_cfg.get("map_dir",   "/data/maps"),
        token_dir=storage_cfg.get("token_dir", "/data/tokens"),
    )

    bot_cfg = cfg["bot"]
    bot = MastodonBot(
        api_base_url=bot_cfg["mastodon_api_base_url"],
        access_token=bot_cfg["mastodon_access_token"],
    )

    caldav_pusher = _build_caldav_pusher(cfg)

    sync_cfg     = cfg.get("sync", {})
    lookback_days = sync_cfg.get("lookback_days", 30)

    users = cfg.get("users", [])
    if not users:
        logger.warning("Keine Benutzer in der Konfiguration gefunden.")

    for user_cfg in users:
        try:
            process_user(user_cfg, store, bot, caldav_pusher, lookback_days)
        except Exception as exc:
            logger.error("Unbehandelter Fehler bei Benutzer %s: %s", user_cfg.get("name"), exc)

    store.close()


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/app/config.toml"
    run(config_path)
