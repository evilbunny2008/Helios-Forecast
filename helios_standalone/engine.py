"""One refresh of the forecast: the integration coordinator's pipeline without Home Assistant.

Follows HeliosForecastCoordinator._async_update_data step for step and calls the same pure functions
with the same arguments, so the numbers match what the integration would publish for the same site
and history. What changes is only where things come from and go: the history from a Source instead
of the recorder, the persisted trend and day-ahead record from JSON files instead of Store, and the
result into a Snapshot the web server reads instead of entities and statistics. The opt-in benchmark
upload is left out: it identifies itself by a Home Assistant config entry.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import partial
from typing import Any, Awaitable, Callable, Dict, List, Optional

from aiohttp import ClientSession

from custom_components.helios_forecast.analog import build_library, enrich_archive_points, enrich_points
from custom_components.helios_forecast.battery import BatterySocPoint, project_battery_soc
from custom_components.helios_forecast.checkup import (
    Problem,
    check_config,
    check_consumption_coverage,
    check_production_history,
)
from custom_components.helios_forecast.config import (
    battery_from_config,
    inverter_max_w_from_config,
    layout_from_config,
    learning_from_config,
    location_from_config,
    trend_anchor_hour_from_config,
)
from custom_components.helios_forecast.consumption import (
    ConsumptionProfile,
    ConsumptionSources,
    build_consumption_profile,
)
from custom_components.helios_forecast.curtailment import flag_curtailed
from custom_components.helios_forecast.forecast import ForecastPoint, build_forecast_series
from custom_components.helios_forecast.openmeteo import WeatherSeries, fetch_weather
from custom_components.helios_forecast.reliability import SKILL_WINDOW_DAYS, Reliability, compute_reliability
from custom_components.helios_forecast.solar.power import PvLayout
from custom_components.helios_forecast.solar.residual import (
    LEARN_DAYS,
    ProductionBucket,
    SkyResidualInput,
    build_sky_residual_map,
)
from custom_components.helios_forecast.statistics import observed_snapshot
from custom_components.helios_forecast.summary import ForecastSummary, summarize
from custom_components.helios_forecast.trend import TodayTrend, TrendReference, compute_trend, should_capture

from .settings import Settings
from .sources import Source
from .state import JsonStore

_LOGGER = logging.getLogger(__name__)

# The integration's cadence and horizons (coordinator.py), kept identical.
STEP_MINUTES = 15
FORECAST_DAYS = 7
BATTERY_SOC_HORIZON_HOURS = 48.0
# The source's consumption history, signed the way consumption.py sums the Energy dashboard's
# meters. A FoxESS "loads" figure is already the house's consumption, so it is the only term.
_CONSUMPTION_ID = "consumption"

WeatherFetcher = Callable[..., Awaitable[Optional[WeatherSeries]]]


class RefreshError(RuntimeError):
    """The refresh could not produce a forecast (no weather at all)."""


@dataclass
class Snapshot:
    refreshed_at: datetime
    points: List[ForecastPoint]
    archive_points: List[ForecastPoint]
    elapsed_points: List[ForecastPoint]
    summary: ForecastSummary
    reliability: Reliability
    trend: TodayTrend
    battery_soc: List[BatterySocPoint]
    observed: Dict[str, Optional[float]]
    problems: List[Problem]
    production: List[ProductionBucket]
    layout: PvLayout
    lat: float
    lon: float
    live_soc: Optional[float] = None
    weather_reused: bool = False
    learning: bool = False
    timings: Dict[str, float] = field(default_factory=dict)


class Engine:
    def __init__(
        self,
        settings: Settings,
        source: Source,
        session: ClientSession,
        *,
        weather_fetcher: WeatherFetcher = fetch_weather,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.settings = settings
        self.source = source
        self._session = session
        self._fetch_weather = weather_fetcher
        self._clock = clock
        self.snapshot: Optional[Snapshot] = None
        self.last_error: Optional[str] = None
        self._weather: Optional[WeatherSeries] = None
        self._lock = asyncio.Lock()
        self._last_archive_hour: Optional[datetime] = None
        self._archive_points: List[ForecastPoint] = []
        self._consumption_profile: Optional[ConsumptionProfile] = None
        self._last_consumption_hour: Optional[datetime] = None
        state_dir = settings.data_dir / "state"
        self._trend_store = JsonStore(state_dir / "trend.json")
        self._skill_store = JsonStore(state_dir / "day_ahead.json")
        self._trend_ref: Optional[TrendReference] = None
        self._trend_loaded = False
        self._day_ahead: Dict[date, float] = {}
        self._day_ahead_loaded = False

    async def refresh(self) -> Snapshot:
        """Run one refresh; concurrent callers wait for the one in progress instead of starting another."""
        async with self._lock:
            try:
                snapshot = await self._refresh()
            except Exception as err:
                self.last_error = str(err)
                raise
            self.last_error = None
            self.snapshot = snapshot
            return snapshot

    async def _refresh(self) -> Snapshot:
        timings: Dict[str, float] = {}
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        data = self.settings.entry
        tz = self.settings.tz
        lat, lon = location_from_config(data, self.settings.latitude, self.settings.longitude)
        layout = layout_from_config(data)
        cap = inverter_max_w_from_config(data)
        problems = check_config(data, self.settings.latitude, self.settings.longitude)

        weather_task = asyncio.ensure_future(
            self._fetch_weather(self._session, lat, lon, past_days=LEARN_DAYS, forecast_days=FORECAST_DAYS + 1)
        )
        # The source sync and the weather fetch hit different services; run them side by side.
        try:
            await self.source.sync(self._clock(), LEARN_DAYS)
        except Exception as err:  # noqa: BLE001 - history is best-effort, the forecast still renders
            _LOGGER.warning("History source sync failed, using what is cached: %s", err)
            self.source.status.ok, self.source.status.last_error = False, str(err)
        timings["source_s"] = loop.time() - t0

        reused = False
        try:
            weather = await weather_task
        except Exception as err:  # noqa: BLE001
            if self._weather is None:
                raise RefreshError(f"Open-Meteo fetch failed: {err}") from err
            _LOGGER.warning("Open-Meteo fetch failed (%s); reusing the last successful fetch", err)
            weather, reused = self._weather, True
        if weather is None:
            if self._weather is None:
                raise RefreshError("Open-Meteo returned no weather data")
            _LOGGER.warning("Open-Meteo returned no weather data; reusing the last successful fetch")
            weather, reused = self._weather, True
        self._weather = weather
        timings["weather_s"] = loop.time() - t0

        now = self._clock().astimezone(tz)
        learn_start = now - timedelta(days=LEARN_DAYS)
        production: List[ProductionBucket] = []
        residual_map = None
        production_name = learning_from_config(data)
        history = self.source.production(learn_start, now)
        if production_name and history is not None:
            production = self._flag_curtailed(history, learn_start, now, cap)
            problems += check_production_history(
                production, production_name, lat, lon, layout.total_kwp, now, LEARN_DAYS
            )
            if production:
                residual_map = await asyncio.to_thread(
                    build_sky_residual_map,
                    SkyResidualInput(
                        lat=lat,
                        lon=lon,
                        layout=layout,
                        production=production,
                        inverter_max_w=cap,
                        cloud_times=[t.timestamp() * 1000.0 for t in weather.times],
                        cloud=weather.cloud,
                        shortwave=weather.shortwave,
                        direct=weather.direct,
                        diffuse=weather.diffuse,
                        temp=weather.temp,
                        wind=weather.wind,
                        snow=weather.snow,
                        now_ms=now.timestamp() * 1000.0,
                    ),
                )

        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=FORECAST_DAYS)
        points = await asyncio.to_thread(
            partial(
                build_forecast_series,
                weather,
                layout,
                lat,
                lon,
                inverter_max_w=cap,
                start=start,
                end=end,
                step_minutes=STEP_MINUTES,
                residual_map=residual_map,
            )
        )
        analog_library = await asyncio.to_thread(build_library, production, weather, lat, lon, layout, cap)
        points = await asyncio.to_thread(enrich_points, points, analog_library, weather, lat, lon, now)
        elapsed = await asyncio.to_thread(
            enrich_archive_points, [p for p in points if p.t < now], analog_library, weather, lat, lon
        )
        served = elapsed + [p for p in points if p.t >= now]
        summary = await asyncio.to_thread(partial(summarize, served, now=now, tz=tz, step_minutes=STEP_MINUTES))
        timings["model_s"] = loop.time() - t0

        now_utc = now.astimezone(timezone.utc)
        archive_hour = now_utc.replace(minute=0, second=0, microsecond=0)
        if self._last_archive_hour != archive_hour:
            self._archive_points = await asyncio.to_thread(
                self._compute_archive_points, now_utc, weather, layout, lat, lon, cap, residual_map, analog_library
            )
            self._last_archive_hour = archive_hour

        day_ahead = self._record_day_ahead(now, summary)
        reliability = await asyncio.to_thread(compute_reliability, production, day_ahead, weather, now, tz)
        trend = self._today_trend(now, summary)
        battery_soc = await self._project_battery_soc(points, now)
        if self._consumption_profile is not None:
            problems += check_consumption_coverage(self._consumption_profile.coverage)
        timings["total_s"] = loop.time() - t0

        return Snapshot(
            refreshed_at=now_utc,
            points=points,
            archive_points=self._archive_points,
            elapsed_points=elapsed,
            summary=summary,
            reliability=reliability,
            trend=trend,
            battery_soc=battery_soc,
            observed=observed_snapshot(weather, now_utc),
            problems=problems,
            production=production,
            layout=layout,
            lat=lat,
            lon=lon,
            live_soc=self.source.live_soc(),
            weather_reused=reused,
            learning=residual_map is not None,
            timings=timings,
        )

    def _flag_curtailed(self, production, start, end, cap) -> List[ProductionBucket]:
        if not production:
            return production
        soc_max = self.source.soc_max_by_hour(start, end) if math.isfinite(cap) else None
        if not soc_max:
            return production
        return flag_curtailed(production, soc_max_by_start_ms=soc_max, cap_w=cap)

    def _compute_archive_points(self, now, weather, layout, lat, lon, cap, residual_map, analog_library):
        """Hourly predicted points over [now - LEARN_DAYS, current hour), as coordinator._compute_archive_points."""
        cutoff = now.replace(minute=0, second=0, microsecond=0)
        points = build_forecast_series(
            weather,
            layout,
            lat,
            lon,
            inverter_max_w=cap,
            start=cutoff - timedelta(days=LEARN_DAYS),
            end=cutoff,
            step_minutes=60,
            residual_map=residual_map,
        )
        return enrich_archive_points(points, analog_library, weather, lat, lon)

    async def _project_battery_soc(self, points, now) -> List[BatterySocPoint]:
        battery = battery_from_config(self.settings.entry)
        if battery is None:
            return []
        soc = self.source.live_soc()
        if soc is None:
            _LOGGER.info("Battery projection skipped: no live state of charge from the source")
            return []
        profile = self._consumption_profile_for(now)
        if profile is None:
            _LOGGER.info("Battery projection skipped: no consumption history from the source yet")
            return []
        return await asyncio.to_thread(
            partial(
                project_battery_soc,
                battery,
                soc / 100.0,
                points,
                profile,
                now=now,
                tz=self.settings.tz,
                horizon_hours=BATTERY_SOC_HORIZON_HOURS,
                step_minutes=STEP_MINUTES,
            )
        )

    def _consumption_profile_for(self, now) -> Optional[ConsumptionProfile]:
        """Rebuilt at most once an hour, keeping the last good one, as the coordinator does."""
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        if self._last_consumption_hour == this_hour:
            return self._consumption_profile
        self._last_consumption_hour = this_hour
        buckets = self.source.consumption(now - timedelta(days=LEARN_DAYS), now)
        if not buckets:
            return self._consumption_profile
        profile = build_consumption_profile(
            ConsumptionSources(signed={_CONSUMPTION_ID: 1}), {_CONSUMPTION_ID: buckets}, self.settings.tz
        )
        if profile is not None:
            self._consumption_profile = profile
        return self._consumption_profile

    def _record_day_ahead(self, now: datetime, summary: ForecastSummary) -> Dict[date, float]:
        """Tomorrow's predicted total, written down once before that day starts (coordinator._record_day_ahead)."""
        if not self._day_ahead_loaded:
            for key, value in ((self._skill_store.load() or {}).get("days") or {}).items():
                try:
                    self._day_ahead[date.fromisoformat(key)] = float(value)
                except (TypeError, ValueError):
                    continue
            self._day_ahead_loaded = True
        tomorrow = now.date() + timedelta(days=1)
        if tomorrow not in self._day_ahead and len(summary.days) > 1:
            self._day_ahead[tomorrow] = summary.days[1].energy_kwh
            oldest = now.date() - timedelta(days=SKILL_WINDOW_DAYS + 1)
            self._day_ahead = {d: kwh for d, kwh in self._day_ahead.items() if d >= oldest}
            self._skill_store.save({"days": {d.isoformat(): kwh for d, kwh in sorted(self._day_ahead.items())}})
        return self._day_ahead

    def _today_trend(self, now: datetime, summary: ForecastSummary) -> TodayTrend:
        """Today's predicted total against its frozen morning reference (coordinator._today_trend)."""
        today = now.date().isoformat()
        current = summary.days[0].energy_kwh if summary.days else 0.0
        if not self._trend_loaded:
            self._trend_loaded = True
            stored: Dict[str, Any] = self._trend_store.load() or {}
            try:
                if stored.get("date") and stored.get("captured_at"):
                    self._trend_ref = TrendReference(
                        date=str(stored["date"]),
                        kwh=float(stored["kwh"]),
                        captured_at=datetime.fromisoformat(str(stored["captured_at"])),
                    )
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Ignored an unreadable today-trend reference; today's trend starts over")
        if should_capture(self._trend_ref, today, now, trend_anchor_hour_from_config(self.settings.entry)):
            self._trend_ref = TrendReference(date=today, kwh=current, captured_at=datetime.now(timezone.utc))
            self._trend_store.save(
                {"date": today, "kwh": current, "captured_at": self._trend_ref.captured_at.isoformat()}
            )
        return compute_trend(self._trend_ref, current, today)
