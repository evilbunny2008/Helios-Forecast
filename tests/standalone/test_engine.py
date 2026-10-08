from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import base_config, make_weather
from custom_components.helios_forecast.solar.residual import ProductionBucket
from helios_standalone.engine import Engine, RefreshError
from helios_standalone.settings import parse_settings
from helios_standalone.sources import Source
from helios_standalone.views import daily_comparison, series_view, solcast_view, summary_view

NOW = datetime(2026, 10, 8, 3, 20, tzinfo=timezone.utc)  # 14:20 in Sydney


class ListSource(Source):
    name = "list"

    def __init__(self, buckets):
        super().__init__()
        self.buckets = buckets
        self.synced = 0

    async def sync(self, now, learn_days):
        self.synced += 1

    def production(self, start, end):
        s, e = start.timestamp() * 1000, end.timestamp() * 1000
        return [b for b in self.buckets if b.start_ms >= s and b.end_ms <= e]


def run_engine(tmp_path: Path, source: Source, weather_fn=None, raw=None):
    settings = parse_settings(raw or base_config(), tmp_path)
    if source.name != "none":
        settings.entry["production_entity"] = "list:production"
    weather = make_weather(NOW)

    async def fetch(session, lat, lon, **kw):
        return weather if weather_fn is None else weather_fn()

    engine = Engine(settings, source, session=None, weather_fetcher=fetch, clock=lambda: NOW)
    return engine, asyncio.run(engine.refresh())


def test_physical_model_alone(tmp_path: Path):
    engine, snap = run_engine(tmp_path, Source())
    assert not snap.learning and snap.production == []
    today = snap.summary.days[0]
    assert today.energy_kwh > 5 and today.energy_kwh == pytest.approx(today.energy_raw_kwh)
    assert len(snap.summary.days) == 7
    assert any(p.key == "production_entity_unset" for p in snap.problems)
    assert len(snap.archive_points) == 60 * 24


def test_learns_a_roof_that_makes_eighty_percent_of_the_model(tmp_path: Path):
    _, plain = run_engine(tmp_path / "a", Source())
    buckets = [
        ProductionBucket(
            start_ms=p.t.timestamp() * 1000, end_ms=p.t.timestamp() * 1000 + 3_600_000, kwh=p.pv_raw_w * 0.8 / 1000
        )
        for p in plain.archive_points
    ]
    source = ListSource(buckets)
    engine, snap = run_engine(tmp_path / "b", source)
    assert source.synced == 1 and snap.learning
    ratio = snap.summary.days[1].energy_kwh / plain.summary.days[1].energy_kwh
    assert 0.7 < ratio < 0.9
    assert snap.reliability.days_learned >= 59

    comparison = daily_comparison(snap, engine.settings.tz)
    full = [d for d in comparison if d["measured_kwh"] and d["date"] != "2026-10-08"]
    assert len(full) >= 58
    off = sum(abs(d["predicted_kwh"] - d["measured_kwh"]) for d in full) / sum(d["measured_kwh"] for d in full)
    assert off < 0.1


def test_weather_failure_reuses_the_last_fetch_then_gives_up_without_one(tmp_path: Path):
    calls = {"n": 0}
    weather = make_weather(NOW)

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            raise TimeoutError("open-meteo down")
        return weather

    engine, first = run_engine(tmp_path, Source(), weather_fn=flaky)
    second = asyncio.run(engine.refresh())
    assert second.weather_reused and not first.weather_reused

    with pytest.raises(RefreshError):
        run_engine(tmp_path / "x", Source(), weather_fn=lambda: None)


def test_trend_and_day_ahead_survive_a_restart(tmp_path: Path):
    run_engine(tmp_path, Source())
    state = tmp_path / "data" / "state"
    assert (state / "trend.json").exists() and (state / "day_ahead.json").exists()
    _, again = run_engine(tmp_path, Source())
    assert again.trend.reference_kwh is not None and again.trend.delta_kwh == pytest.approx(0.0)


def test_views_serialise(tmp_path: Path):
    engine, snap = run_engine(tmp_path, Source())
    summary = summary_view(
        snap, site_name="Test", tz_name="Australia/Sydney", source_status=engine.source.status, last_error=None
    )
    assert summary["site"]["total_kwp"] == 6.6 and len(summary["days"]) == 7
    assert summary["problems"][0]["title"]  # text from the integration's translations
    series = series_view(snap)
    times = [p["t"] for p in series["points"]]
    assert times == sorted(times) and len(times) == len(set(times))
    solcast = solcast_view(snap)["forecasts"]
    assert solcast[0]["period_end"] == "2026-10-08T03:30:00.0000000Z"
    assert all(f["period"] == "PT30M" for f in solcast)
    # Half-hour averages of the 15-minute points keep the energy: compare tomorrow's total.
    tomorrow = [f for f in solcast if "2026-10-08T13:30" <= f["period_end"] <= "2026-10-09T13:00"]
    assert sum(f["pv_estimate"] * 0.5 for f in tomorrow) == pytest.approx(snap.summary.days[1].energy_kwh, rel=0.01)
