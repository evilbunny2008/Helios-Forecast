"""Shared helpers for the standalone runner's tests. Needs aiohttp, unlike the pure suite."""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from custom_components.helios_forecast.openmeteo import WeatherSeries  # noqa: E402

SYDNEY_UTC_OFFSET_H = 11


def make_weather(now_utc: datetime, *, past_days: int = 60, forecast_days: int = 8) -> WeatherSeries:
    """Whole UTC days as Open-Meteo answers them, with a clear-ish day shape centred on Sydney noon."""
    midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    t = midnight - timedelta(days=past_days)
    end = midnight + timedelta(days=forecast_days) - timedelta(hours=1)
    times = []
    while t <= end:
        times.append(t)
        t += timedelta(hours=1)
    ghi = []
    for t in times:
        local_h = (t.hour + SYDNEY_UTC_OFFSET_H) % 24
        ghi.append(max(0.0, 900.0 * math.cos((local_h - 12) / 7 * math.pi / 2)) if abs(local_h - 12) < 7 else 0.0)
    n = len(times)
    return WeatherSeries(
        times=times,
        cloud=[15.0] * n,
        shortwave=ghi,
        direct=[g * 0.75 for g in ghi],
        diffuse=[g * 0.25 for g in ghi],
        temp=[18.0] * n,
        wind=[8.0] * n,
        snow=[0.0] * n,
        cloud_spread=[4.0] * n,
    )


def base_config(**overrides) -> dict:
    raw = {
        "data_dir": "data",
        "site": {
            "name": "Test",
            "latitude": -33.86,
            "longitude": 151.21,
            "timezone": "Australia/Sydney",
            "inverter_max_kw": 5.0,
        },
        "arrays": [{"tilt": 20, "azimuth": 0, "kwp": 6.6}],
        "source": {"type": "none"},
    }
    for key, value in overrides.items():
        raw[key] = value
    return raw
