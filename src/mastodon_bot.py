"""Mastodon bot — sends private activity DMs to users."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mastodon import Mastodon

from format import build_mastodon_message

logger = logging.getLogger(__name__)


class MastodonBot:
    """
    Single bot account that sends direct mentions to individual users.

    Visibility is set to ``direct`` so the post appears only in the
    recipient's DM timeline, not on the public timeline.
    """

    def __init__(self, api_base_url: str, access_token: str, request_timeout: int = 30) -> None:
        self._client = Mastodon(
            access_token=access_token,
            api_base_url=api_base_url,
            request_timeout=request_timeout,
            ratelimit_method="throw",
        )
        logger.info("Mastodon bot initialised (%s).", api_base_url)

    def post_activity(
        self,
        mastodon_handle: str,
        activity: dict[str, Any],
        map_image_path: Path | None = None,
        public: bool = False,
    ) -> str | None:
        """
        Send a mention to *mastodon_handle* with the activity summary.
        Attaches a map image if *map_image_path* exists.
        Use *public=True* for a public post, otherwise posts as unlisted.
        Returns the Mastodon status ID of the posted status, or None on failure.
        """
        text = build_mastodon_message(mastodon_handle, activity)
        logger.debug("Mastodon message:\n%s", text)

        media_ids: list = []
        if map_image_path and map_image_path.exists():
            try:
                with open(map_image_path, "rb") as fh:
                    media = self._client.media_post(
                        fh,
                        mime_type="image/png",
                        description="Streckenkarte der Aktivität",
                    )
                media_ids.append(media["id"])
                logger.debug("Map image uploaded: %s", map_image_path)
            except Exception as exc:
                logger.warning("Map image upload failed: %s", exc)

        response = self._client.status_post(
            text,
            media_ids=media_ids or None,
            visibility="public" if public else "unlisted",
        )
        logger.info(
            "Mastodon-Post gesendet an %s für Aktivität %s.",
            mastodon_handle,
            activity.get("garmin_activity_id"),
        )
        return str(response["id"]) if response and response.get("id") else None

    def get_favourited_by(self, status_id: str) -> list[dict]:
        """Return list of accounts that have favourited *status_id*."""
        return self._client.status_favourited_by(status_id) or []

    def post_reply(self, text: str, in_reply_to_id: str, public: bool = False) -> None:
        """Post a reply to *in_reply_to_id*."""
        self._client.status_post(
            text,
            in_reply_to_id=in_reply_to_id,
            visibility="public" if public else "unlisted",
        )


# ── Standalone test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    from format import build_mastodon_message

    logging.basicConfig(level=logging.DEBUG)
    fake = {
        "garmin_activity_id": "99999",
        "activity_name":      "Morgenlauf",
        "activity_type":      "running",
        "start_time_utc":     "2025-03-04 07:15:00",
        "duration_s":         2732.0,
        "distance_m":         8500.0,
        "elevation_gain_m":   115.0,
        "avg_hr":             148,
        "avg_power_w":        None,
    }
    print(build_mastodon_message("@alice@fosstodon.org", fake))
    print()
    fake2 = {
        "garmin_activity_id": "88888",
        "activity_name":      "Nachmittagsfahrt",
        "activity_type":      "cycling",
        "start_time_utc":     "2025-03-04 14:30:00",
        "duration_s":         4360.0,
        "distance_m":         38200.0,
        "elevation_gain_m":   540.0,
        "avg_hr":             142,
        "avg_power_w":        210.0,
    }
    print(build_mastodon_message("@bob@mastodon.social", fake2))
