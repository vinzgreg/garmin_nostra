"""Garmin Connect client — fetches activities, GPX and FIT data."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

logger = logging.getLogger(__name__)


class GarminRateLimitError(Exception):
    """Raised when a Garmin SSO credential login is blocked by a 429 response."""


# How long to wait before retrying a credential login after a 429 response.
_RATE_LIMIT_BACKOFF_HOURS = 2


def _backoff_path(tokenstore: str | None) -> Path | None:
    if not tokenstore:
        return None
    return Path(tokenstore) / ".rate_limited_until"


def _read_backoff_until(tokenstore: str | None) -> datetime | None:
    path = _backoff_path(tokenstore)
    if path is None or not path.exists():
        return None
    try:
        return datetime.fromisoformat(path.read_text().strip())
    except Exception:
        return None


def _write_backoff(tokenstore: str | None) -> None:
    path = _backoff_path(tokenstore)
    if path is None:
        return
    until = datetime.now(timezone.utc) + timedelta(hours=_RATE_LIMIT_BACKOFF_HOURS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(until.isoformat())
    logger.warning("Garmin credential login rate-limited — backing off until %s.", until.isoformat())


def _clear_backoff(tokenstore: str | None) -> None:
    path = _backoff_path(tokenstore)
    if path is not None:
        path.unlink(missing_ok=True)


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
        self._client: Garmin | None = None

    def connect(self) -> None:
        logger.info("Connecting to Garmin Connect for %s …", self._username)
        self._client = None

        backoff_until = _read_backoff_until(self._tokenstore)
        if backoff_until is not None and datetime.now(timezone.utc) < backoff_until:
            raise GarminRateLimitError(
                f"Garmin SSO is rate-limiting {self._username} — "
                f"will retry after {backoff_until.isoformat()}."
            )

        try:
            client = Garmin(self._username, self._password)
            client.login(self._tokenstore)
            _clear_backoff(self._tokenstore)
            logger.info("Authenticated for %s (token store: %s).",
                         self._username, self._tokenstore or "none")
            self._client = client
        except GarminConnectTooManyRequestsError:
            _write_backoff(self._tokenstore)
            raise GarminRateLimitError(
                f"Garmin SSO returned 429 for {self._username} — "
                f"credential login blocked for {_RATE_LIMIT_BACKOFF_HOURS}h."
            ) from None
        except GarminConnectAuthenticationError as exc:
            if isinstance(exc.__cause__, GarminConnectTooManyRequestsError) or "429" in str(exc):
                _write_backoff(self._tokenstore)
                raise GarminRateLimitError(
                    f"Garmin SSO returned 429 for {self._username} — "
                    f"credential login blocked for {_RATE_LIMIT_BACKOFF_HOURS}h."
                ) from None
            raise
        except Exception:
            self._client = None
            raise

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
        import tempfile

        client = self._client_()

        def _upload() -> None:
            with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as tmp:
                tmp.write(fit_data)
                tmp_path = tmp.name
            try:
                client.upload_activity(tmp_path)
            finally:
                Path(tmp_path).unlink(missing_ok=True)

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
