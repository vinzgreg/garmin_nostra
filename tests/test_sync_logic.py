"""Tests for sync orchestration logic — process_user and process_user_wahoo.

All external I/O (Garmin API, Wahoo API, Mastodon, CalDAV) is mocked so
these tests run offline and never touch real services.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from sync import process_user, process_user_wahoo


# ── Helpers ───────────────────────────────────────────────────────────────────

def _old_enough(fixture: dict) -> dict:
    """Return activity with startTimeGMT set to 1 hour ago (passes the 10-min gate)."""
    act = dict(fixture)
    act["startTimeGMT"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M:%S")
    act["startTimeLocal"] = act["startTimeGMT"]
    return act


def _too_recent(fixture: dict) -> dict:
    """Return activity with startTimeGMT set to 2 minutes ago (fails the 10-min gate)."""
    act = dict(fixture)
    act["startTimeGMT"] = (
        datetime.now(timezone.utc) - timedelta(minutes=2)
    ).strftime("%Y-%m-%d %H:%M:%S")
    act["startTimeLocal"] = act["startTimeGMT"]
    return act


def _make_garmin_client(activities: list):
    """Return a mock GarminClient whose get_activities_since returns *activities*."""
    client = MagicMock()
    client.get_activities_since.return_value = activities
    client.get_gpx.return_value = b""
    client.get_fit.return_value = b""
    return client


def _make_wahoo_client(workouts: list, summary: dict | None = None, fit_data: bytes = b"FIT"):
    client = MagicMock()
    client.get_workouts_since.return_value = workouts
    client.get_workout_summary.return_value = summary or {}
    client.get_fit.return_value = fit_data
    return client


def _make_bot():
    bot = MagicMock()
    bot.post_activity.return_value = "MOCK_STATUS_ID"
    return bot


def _base_user_cfg(overrides: dict | None = None) -> dict:
    cfg = {
        "name": "testuser",
        "garmin_username": "test@example.invalid",
        "garmin_password": "secret",
        "mastodon_handle": "@testuser@social.example.invalid",
        "caldav_enabled": False,
        "mastodon_public": False,
    }
    if overrides:
        cfg.update(overrides)
    return cfg


# ── Garmin sync — basic flow ──────────────────────────────────────────────────

@patch("sync.render_map", return_value=None)
@patch("sync.GarminClient")
def test_garmin_new_activity_is_stored(MockGarmin, mock_render, store, user_cfg, garmin_running):
    act = _old_enough(garmin_running)
    MockGarmin.return_value = _make_garmin_client([act])
    bot = _make_bot()

    process_user(user_cfg, store, bot, None, lookback_days=7)

    uid = store.upsert_user(user_cfg)
    fetched = store.get_activity(uid, str(garmin_running["activityId"]))
    assert fetched is not None
    assert fetched["activity_type"] == "running"


@patch("sync.render_map", return_value=None)
@patch("sync.GarminClient")
def test_garmin_mastodon_posted_for_running(MockGarmin, mock_render, store, user_cfg, garmin_running):
    act = _old_enough(garmin_running)
    MockGarmin.return_value = _make_garmin_client([act])
    bot = _make_bot()

    process_user(user_cfg, store, bot, None, lookback_days=7)

    bot.post_activity.assert_called_once()
    uid = store.upsert_user(user_cfg)
    fetched = store.get_activity(uid, str(garmin_running["activityId"]))
    assert fetched["mastodon_posted"] == 1
    assert fetched["mastodon_status_id"] == "MOCK_STATUS_ID"


@patch("sync.render_map", return_value=None)
@patch("sync.GarminClient")
def test_garmin_too_recent_activity_is_skipped(MockGarmin, mock_render, store, user_cfg, garmin_running):
    act = _too_recent(garmin_running)
    MockGarmin.return_value = _make_garmin_client([act])
    bot = _make_bot()

    process_user(user_cfg, store, bot, None, lookback_days=7)

    uid = store.upsert_user(user_cfg)
    assert store.get_activity(uid, str(garmin_running["activityId"])) is None
    bot.post_activity.assert_not_called()


@patch("sync.render_map", return_value=None)
@patch("sync.GarminClient")
def test_garmin_indoor_cycling_deferred_no_mastodon(MockGarmin, mock_render, store, user_cfg, garmin_indoor_cycling):
    """Indoor cycling activities are saved but integrations are deferred to the next cycle."""
    act = _old_enough(garmin_indoor_cycling)
    MockGarmin.return_value = _make_garmin_client([act])
    bot = _make_bot()

    process_user(user_cfg, store, bot, None, lookback_days=7)

    bot.post_activity.assert_not_called()
    uid = store.upsert_user(user_cfg)
    fetched = store.get_activity(uid, str(garmin_indoor_cycling["activityId"]))
    # Activity is in DB but not yet posted
    assert fetched is not None
    assert fetched["mastodon_posted"] == 0


@patch("sync.render_map", return_value=None)
@patch("sync.GarminClient")
def test_garmin_indoor_cycling_posted_on_second_run(MockGarmin, mock_render, store, user_cfg, garmin_indoor_cycling):
    """On a second sync the indoor cycling activity has mastodon_posted=0 (was deferred),
    so it should now be posted."""
    act = _old_enough(garmin_indoor_cycling)
    client = _make_garmin_client([act])
    MockGarmin.return_value = client
    bot = _make_bot()

    # First run — deferred
    process_user(user_cfg, store, bot, None, lookback_days=7)
    assert bot.post_activity.call_count == 0

    # Second run — same activity returned again by Garmin (re-fetched via 2h lookback)
    process_user(user_cfg, store, bot, None, lookback_days=7)
    assert bot.post_activity.call_count == 1


@patch("sync.render_map", return_value=None)
@patch("sync.GarminClient")
def test_garmin_already_posted_is_not_posted_again(MockGarmin, mock_render, store, user_cfg, garmin_running):
    """An activity already marked mastodon_posted=1 must not trigger another post."""
    act = _old_enough(garmin_running)
    MockGarmin.return_value = _make_garmin_client([act])
    bot = _make_bot()

    process_user(user_cfg, store, bot, None, lookback_days=7)
    assert bot.post_activity.call_count == 1

    # Second run — activity is known and already posted
    process_user(user_cfg, store, bot, None, lookback_days=7)
    assert bot.post_activity.call_count == 1  # not called again


@patch("sync.render_map", return_value=None)
@patch("sync.GarminClient")
def test_garmin_mastodon_suppressed_by_type(MockGarmin, mock_render, store, user_cfg, garmin_running):
    act = _old_enough(garmin_running)
    MockGarmin.return_value = _make_garmin_client([act])
    bot = _make_bot()
    cfg = _base_user_cfg({"mastodon_suppress_types": ["running"]})

    process_user(cfg, store, bot, None, lookback_days=7)
    bot.post_activity.assert_not_called()


@patch("sync.render_map", return_value=None)
@patch("sync.GarminClient")
def test_garmin_caldav_pushed_when_enabled(MockGarmin, mock_render, store, user_cfg, garmin_running):
    act = _old_enough(garmin_running)
    MockGarmin.return_value = _make_garmin_client([act])
    bot = _make_bot()
    caldav = MagicMock()
    cfg = _base_user_cfg({"caldav_enabled": True})

    process_user(cfg, store, bot, caldav, lookback_days=7)
    caldav.push.assert_called_once()
    uid = store.upsert_user(cfg)
    fetched = store.get_activity(uid, str(garmin_running["activityId"]))
    assert fetched["caldav_pushed"] == 1


# ── Garmin sync — duplicate / dedup ──────────────────────────────────────────

@patch("sync.render_map", return_value=None)
@patch("sync.GarminClient")
def test_garmin_dedup_with_wahoo_skips_matching_activity(MockGarmin, mock_render, store, user_cfg, garmin_cycling):
    """When dedup_with_wahoo=True and a Wahoo activity with the same start time already
    exists, the Garmin activity is skipped (not stored)."""
    from wahoo import map_wahoo_activity
    from conftest import load_fixture

    # Store the matching Wahoo activity first
    uid = store.upsert_user(user_cfg)
    w_workout = load_fixture("wahoo_workout_cycling.json")
    w_summary = load_fixture("wahoo_summary_cycling.json")
    # Use same start time as our Garmin fixture
    garmin_start = "2023-06-25 08:52:35"
    w_workout = dict(w_workout)
    w_workout["starts"] = "2023-06-25T08:52:35.000Z"
    w_summary = dict(w_summary)
    w_summary["started_at"] = "2023-06-25T08:52:35.000Z"
    wahoo_row = map_wahoo_activity(uid, w_workout, w_summary)
    store.save_wahoo_activity(uid, wahoo_row)

    act = _old_enough(garmin_cycling)
    act["startTimeGMT"] = garmin_start
    act["startTimeLocal"] = "2023-06-25 10:52:35"
    MockGarmin.return_value = _make_garmin_client([act])
    bot = _make_bot()

    process_user(user_cfg, store, bot, None, lookback_days=7, dedup_with_wahoo=True)

    # Garmin activity must not be stored
    assert store.get_activity(uid, str(garmin_cycling["activityId"])) is None
    bot.post_activity.assert_not_called()


# ── Wahoo sync — basic flow ───────────────────────────────────────────────────

@patch("sync.WahooClient")
def test_wahoo_new_activity_is_stored(MockWahoo, store, wahoo_workout_cycling, wahoo_summary_cycling):
    workout = dict(wahoo_workout_cycling)
    workout["starts"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    client = _make_wahoo_client([workout], summary=wahoo_summary_cycling)
    MockWahoo.return_value = client

    bot = _make_bot()
    cfg = _base_user_cfg({
        "source": "wahoo",
        "wahoo_client_id": "test_cid",
        "wahoo_client_secret": "test_csecret",
        "wahoo_refresh_token": "test_rtoken",
    })

    process_user_wahoo(cfg, store, bot, None, lookback_days=7)

    uid = store.upsert_user(cfg)
    fetched = store.get_wahoo_activity(uid, str(wahoo_workout_cycling["id"]))
    assert fetched is not None
    assert fetched["source"] == "WahooNoStra"
    assert fetched["mastodon_status_id"] == "MOCK_STATUS_ID"


@patch("sync.WahooClient")
def test_wahoo_indoor_cycling_power_stored(MockWahoo, store, wahoo_workout_indoor, wahoo_summary_indoor):
    workout = dict(wahoo_workout_indoor)
    workout["starts"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    client = _make_wahoo_client([workout], summary=wahoo_summary_indoor)
    MockWahoo.return_value = client

    bot = _make_bot()
    cfg = _base_user_cfg({
        "wahoo_client_id": "cid", "wahoo_client_secret": "cs", "wahoo_refresh_token": "rt",
    })

    process_user_wahoo(cfg, store, bot, None, lookback_days=7)

    uid = store.upsert_user(cfg)
    fetched = store.get_wahoo_activity(uid, str(wahoo_workout_indoor["id"]))
    assert fetched["avg_power_w"] == pytest.approx(139.0)
    assert fetched["normalized_power_w"] == pytest.approx(146.0)


@patch("sync.WahooClient")
def test_wahoo_too_recent_activity_is_skipped(MockWahoo, store, wahoo_workout_cycling, wahoo_summary_cycling):
    workout = dict(wahoo_workout_cycling)
    workout["starts"] = (
        datetime.now(timezone.utc) - timedelta(minutes=2)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    MockWahoo.return_value = _make_wahoo_client([workout], summary=wahoo_summary_cycling)
    bot = _make_bot()
    cfg = _base_user_cfg({
        "wahoo_client_id": "cid", "wahoo_client_secret": "cs", "wahoo_refresh_token": "rt",
    })

    process_user_wahoo(cfg, store, bot, None, lookback_days=7)

    uid = store.upsert_user(cfg)
    assert store.get_wahoo_activity(uid, str(wahoo_workout_cycling["id"])) is None
    bot.post_activity.assert_not_called()


@patch("sync.WahooClient")
def test_wahoo_already_posted_is_not_posted_again(MockWahoo, store, wahoo_workout_cycling, wahoo_summary_cycling):
    workout = dict(wahoo_workout_cycling)
    workout["starts"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    MockWahoo.return_value = _make_wahoo_client([workout], summary=wahoo_summary_cycling)
    bot = _make_bot()
    cfg = _base_user_cfg({
        "wahoo_client_id": "cid", "wahoo_client_secret": "cs", "wahoo_refresh_token": "rt",
    })

    process_user_wahoo(cfg, store, bot, None, lookback_days=7)
    assert bot.post_activity.call_count == 1

    process_user_wahoo(cfg, store, bot, None, lookback_days=7)
    assert bot.post_activity.call_count == 1  # not called again


@patch("sync.WahooClient")
def test_wahoo_skipped_workout_is_not_processed(MockWahoo, store, wahoo_workout_cycling, wahoo_summary_cycling):
    workout = dict(wahoo_workout_cycling)
    workout["starts"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    MockWahoo.return_value = _make_wahoo_client([workout], summary=wahoo_summary_cycling)
    bot = _make_bot()
    cfg = _base_user_cfg({
        "wahoo_client_id": "cid", "wahoo_client_secret": "cs", "wahoo_refresh_token": "rt",
    })

    uid = store.upsert_user(cfg)
    store.mark_wahoo_skipped(uid, str(wahoo_workout_cycling["id"]), "401 Unauthorized")

    process_user_wahoo(cfg, store, bot, None, lookback_days=7)

    assert store.get_wahoo_activity(uid, str(wahoo_workout_cycling["id"])) is None
    bot.post_activity.assert_not_called()


# ── Wahoo → Garmin bridge (wahoo_sync_to_garmin) ─────────────────────────────

@patch("sync.GarminClient")
@patch("sync.WahooClient")
def test_wahoo_to_garmin_bridge_uploads_fit(MockWahoo, MockGarmin, store, wahoo_workout_indoor, wahoo_summary_indoor):
    """When wahoo_sync_to_garmin=True, the FIT file is uploaded to Garmin."""
    workout = dict(wahoo_workout_indoor)
    workout["starts"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    fit_bytes = b"FITDATA"
    wahoo_client = _make_wahoo_client([workout], summary=wahoo_summary_indoor, fit_data=fit_bytes)
    MockWahoo.return_value = wahoo_client

    garmin_client = MagicMock()
    MockGarmin.return_value = garmin_client

    bot = _make_bot()
    cfg = _base_user_cfg({
        "wahoo_sync_to_garmin": True,
        "wahoo_client_id": "cid",
        "wahoo_client_secret": "cs",
        "wahoo_refresh_token": "rt",
    })

    process_user_wahoo(cfg, store, bot, None, lookback_days=7)

    garmin_client.upload_fit.assert_called_once()
    upload_args = garmin_client.upload_fit.call_args
    assert upload_args[0][0] == fit_bytes

    uid = store.upsert_user(cfg)
    fetched = store.get_wahoo_activity(uid, str(wahoo_workout_indoor["id"]))
    assert fetched["wahoo_synced_to_garmin"] == 1


@patch("sync.GarminClient")
@patch("sync.WahooClient")
def test_wahoo_to_garmin_bridge_skips_if_no_fit(MockWahoo, MockGarmin, store, wahoo_workout_indoor, wahoo_summary_indoor):
    """If WahooClient.get_fit returns None, no upload attempt is made."""
    workout = dict(wahoo_workout_indoor)
    workout["starts"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    wahoo_client = _make_wahoo_client([workout], summary=wahoo_summary_indoor, fit_data=None)
    MockWahoo.return_value = wahoo_client

    garmin_client = MagicMock()
    MockGarmin.return_value = garmin_client

    bot = _make_bot()
    cfg = _base_user_cfg({
        "wahoo_sync_to_garmin": True,
        "wahoo_client_id": "cid",
        "wahoo_client_secret": "cs",
        "wahoo_refresh_token": "rt",
    })

    process_user_wahoo(cfg, store, bot, None, lookback_days=7)
    garmin_client.upload_fit.assert_not_called()


@patch("sync.GarminClient")
@patch("sync.WahooClient")
def test_wahoo_to_garmin_bridge_handles_duplicate_gracefully(MockWahoo, MockGarmin, store, wahoo_workout_indoor, wahoo_summary_indoor):
    """If Garmin rejects the upload as a duplicate, the activity is still marked synced."""
    workout = dict(wahoo_workout_indoor)
    workout["starts"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    wahoo_client = _make_wahoo_client([workout], summary=wahoo_summary_indoor, fit_data=b"FIT")
    MockWahoo.return_value = wahoo_client

    garmin_client = MagicMock()
    garmin_client.upload_fit.side_effect = Exception("duplicate activity")
    MockGarmin.return_value = garmin_client

    bot = _make_bot()
    cfg = _base_user_cfg({
        "wahoo_sync_to_garmin": True,
        "wahoo_client_id": "cid",
        "wahoo_client_secret": "cs",
        "wahoo_refresh_token": "rt",
    })

    process_user_wahoo(cfg, store, bot, None, lookback_days=7)

    uid = store.upsert_user(cfg)
    fetched = store.get_wahoo_activity(uid, str(wahoo_workout_indoor["id"]))
    # "duplicate" in error message → marked as synced, not an error
    assert fetched["wahoo_synced_to_garmin"] == 1


@patch("sync.GarminClient")
@patch("sync.WahooClient")
def test_wahoo_to_garmin_bridge_does_not_retry_after_success(MockWahoo, MockGarmin, store, wahoo_workout_indoor, wahoo_summary_indoor):
    """Second sync run must not call upload_fit again for an already-synced activity."""
    workout = dict(wahoo_workout_indoor)
    workout["starts"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    wahoo_client = _make_wahoo_client([workout], summary=wahoo_summary_indoor, fit_data=b"FIT")
    MockWahoo.return_value = wahoo_client
    garmin_client = MagicMock()
    MockGarmin.return_value = garmin_client

    bot = _make_bot()
    cfg = _base_user_cfg({
        "wahoo_sync_to_garmin": True,
        "wahoo_client_id": "cid",
        "wahoo_client_secret": "cs",
        "wahoo_refresh_token": "rt",
    })

    process_user_wahoo(cfg, store, bot, None, lookback_days=7)
    assert garmin_client.upload_fit.call_count == 1

    process_user_wahoo(cfg, store, bot, None, lookback_days=7)
    assert garmin_client.upload_fit.call_count == 1  # not called again


# ── "both" mode — Wahoo + Garmin with cross-source dedup ──────────────────────

@patch("sync.render_map", return_value=None)
@patch("sync.GarminClient")
@patch("sync.WahooClient")
def test_both_mode_garmin_duplicate_suppressed(MockWahoo, MockGarmin, mock_render,
                                                store, wahoo_workout_cycling, wahoo_summary_cycling,
                                                garmin_cycling):
    """In 'both' mode: Wahoo activity is stored first, then the same Garmin activity
    (same start time) is suppressed and not posted to Mastodon."""
    start_iso = "2023-06-25T08:52:35.000Z"
    start_gmt = "2023-06-25 08:52:35"

    # Wahoo returns this workout
    workout = dict(wahoo_workout_cycling)
    workout["starts"] = start_iso
    wahoo_summary = dict(wahoo_summary_cycling)
    wahoo_summary["started_at"] = start_iso
    wahoo_client = _make_wahoo_client([workout], summary=wahoo_summary)
    MockWahoo.return_value = wahoo_client

    # Garmin returns same time window as the Wahoo workout
    garmin_act = dict(garmin_cycling)
    garmin_act["startTimeGMT"] = start_gmt
    garmin_act["startTimeLocal"] = "2023-06-25 10:52:35"
    garmin_act["duration"] = float(wahoo_summary["duration_total_accum"])
    garmin_client = _make_garmin_client([garmin_act])
    MockGarmin.return_value = garmin_client

    bot = _make_bot()
    cfg = _base_user_cfg({
        "source": "both",
        "wahoo_client_id": "cid",
        "wahoo_client_secret": "cs",
        "wahoo_refresh_token": "rt",
    })

    # Run Wahoo side (as sync._run_inner would for source='both')
    process_user_wahoo(cfg, store, bot, None, lookback_days=7)
    # Run Garmin side with dedup enabled
    process_user(cfg, store, bot, None, lookback_days=7,
                 dedup_with_wahoo=True, tag_source=True, skip_kudos=True)

    uid = store.upsert_user(cfg)
    garmin_fetched = store.get_activity(uid, str(garmin_cycling["activityId"]))
    wahoo_fetched = store.get_wahoo_activity(uid, str(wahoo_workout_cycling["id"]))

    # Wahoo activity is fully processed
    assert wahoo_fetched is not None
    assert wahoo_fetched["mastodon_posted"] == 1

    # Garmin duplicate is either absent (skipped by near-time check) or suppressed
    if garmin_fetched is not None:
        assert garmin_fetched["suppressed"] is not None
        assert garmin_fetched["mastodon_posted"] == 0
