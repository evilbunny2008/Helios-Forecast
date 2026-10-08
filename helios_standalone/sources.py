"""Where the measured history comes from: the part the Home Assistant recorder plays in the integration.

The model learns from hourly produced energy (ProductionBucket), flags hours a full battery held the
inverter back from the hourly maximum state of charge, and projects the battery from a live state of
charge and the house's hourly consumption. A source supplies those four things; anything it cannot
supply it answers with None and the matching feature stays off, exactly as a missing entity does in
Home Assistant.

FoxESS days are cached on disk: a finished day is fetched once, so after the first backfill a refresh
costs a few calls (today's report and samples, the live state of charge), well inside the Open API's
daily allowance.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from custom_components.helios_forecast.solar.residual import ProductionBucket

from .foxess import FoxessClient, FoxessError
from .settings import CsvSettings, FoxessSettings

_LOGGER = logging.getLogger(__name__)

# A day is final once it was fetched this long after it ended: the cloud settles late samples first.
_SETTLE = timedelta(hours=2)
# Bounds one refresh's backfill, so a first start on a slow API cannot hold the forecast up for long;
# whatever is left is fetched on the next refresh.
MAX_CALLS_PER_SYNC = 150


@dataclass
class SourceStatus:
    name: str
    ok: bool = True
    last_error: Optional[str] = None
    last_sync: Optional[datetime] = None
    days_cached: int = 0
    api_calls: int = 0
    detail: Dict[str, str] = field(default_factory=dict)


class Source:
    """No measurements: the forecast runs on the physical model alone."""

    name = "none"

    def __init__(self) -> None:
        self.status = SourceStatus(self.name)

    async def sync(self, now: datetime, learn_days: int) -> None:
        return None

    def production(self, start: datetime, end: datetime) -> Optional[List[ProductionBucket]]:
        return None

    def consumption(self, start: datetime, end: datetime) -> Optional[List[ProductionBucket]]:
        return None

    def soc_max_by_hour(self, start: datetime, end: datetime) -> Optional[Dict[float, float]]:
        return None

    def live_soc(self) -> Optional[float]:
        return None


def _hour_buckets(
    day: date, tz: ZoneInfo, values: List[Optional[float]], start: datetime, end: datetime
) -> List[ProductionBucket]:
    """One local day's hourly values as buckets, keeping only complete hours inside [start, end)."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)
    next_midnight = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    hours = int((next_midnight - midnight).total_seconds() // 3600)
    out = []
    for i, kwh in enumerate(values[:hours]):
        if kwh is None:
            continue
        b_start = midnight + timedelta(hours=i)
        b_end = b_start + timedelta(hours=1)
        if b_start < start or b_end > end:
            continue
        out.append(ProductionBucket(start_ms=b_start.timestamp() * 1000.0, end_ms=b_end.timestamp() * 1000.0, kwh=kwh))
    return out


class FoxessSource(Source):
    name = "foxess"

    def __init__(
        self, client: FoxessClient, settings: FoxessSettings, tz: ZoneInfo, cache_dir: Path, *, battery: bool
    ) -> None:
        super().__init__()
        self._client = client
        self._settings = settings
        self._tz = tz
        self._dir = cache_dir
        self._battery = battery
        self._reports: Dict[date, Dict] = {}
        self._soc: Dict[date, Dict] = {}
        self._live_soc: Optional[float] = None
        self._dir.mkdir(parents=True, exist_ok=True)

    # --- cache ---------------------------------------------------------------------------------

    def _path(self, kind: str, day: date) -> Path:
        return self._dir / f"{kind}_{day.isoformat()}.json"

    def _load(self, kind: str, day: date) -> Optional[Dict]:
        try:
            return json.loads(self._path(kind, day).read_text())
        except (OSError, ValueError):
            return None

    def _save(self, kind: str, day: date, payload: Dict) -> None:
        path = self._path(kind, day)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)

    def _final(self, cached: Optional[Dict], day: date) -> bool:
        if not cached or not cached.get("fetched_at"):
            return False
        try:
            fetched = datetime.fromisoformat(cached["fetched_at"])
        except ValueError:
            return False
        day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=self._tz)
        return fetched >= day_end + _SETTLE

    def _prune(self, oldest: date) -> None:
        for path in self._dir.glob("*_*.json"):
            try:
                day = date.fromisoformat(path.stem.split("_", 1)[1])
            except ValueError:
                continue
            if day < oldest:
                path.unlink(missing_ok=True)

    # --- sync ----------------------------------------------------------------------------------

    async def sync(self, now: datetime, learn_days: int) -> None:
        """Bring the cache up to date: today, then every day of the window not yet final, newest first."""
        today = now.astimezone(self._tz).date()
        # The learning window starts learn_days before now, inside the local day learn_days ago.
        oldest = today - timedelta(days=learn_days)
        days = [today - timedelta(days=i) for i in range(learn_days + 1)]
        report_vars = [self._settings.production_variable]
        if self._battery:
            report_vars.append(self._settings.consumption_variable)
        # Everything already on disk first, so an API that is down still leaves the learning its history.
        for day in days:
            if (cached := self._load("report", day)) is not None:
                self._reports[day] = cached
            if self._battery and (cached_soc := self._load("soc", day)) is not None:
                self._soc[day] = cached_soc

        # Then what is missing or not final yet, newest first. A refused key or a missing inverter stops
        # at once, and so do a few failures in a row: every further call would only spend the quota.
        work = []
        for day in days:
            cached = self._reports.get(day)
            if not self._final(cached, day) or any(v not in (cached or {}).get("vars", {}) for v in report_vars):
                work.append(("report", day))
            if self._battery and not self._final(self._soc.get(day), day):
                work.append(("soc", day))
        calls = 0
        failures_in_a_row = 0
        errors: List[str] = []
        try:
            if self._battery:
                calls += 1
                self._live_soc = (await self._client.real(["SoC"])).get("SoC")
            for kind, day in work:
                if calls >= MAX_CALLS_PER_SYNC:
                    errors.append(f"backfill paused after {calls} calls, it continues next refresh")
                    break
                if failures_in_a_row >= 3:
                    errors.append("stopped after 3 failed calls in a row, retrying next refresh")
                    break
                calls += 1
                try:
                    if kind == "report":
                        values = await self._client.report_day(day, report_vars)
                        payload = {"fetched_at": datetime.now(self._tz).isoformat(), "vars": values}
                        self._reports[day] = payload
                    else:
                        payload = await self._fetch_soc(day)
                        self._soc[day] = payload
                except FoxessError as err:
                    if err.fatal:
                        raise
                    errors.append(str(err))
                    failures_in_a_row += 1
                    continue
                failures_in_a_row = 0
                self._save(kind, day, payload)
        except FoxessError as err:
            errors.insert(0, str(err))
        self._prune(oldest)
        self._reports = {d: v for d, v in self._reports.items() if d >= oldest}
        self._soc = {d: v for d, v in self._soc.items() if d >= oldest}
        self.status.ok = not errors
        self.status.last_error = errors[0] if errors else None
        self.status.last_sync = datetime.now(timezone.utc)
        self.status.days_cached = len(self._reports)
        self.status.api_calls = self._client.calls
        if errors:
            _LOGGER.warning("FoxESS sync: %s%s", errors[0], f" (and {len(errors) - 1} more)" if len(errors) > 1 else "")

    async def _fetch_soc(self, day: date) -> Dict:
        begin = datetime(day.year, day.month, day.day, tzinfo=self._tz)
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=self._tz) - timedelta(seconds=1)
        samples = (await self._client.history(begin, end, ["SoC"], self._tz)).get("SoC") or []
        hourly: Dict[str, float] = {}
        for t, v in samples:
            hour = t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
            hourly[hour] = max(hourly.get(hour, v), v)
        return {"fetched_at": datetime.now(self._tz).isoformat(), "max": hourly}

    # --- reads ---------------------------------------------------------------------------------

    def _buckets(self, var: str, start: datetime, end: datetime) -> Optional[List[ProductionBucket]]:
        if not self._reports:
            return None
        out: List[ProductionBucket] = []
        for day in sorted(self._reports):
            values = self._reports[day].get("vars", {}).get(var)
            if values:
                out.extend(_hour_buckets(day, self._tz, values, start, end))
        return out

    def production(self, start, end):
        return self._buckets(self._settings.production_variable, start, end)

    def consumption(self, start, end):
        return self._buckets(self._settings.consumption_variable, start, end) if self._battery else None

    def soc_max_by_hour(self, start, end):
        if not self._battery or not self._soc:
            return None
        out: Dict[float, float] = {}
        for cached in self._soc.values():
            for hour, v in (cached.get("max") or {}).items():
                t = datetime.fromisoformat(hour)
                if start <= t < end:
                    out[t.timestamp() * 1000.0] = float(v)
        return out

    def live_soc(self):
        return self._live_soc


