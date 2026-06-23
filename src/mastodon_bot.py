"""Mastodon bot — sends private activity DMs to users."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from mastodon import Mastodon

from format import build_mastodon_message

logger = logging.getLogger(__name__)

# Media uploads occasionally fail transiently (rate limit, hoster storage
# hiccup, timeout). Retry a couple of times before giving up and posting
# without the attachment.
_MEDIA_UPLOAD_RETRIES = 2      # additional attempts after the first
_MEDIA_UPLOAD_BACKOFF_S = 3    # wait between attempts


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

    def _upload_media(self, image_path: Path, description: str) -> str | None:
        """Upload a PNG to Mastodon, retrying transient failures with a short
        backoff. Returns the media id, or None if every attempt fails — the
        caller then posts without this attachment rather than losing the post.
        """
        attempts = _MEDIA_UPLOAD_RETRIES + 1
        for attempt in range(1, attempts + 1):
            try:
                with open(image_path, "rb") as fh:
                    media = self._client.media_post(
                        fh,
                        mime_type="image/png",
                        description=description,
                    )
                logger.debug("Media uploaded: %s", image_path)
                return media["id"]
            except Exception as exc:
                if attempt < attempts:
                    logger.warning(
                        "Media upload failed (attempt %d/%d) for %s: %s — retrying in %ds.",
                        attempt, attempts, image_path, exc, _MEDIA_UPLOAD_BACKOFF_S,
                    )
                    time.sleep(_MEDIA_UPLOAD_BACKOFF_S)
                else:
                    logger.error(
                        "Media upload permanently failed after %d attempts for %s: %s "
                        "— posting without it.",
                        attempts, image_path, exc,
                    )
        return None

    def post_activity(
        self,
        mastodon_handle: str,
        activity: dict[str, Any],
        map_image_path: Path | None = None,
        visibility: str = "direct",
        extra_mentions: list[str] | None = None,
        elevation_profile_path: Path | None = None,
    ) -> str | None:
        """
        Send a mention to *mastodon_handle* with the activity summary.
        Attaches a map image if *map_image_path* exists, and an elevation
        profile image if *elevation_profile_path* exists.
        *visibility* is passed directly to the Mastodon API:
          "public"   — on the public timeline
          "unlisted" — accessible via link, shown to followers
          "direct"   — DM, only visible to the mentioned user
        Returns the Mastodon status ID of the posted status, or None on failure.
        """
        text = build_mastodon_message(mastodon_handle, activity, extra_mentions=extra_mentions)
        logger.debug("Mastodon message:\n%s", text)

        media_ids: list = []
        if map_image_path and map_image_path.exists():
            media_id = self._upload_media(map_image_path, "Streckenkarte der Aktivität")
            if media_id:
                media_ids.append(media_id)

        if elevation_profile_path and elevation_profile_path.exists():
            media_id = self._upload_media(elevation_profile_path, "Höhenprofil der Aktivität")
            if media_id:
                media_ids.append(media_id)

        response = self._client.status_post(
            text,
            media_ids=media_ids or None,
            visibility=visibility,
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

    def post_reply(self, text: str, in_reply_to_id: str, visibility: str = "direct") -> None:
        """Post a reply to *in_reply_to_id*."""
        self._client.status_post(
            text,
            in_reply_to_id=in_reply_to_id,
            visibility=visibility,
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
