"""Shared pytest fixtures for garmin-nostra tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make src/ importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from storage import ActivityStore  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture()
def store(tmp_path):
    """Isolated ActivityStore backed by a temp directory (not in-memory,
    so WAL mode and migrations run exactly as in production)."""
    s = ActivityStore(
        db_path=str(tmp_path / "test.db"),
        gpx_dir=str(tmp_path / "gpx"),
        fit_dir=str(tmp_path / "fit"),
        map_dir=str(tmp_path / "maps"),
        token_dir=str(tmp_path / "tokens"),
    )
    yield s
    s.close()


@pytest.fixture()
def user_cfg():
    return {
        "name": "testuser",
        "garmin_username": "test@example.invalid",
        "garmin_password": "test_password",
        "mastodon_handle": "@testuser@social.example.invalid",
        "caldav_enabled": False,
        "mastodon_public": False,
    }


@pytest.fixture()
def user_id(store, user_cfg):
    return store.upsert_user(user_cfg)


# ── Activity fixtures ─────────────────────────────────────────────────────────

@pytest.fixture()
def garmin_running():
    return load_fixture("garmin_running.json")


@pytest.fixture()
def garmin_cycling():
    return load_fixture("garmin_cycling.json")


@pytest.fixture()
def garmin_indoor_cycling():
    return load_fixture("garmin_indoor_cycling.json")


@pytest.fixture()
def wahoo_workout_cycling():
    return load_fixture("wahoo_workout_cycling.json")


@pytest.fixture()
def wahoo_summary_cycling():
    return load_fixture("wahoo_summary_cycling.json")


@pytest.fixture()
def wahoo_workout_indoor():
    return load_fixture("wahoo_workout_indoor.json")


@pytest.fixture()
def wahoo_summary_indoor():
    return load_fixture("wahoo_summary_indoor.json")