def read_hourly_csv(path: str) -> List[ProductionBucket]:
    """Rows of "start,kwh" (a header row is skipped); start must carry a UTC offset."""
    out: List[ProductionBucket] = []
    with open(path, newline="") as f:
        for n, row in enumerate(csv.reader(f), start=1):
            if not row or row[0].strip().startswith("#"):
                continue
            try:
                start = datetime.fromisoformat(row[0].strip())
                kwh = float(row[1])
            except (ValueError, IndexError):
                if n == 1:
                    continue  # header
                raise ValueError(f"{path}:{n}: expected 'start,kwh', got {row!r}") from None
            if start.tzinfo is None:
                raise ValueError(f"{path}:{n}: {row[0]!r} has no UTC offset")
            ms = start.timestamp() * 1000.0
            out.append(ProductionBucket(start_ms=ms, end_ms=ms + 3_600_000.0, kwh=kwh))
    return sorted(out, key=lambda b: b.start_ms)


class CsvSource(Source):
    """Hourly history from files, for inverters without a supported API (or for trying the model out)."""

    name = "csv"

    def __init__(self, settings: CsvSettings) -> None:
        super().__init__()
        self._settings = settings
        self._production: List[ProductionBucket] = []
        self._consumption: Optional[List[ProductionBucket]] = None

    async def sync(self, now, learn_days):
        try:
            self._production = read_hourly_csv(self._settings.production_path)
            if self._settings.consumption_path:
                self._consumption = read_hourly_csv(self._settings.consumption_path)
            self.status.ok, self.status.last_error = True, None
        except (OSError, ValueError) as err:
            self.status.ok, self.status.last_error = False, str(err)
            _LOGGER.warning("CSV source: %s", err)
        self.status.last_sync = datetime.now(timezone.utc)

    @staticmethod
    def _window(buckets: List[ProductionBucket], start: datetime, end: datetime) -> List[ProductionBucket]:
        s, e = start.timestamp() * 1000.0, end.timestamp() * 1000.0
        return [b for b in buckets if b.start_ms >= s and b.end_ms <= e]

    def production(self, start, end):
        return self._window(self._production, start, end)

    def consumption(self, start, end):
        return self._window(self._consumption, start, end) if self._consumption is not None else None
