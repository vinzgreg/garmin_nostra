"""Garmin Connect client — fetches activities and GPX data."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from garminconnect import Garmin, GarminConnectAuthenticationError

logger = logging.getLogger(__name__)


class GarminClient:
    """
    Thin wrapper around the garminconnect library.

    Token caching is handled per-user via *tokenstore* (a directory path).
    On first run the library authenticates with username/password and saves
    OAuth tokens to *tokenstore*. Subsequent runs load the saved tokens,
    avoiding repeated credential round-trips.
    """

    def __init__(
        self,
        username: str,
        password: str,
        tokenstore: str | Path | None = None,
    ) -> None:
        self._username   = username
        self._password   = password
        self._tokenstore = str(tokenstore) if tokenstore else None
        self._client: Garmin | None = None

    def connect(self) -> None:
        logger.info("Connecting to Garmin Connect for %s …", self._username)
        self._client = Garmin(self._username, self._password)
        try:
            self._client.login(self._tokenstore)
            logger.info("Authenticated (token store: %s).", self._tokenstore or "none")
        except GarminConnectAuthenticationError as exc:
            raise RuntimeError(
                f"Garmin authentication failed for {self._username}: {exc}"
            ) from exc
        except Exception as exc:
            logger.warning("Token login failed (%s) — retrying with credentials.", exc)
            self._client = Garmin(self._username, self._password)
            self._client.login()
            if self._tokenstore:
                Path(self._tokenstore).mkdir(parents=True, exist_ok=True)
                try:
                    self._client.garth.dump(self._tokenstore)
                except Exception as dump_exc:
                    logger.debug("Could not save tokens: %s", dump_exc)
            logger.info("Authenticated with credentials.")

    def _client_(self) -> Garmin:
        if self._client is None:
            self.connect()
        return self._client  # type: ignore[return-value]

    def get_activities_since(self, since: datetime) -> list[dict[str, Any]]:
        """Return activities with a start time strictly after *since* (UTC-aware)."""
        client = self._client_()
        raw = client.get_activities(0, 100)
        result = []
        for act in raw:
            start_str = act.get("startTimeGMT") or act.get("startTimeLocal", "")
            try:
                start = datetime.fromisoformat(
                    start_str.replace(" ", "T")
                ).replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                continue
            if start > since:
                result.append(act)
        logger.info(
            "Found %d new activities since %s for %s.",
            len(result), since.isoformat(), self._username,
        )
        return result

    def get_gpx(self, activity_id: int | str) -> bytes:
        """Download GPX bytes for *activity_id*."""
        client = self._client_()
        return client.download_activity(
            activity_id, dl_fmt=client.ActivityDownloadFormat.GPX
        )
