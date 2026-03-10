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

    def get_activities_since(self, since: datetime, page_size: int = 100, timeout: int = 120) -> list[dict[str, Any]]:
        """Return all activities with a start time strictly after *since* (UTC-aware).

        Paginates through the Garmin API until it reaches activities older than *since*
        or an empty page. *timeout* caps the entire pagination loop.
        """
        import signal

        def _timeout_handler(signum, frame):
            raise TimeoutError(f"get_activities_since timed out after {timeout}s")

        client  = self._client_()
        result  = []
        start   = 0

        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(timeout)
        try:
            while True:
                page = client.get_activities(start, page_size)
                if not page:
                    break

                page_had_match = False
                for act in page:
                    start_str = act.get("startTimeGMT") or act.get("startTimeLocal", "")
                    try:
                        act_time = datetime.fromisoformat(
                            start_str.replace(" ", "T")
                        ).replace(tzinfo=timezone.utc)
                    except (ValueError, AttributeError):
                        continue
                    if act_time > since:
                        result.append(act)
                        page_had_match = True

                # If no activity on this page was newer than *since*, we're done
                if not page_had_match:
                    break

                start += page_size
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        logger.info(
            "Found %d activities since %s for %s.",
            len(result), since.isoformat(), self._username,
        )
        return result

    def get_gpx(self, activity_id: int | str, timeout: int = 30) -> bytes:
        """Download GPX bytes for *activity_id* with a hard *timeout* in seconds.

        Runs the download in a worker thread so the timeout is enforced even
        when the underlying httpx/garth library ignores socket defaults.
        The worker thread is daemon so it does not block process exit.
        """
        import concurrent.futures

        client = self._client_()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(
                client.download_activity,
                activity_id,
                dl_fmt=client.ActivityDownloadFormat.GPX,
            )
            try:
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                raise TimeoutError(f"GPX download timed out after {timeout}s")
