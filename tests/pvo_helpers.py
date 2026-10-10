"""Shared test helpers."""

from __future__ import annotations

from datetime import datetime

from homeassistant.util import dt as dt_util


def local_dt(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Return a 2026 local datetime in Home Assistant's time zone."""
    return datetime(2026, month, day, hour, minute, tzinfo=dt_util.get_default_time_zone())


def local_ts(month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    """Return the epoch of a 2026 local time in Home Assistant's time zone."""
    return int(local_dt(month, day, hour, minute).timestamp())
