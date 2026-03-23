"""Wahoo Cloud API client — fetches workouts, FIT data, handles OAuth 2.0."""

from __future__ import annotations

import concurrent.futures
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_API_BASE = "https://api.wahooligan.com"
_TOKEN_URL = f"{_API_BASE}/oauth/token"


class WahooAuthError(Exception):
    """Raised when Wahoo OAuth authentication fails permanently."""


class WahooClient:
    """Wahoo Cloud API v1 client with automatic token refresh.

    *token_path* is a file that stores the OAuth tokens as JSON
    (access_token, refresh_token, expires_at).  On first use the caller
    must supply a valid *refresh_token* (obtained via wahoo_auth.py).
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_path: Path | str | None = None,
        timeout: int = 30,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._token_path = Path(token_path) if token_path else None
        self._timeout = timeout
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._session: requests.Session | None = None

        self._load_tokens()

    # ── Token management ──────────────────────────────────────────────────

    def _load_tokens(self) -> None:
        """Load cached tokens from disk if available."""
        if self._token_path and self._token_path.exists():
            try:
                data = json.loads(self._token_path.read_text())
                self._access_token = data.get("access_token")
                self._refresh_token = data.get("refresh_token", self._refresh_token)
                self._token_expires_at = float(data.get("expires_at", 0))
                logger.debug("Loaded Wahoo tokens from %s.", self._token_path)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load Wahoo tokens from %s: %s", self._token_path, exc)

    def _save_tokens(self) -> None:
        """Persist current tokens to disk."""
        if not self._token_path:
            return
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._token_expires_at,
        }
        try:
            tmp = self._token_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.rename(self._token_path)  # atomic on POSIX
            logger.debug("Saved Wahoo tokens to %s.", self._token_path)
        except OSError as exc:
            logger.warning("Could not save Wahoo tokens to %s: %s", self._token_path, exc)

    def _token_is_valid(self) -> bool:
        import time
        return (
            self._access_token is not None
            and self._token_expires_at > time.time() + 60  # 60s safety margin
        )

    def _refresh_access_token(self) -> None:
        """Exchange the refresh token for a new access token."""
        import time

        logger.info("Refreshing Wahoo access token …")
        resp = requests.post(
            _TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise WahooAuthError(
                f"Wahoo token refresh failed (HTTP {resp.status_code}): {resp.text}"
            )

        token_data = resp.json()
        if "access_token" not in token_data:
            raise WahooAuthError(
                f"Wahoo token response missing 'access_token': {list(token_data.keys())}"
            )
        self._access_token = token_data["access_token"]
        self._refresh_token = token_data.get("refresh_token", self._refresh_token)
        self._token_expires_at = time.time() + token_data.get("expires_in", 7200)
        self._save_tokens()
        logger.info("Wahoo access token refreshed successfully.")

    def _ensure_auth(self) -> None:
        """Ensure we have a valid access token, refreshing if needed."""
        logger.debug("Token valid: %s, expires_at: %s", self._token_is_valid(), self._token_expires_at)
        if not self._token_is_valid():
            self._refresh_access_token()

    def _get_session(self) -> requests.Session:
        """Return a requests session with the current auth header."""
        self._ensure_auth()
        if self._session is None:
            self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {self._access_token}"
        return self._session

    # ── API calls ─────────────────────────────────────────────────────────

    def get_workouts_since(
        self, since: datetime, per_page: int = 30, timeout: int = 120
    ) -> list[dict[str, Any]]:
        """Return all workouts with a start time strictly after *since* (UTC-aware).

        Paginates through the Wahoo API. Uses a background thread so the
        timeout works regardless of which thread calls this method.
        """
        session = self._get_session()

        # Wahoo API requires updated_after for the workouts endpoint
        updated_after = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        def _paginate() -> list[dict[str, Any]]:
            import time as _time
            result: list[dict[str, Any]] = []
            page = 1
            while True:
                params = {
                    "page": page,
                    "per_page": per_page,
                    "updated_after": updated_after,
                }
                resp = session.get(
                    f"{_API_BASE}/v1/workouts",
                    params=params,
                    timeout=self._timeout,
                )
                if resp.status_code == 401:
                    # Token expired mid-pagination — refresh and retry this page
                    self._refresh_access_token()
                    session.headers["Authorization"] = f"Bearer {self._access_token}"
                    resp = session.get(
                        f"{_API_BASE}/v1/workouts",
                        params=params,
                        timeout=self._timeout,
                    )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 30))
                    logger.warning("Wahoo rate-limited (429) on workout list, waiting %ds.", retry_after)
                    _time.sleep(retry_after)
                    resp = session.get(
                        f"{_API_BASE}/v1/workouts",
                        params=params,
                        timeout=self._timeout,
                    )
                if not resp.ok:
                    logger.error("Wahoo API error %s: %s", resp.status_code, resp.text)
                resp.raise_for_status()

                data = resp.json()
                workouts = data.get("workouts", [])
                if not workouts:
                    break

                result.extend(workouts)

                total = data.get("total", 0)
                if page * per_page >= total:
                    break
                page += 1

            return result

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = ex.submit(_paginate)
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"get_workouts_since timed out after {timeout}s")
        finally:
            ex.shutdown(wait=False)

        logger.info("Found %d Wahoo workouts since %s.", len(result), since.isoformat())
        return result

    def get_workout_summary(
        self, workout_id: int | str, timeout: int = 30
    ) -> dict[str, Any]:
        """Fetch the workout summary for a given workout ID."""
        session = self._get_session()

        def _fetch() -> dict[str, Any]:
            import time as _time
            url = f"{_API_BASE}/v1/workouts/{workout_id}/workout_summary"
            resp = session.get(url, timeout=self._timeout)
            if resp.status_code == 401 and not self._token_is_valid():
                # Only refresh if the token actually expired — avoid
                # hammering the token endpoint for permanently-unauthorized
                # workouts (deleted, archived, wrong account).
                self._refresh_access_token()
                session.headers["Authorization"] = f"Bearer {self._access_token}"
                resp = session.get(url, timeout=self._timeout)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 30))
                logger.warning(
                    "Wahoo rate-limited (429) for workout %s, waiting %ds.",
                    workout_id, retry_after,
                )
                _time.sleep(retry_after)
                resp = session.get(url, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            return data.get("workout_summary", data)

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = ex.submit(_fetch)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"Workout summary fetch timed out after {timeout}s")
        finally:
            ex.shutdown(wait=False)

    def get_fit(self, workout_id: int | str, timeout: int = 30, summary: dict | None = None) -> bytes | None:
        """Download FIT file bytes for a workout, or None if unavailable.

        The FIT URL is in the workout_summary's file.url field.
        Pass an already-fetched *summary* to avoid a redundant API call.
        """
        if summary is None:
            summary = self.get_workout_summary(workout_id, timeout=timeout)
        file_info = summary.get("file")
        if not file_info or not file_info.get("url"):
            logger.warning("No FIT file URL for Wahoo workout %s.", workout_id)
            return None

        fit_url = file_info["url"]

        def _download() -> bytes:
            resp = requests.get(fit_url, timeout=self._timeout)
            resp.raise_for_status()
            return resp.content

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = ex.submit(_download)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"Wahoo FIT download timed out after {timeout}s")
        finally:
            ex.shutdown(wait=False)

    def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session:
            self._session.close()
            self._session = None


# ── Wahoo workout type ID → Garmin-compatible activity type key ────────────

_WAHOO_TYPE_MAP: dict[int, str] = {
    0:  "cycling",
    1:  "running",
    5:  "treadmill_running",
    6:  "walking",
    9:  "hiking",
    12: "indoor_cycling",
    13: "mountain_biking",
    15: "road_biking",
    25: "lap_swimming",
    26: "open_water_swimming",
    42: "strength_training",
    47: "workout",
    61: "indoor_cycling",
    62: "workout",
    64: "e_bike_fitness",
    66: "yoga",
    67: "running",
    68: "indoor_cycling",
}


def wahoo_activity_type(workout_type_id: int | None) -> str:
    """Map a Wahoo workout_type_id to a Garmin-compatible activity type key."""
    if workout_type_id is None:
        return "workout"
    return _WAHOO_TYPE_MAP.get(workout_type_id, "workout")


def _safe_float(value: Any) -> float | None:
    """Convert a Wahoo summary value (often returned as string) to float, or None."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _safe_int(value: Any) -> int | None:
    """Convert a Wahoo summary value to int, or None."""
    f = _safe_float(value)
    return int(f) if f is not None else None


