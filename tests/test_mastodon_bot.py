"""Tests for mastodon_bot.py — media upload retry and post fallback behaviour."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import mastodon_bot
from mastodon_bot import MastodonBot


@pytest.fixture()
def bot(monkeypatch):
    """A MastodonBot with a mocked client, built without touching the network."""
    b = MastodonBot.__new__(MastodonBot)
    b._client = MagicMock()
    # Never actually sleep during retry backoff in tests.
    monkeypatch.setattr(mastodon_bot.time, "sleep", lambda *_: None)
    return b


@pytest.fixture()
def png(tmp_path):
    p = tmp_path / "img.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return p


@pytest.fixture()
def activity():
    return {
        "garmin_activity_id": "123",
        "activity_name": "Testfahrt",
        "activity_type": "cycling",
        "start_time_utc": "2026-03-03 08:00:00",
        "duration_s": 3600.0,
        "distance_m": 30000.0,
        "elevation_gain_m": 200.0,
        "avg_hr": 140,
        "avg_power_w": 200.0,
    }


# ── _upload_media ────────────────────────────────────────────────────────────

def test_upload_media_success(bot, png):
    bot._client.media_post.return_value = {"id": "MEDIA1"}
    assert bot._upload_media(png, "desc") == "MEDIA1"
    assert bot._client.media_post.call_count == 1


def test_upload_media_retries_then_succeeds(bot, png):
    bot._client.media_post.side_effect = [RuntimeError("timeout"), {"id": "MEDIA2"}]
    assert bot._upload_media(png, "desc") == "MEDIA2"
    assert bot._client.media_post.call_count == 2


def test_upload_media_permanent_failure_returns_none(bot, png, caplog):
    bot._client.media_post.side_effect = RuntimeError("storage down")
    with caplog.at_level("ERROR"):
        assert bot._upload_media(png, "desc") is None
    # initial attempt + configured retries
    assert bot._client.media_post.call_count == mastodon_bot._MEDIA_UPLOAD_RETRIES + 1
    assert any("permanently failed" in r.message for r in caplog.records)


# ── post_activity fallback ───────────────────────────────────────────────────

def test_post_activity_posts_text_when_all_media_fail(bot, png, activity):
    bot._client.media_post.side_effect = RuntimeError("nope")
    bot._client.status_post.return_value = {"id": "STATUS1"}

    status_id = bot.post_activity(
        "@u@inst.social", activity, map_image_path=png, elevation_profile_path=png,
    )

    assert status_id == "STATUS1"
    bot._client.status_post.assert_called_once()
    # media_ids must be None (no attachments) — never block the post on media
    assert bot._client.status_post.call_args.kwargs["media_ids"] is None


def test_post_activity_attaches_only_succeeding_media(bot, png, activity):
    # First media (map) succeeds, second (elevation) fails permanently.
    bot._client.media_post.side_effect = (
        [{"id": "MAP"}] + [RuntimeError("x")] * (mastodon_bot._MEDIA_UPLOAD_RETRIES + 1)
    )
    bot._client.status_post.return_value = {"id": "STATUS2"}

    status_id = bot.post_activity(
        "@u@inst.social", activity, map_image_path=png, elevation_profile_path=png,
    )

    assert status_id == "STATUS2"
    assert bot._client.status_post.call_args.kwargs["media_ids"] == ["MAP"]
