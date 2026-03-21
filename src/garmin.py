"""Garmin Connect client — fetches activities, GPX and FIT data."""

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
        timeout: int = 30,
    ) -> None:
        self._username   = username
        self._password   = password
        self._tokenstore = str(tokenstore) if tokenstore else None
        self._timeout    = timeout
        self._client: Garmin | None = None

    def _apply_timeout(self, client: Garmin) -> None:
        """Set the garth request timeout on *client* if the API allows it."""
        try:
            client.garth.configure(timeout=self._timeout)
        except Exception:
            try:
                client.garth.timeout = self._timeout
            except Exception:
                pass

    def connect(self) -> None:
        logger.info("Connecting to Garmin Connect for %s …", self._username)
        self._client = Garmin(self._username, self._password)
        try:
            self._client.login(self._tokenstore)
            self._apply_timeout(self._client)
            logger.info("Authenticated (token store: %s).", self._tokenstore or "none")
        except GarminConnectAuthenticationError as exc:
            raise RuntimeError(
                f"Garmin authentication failed for {self._username}: {exc}"
            ) from exc
        except Exception as exc:
            logger.warning("Token login failed (%s) — retrying with credentials.", exc)
            self._client = Garmin(self._username, self._password)
            self._client.login()
            self._apply_timeout(self._client)
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

        Uses a background thread so the timeout works regardless of which thread
        calls this method (unlike the previous SIGALRM approach).
        """
        import concurrent.futures

        client = self._client_()

        def _paginate() -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            start = 0
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

                if not page_had_match:
                    break

                start += page_size
            return result

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = ex.submit(_paginate)
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"get_activities_since timed out after {timeout}s")
        finally:
            ex.shutdown(wait=False)

        logger.info(
            "Found %d activities since %s for %s.",
            len(result), since.isoformat(), self._username,
        )
        return result

    def get_gpx(self, activity_id: int | str, timeout: int = 30) -> bytes:
        """Download GPX bytes for *activity_id* with a hard *timeout* in seconds."""
        import concurrent.futures

        client = self._client_()

        def _download() -> bytes:
            return client.download_activity(
                activity_id, dl_fmt=client.ActivityDownloadFormat.GPX
            )

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = ex.submit(_download)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"GPX download timed out after {timeout}s")
        finally:
            ex.shutdown(wait=False)

    def upload_fit(self, fit_data: bytes, timeout: int = 60) -> None:
        """Upload a FIT file to Garmin Connect with a hard *timeout* in seconds."""
        import concurrent.futures

        client = self._client_()

        def _upload() -> None:
            client.upload_activity(fit_data)

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = ex.submit(_upload)
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"FIT upload timed out after {timeout}s")
        finally:
            ex.shutdown(wait=False)

    def get_fit(self, activity_id: int | str, timeout: int = 30) -> bytes:
        """Download original FIT bytes for *activity_id* with a hard *timeout* in seconds.

        The Garmin API returns a zip archive containing the .fit file; this
        method returns the raw .fit bytes extracted from that archive.
        """
        import concurrent.futures
        import io
        import zipfile

        client = self._client_()

        def _download() -> bytes:
            zip_bytes = client.download_activity(
                activity_id, dl_fmt=client.ActivityDownloadFormat.ORIGINAL
            )
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                fit_names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
                if not fit_names:
                    raise ValueError("No .fit file found in downloaded archive")
                return zf.read(fit_names[0])

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = ex.submit(_download)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"FIT download timed out after {timeout}s")
        finally:
            ex.shutdown(wait=False)