def map_wahoo_activity(user_id: int, workout: dict, summary: dict) -> dict:
    """Map a Wahoo workout + summary to the garmin-nostra storage schema.

    Returns a dict with the same keys as storage._map_activity() so the
    existing save_activity() / formatting / CalDAV / Mastodon code works
    unchanged.
    """
    workout_id = str(workout.get("id", ""))
    workout_type_id = workout.get("workout_type_id")
    activity_type = wahoo_activity_type(workout_type_id)

    starts_str = workout.get("starts") or ""
    try:
        start_dt = datetime.fromisoformat(starts_str.replace("Z", "+00:00"))
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        start_utc = start_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        start_utc = None
    start_local = start_utc  # Wahoo doesn't provide separate local time

    duration_total = _safe_float(summary.get("duration_total_accum"))
    duration_active = _safe_float(summary.get("duration_active_accum"))

    return {
        "user_id":                 user_id,
        "garmin_activity_id":      workout_id,
        "activity_name":           "[Wahoo] " + (workout.get("name") or summary.get("name") or "Workout"),
        "activity_type":           activity_type,
        "sport_type":              activity_type,
        "start_time_utc":          start_utc,
        "start_time_local":        start_local,
        "timezone":                summary.get("time_zone"),
        "duration_s":              duration_active or duration_total,
        "elapsed_time_s":          duration_total,
        "moving_time_s":           duration_active,
        "distance_m":              _safe_float(summary.get("distance_accum")),
        "elevation_gain_m":        _safe_float(summary.get("ascent_accum")),
        "elevation_loss_m":        None,
        "min_elevation_m":         None,
        "max_elevation_m":         None,
        "avg_speed_ms":            _safe_float(summary.get("speed_avg")),
        "max_speed_ms":            None,
        "avg_hr":                  _safe_int(summary.get("heart_rate_avg")),
        "max_hr":                  None,
        "resting_hr":              None,
        "avg_power_w":             _safe_float(summary.get("power_avg")),
        "max_power_w":             None,
        "normalized_power_w":      _safe_float(summary.get("power_bike_np_last")),
        "avg_cadence":             _safe_int(summary.get("cadence_avg")),
        "max_cadence":             None,
        "avg_stride_length_m":     None,
        "avg_vertical_osc_cm":     None,
        "avg_ground_contact_ms":   None,
        "aerobic_training_effect": None,
        "training_stress_score":   _safe_float(summary.get("power_bike_tss_last")),
        "vo2max_estimate":         None,
        "intensity_factor":        None,
        "calories":                _safe_int(summary.get("calories_accum")),
        "steps":                   None,
        "avg_temperature_c":       None,
        "max_temperature_c":       None,
        "start_lat":               None,
        "start_lon":               None,
        "raw_json":                json.dumps(
            {"workout": workout, "summary": summary}, ensure_ascii=False
        ),
        "gpx_path":                None,
        "fit_path":                None,
        "source":                  "WahooNoStra",
        "caldav_pushed":           0,
        "mastodon_posted":         0,
        "synced_at":               datetime.now(timezone.utc).isoformat(),
    }
