"""Turn a Snapshot into the JSON the web UI, the API and the output files serve.

The series follows the integration's helios_forecast/series websocket command (websocket.py): the
hourly past archive, then today's elapsed stretch the archive does not cover yet, then the live
points from now on, with the same point fields. It is restated here rather than imported because
websocket.py imports Home Assistant.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, List, Optional

from custom_components.helios_forecast.forecast import ForecastPoint

from .engine import Snapshot

_ARCHIVE_STEP = timedelta(hours=1)
_TRANSLATIONS = (
    Path(__file__).resolve().parent.parent / "custom_components" / "helios_forecast" / "translations" / "en.json"
)


def _num(v: Optional[float], digits: int = 1) -> Optional[float]:
    return round(v, digits) if v is not None and math.isfinite(v) else None


def _iso(t: Optional[datetime]) -> Optional[str]:
    return t.isoformat() if t is not None else None


def merged_series(snapshot: Snapshot) -> List[ForecastPoint]:
    archive = snapshot.archive_points
    covered_until = archive[-1].t + _ARCHIVE_STEP if archive else None
    series = list(archive)
    series.extend(p for p in snapshot.elapsed_points if covered_until is None or p.t >= covered_until)
    live_after = series[-1].t if series else None
    series.extend(
        p
        for p in snapshot.points
        if (covered_until is None or p.t >= covered_until) and (live_after is None or p.t > live_after)
    )
    return series


def _point(p: ForecastPoint) -> Dict[str, Any]:
    return {
        "t": p.t.isoformat(),
        "pv_w": _num(p.pv_w),
        "pv_raw_w": _num(p.pv_raw_w),
        "pv_p10": _num(p.pv_p10),
        "pv_p90": _num(p.pv_p90),
        "ghi": _num(p.ghi),
        "cloud": _num(p.cloud),
    }


def series_view(snapshot: Snapshot, start: Optional[datetime] = None, end: Optional[datetime] = None) -> Dict[str, Any]:
    points = [
        _point(p) for p in merged_series(snapshot) if (start is None or p.t >= start) and (end is None or p.t < end)
    ]
    actual = [
        {
            "t": datetime.fromtimestamp(b.start_ms / 1000.0, tz=timezone.utc).isoformat(),
            "kwh": _num(b.kwh, 3),
            "curtailed": b.curtailed,
        }
        for b in snapshot.production
        if (start is None or b.end_ms > start.timestamp() * 1000.0)
        and (end is None or b.start_ms < end.timestamp() * 1000.0)
    ]
    battery = [
        {"t": p.t.isoformat(), "soc": _num(p.soc)}
        for p in snapshot.battery_soc
        if (start is None or p.t >= start) and (end is None or p.t < end)
    ]
    daily = [
        {"date": d.date, "kwh": _num(d.energy_kwh, 2), "kwh_raw": _num(d.energy_raw_kwh, 2)}
        for d in snapshot.summary.days
    ]
    return {
        "start": _iso(start),
        "end": _iso(end),
        "points": points,
        "actual": actual,
        "battery_soc": battery,
        "daily": daily,
    }


def daily_comparison(snapshot: Snapshot, tz: tzinfo) -> List[Dict[str, Any]]:
    """Per local day of the learning window and today: what the forecast said against what was measured.

    The predicted figure integrates the merged series (hourly archive, then 15-minute points), each
    point standing for the time up to the next one, capped at an hour. Measured is the sum of the
    source's hourly buckets; a day with no bucket at all has no measured figure rather than zero."""
    series = merged_series(snapshot)
    if not series:
        return []
    today = snapshot.refreshed_at.astimezone(tz).date()
    # The archive starts at an hour, not a midnight: its first local day is partial, so it is left out.
    first_local = series[0].t.astimezone(tz)
    first_day = (
        first_local.date() if first_local.time() == datetime.min.time() else first_local.date() + timedelta(days=1)
    )
    predicted: Dict[date, float] = {}
    for p, nxt in zip(series, series[1:] + [None]):
        day = p.t.astimezone(tz).date()
        if day > today:
            break
        if day < first_day:
            continue
        step_h = min((nxt.t - p.t).total_seconds() / 3600.0, 1.0) if nxt is not None else 0.25
        if math.isfinite(p.pv_w):
            predicted[day] = predicted.get(day, 0.0) + p.pv_w * step_h / 1000.0
    measured: Dict[date, float] = {}
    for b in snapshot.production:
        day = datetime.fromtimestamp(b.start_ms / 1000.0, tz=timezone.utc).astimezone(tz).date()
        measured[day] = measured.get(day, 0.0) + b.kwh
    return [
        {"date": d.isoformat(), "predicted_kwh": _num(predicted.get(d), 2), "measured_kwh": _num(measured.get(d), 2)}
        for d in sorted(set(predicted) | set(measured))
        if first_day <= d <= today
    ]


def _issue_texts() -> Dict[str, Dict[str, str]]:
    try:
        return json.loads(_TRANSLATIONS.read_text()).get("issues") or {}
    except (OSError, ValueError):
        return {}


_ISSUES = _issue_texts()


def problem_view(problem, site_name: str) -> Dict[str, Any]:
    text = _ISSUES.get(problem.key) or {}
    placeholders = {"entry": site_name, **problem.placeholders}

    def fill(s: str) -> str:
        try:
            return s.format(**placeholders)
        except (KeyError, IndexError, ValueError):
            return s

    return {
        "key": problem.key,
        "severity": problem.severity,
        "title": fill(text.get("title") or problem.key.replace("_", " ")),
        "description": fill(text.get("description") or ""),
    }


def summary_view(
    snapshot: Snapshot, *, site_name: str, tz_name: str, source_status, last_error: Optional[str]
) -> Dict[str, Any]:
    s = snapshot.summary
    r = snapshot.reliability
    t = snapshot.trend
    layout = snapshot.layout
    lines = []
    for i, o in enumerate(layout.orientations):
        share = layout.shares[i] if i < len(layout.shares) else None
        lines.append(
            {
                "azimuth": o.azimuth_deg,
                "tilt": o.tilt_deg,
                "tracker": o.tracker,
                "kwp": _num(share * layout.total_kwp, 2) if share is not None else None,
            }
        )
    status = asdict(source_status)
    status["last_sync"] = _iso(source_status.last_sync)
    return {
        "site": {
            "name": site_name,
            "timezone": tz_name,
            "lat": snapshot.lat,
            "lon": snapshot.lon,
            "total_kwp": layout.total_kwp,
            "lines": lines,
        },
        "refreshed_at": _iso(snapshot.refreshed_at),
        "last_error": last_error,
        "learning": snapshot.learning,
        "weather_reused": snapshot.weather_reused,
        "hours_learned": len(snapshot.production),
        "now": {
            "power_w": _num(s.power_now_w),
            "power_low_w": _num(s.power_now_low_w),
            "power_high_w": _num(s.power_now_high_w),
            "power_next_hour_w": _num(s.power_next_hour_w),
            "energy_today_remaining_kwh": _num(s.energy_today_remaining_kwh, 2),
            "energy_this_hour_kwh": _num(s.energy_this_hour_kwh, 3),
            "energy_next_hour_kwh": _num(s.energy_next_hour_kwh, 3),
        },
        "days": [
            {
                "date": d.date,
                "kwh": _num(d.energy_kwh, 2),
                "kwh_raw": _num(d.energy_raw_kwh, 2),
                "peak_w": _num(d.peak_power_w),
                "peak_time": _iso(d.peak_time),
                "reliability": _num(r.per_day[i]) if i < len(r.per_day) else None,
            }
            for i, d in enumerate(s.days)
        ],
        "reliability": {
            "overall": _num(r.overall),
            "data_maturity": _num(r.data_maturity, 3),
            "recent_skill": _num(r.recent_skill, 3),
            "today_predictability": _num(r.today_predictability, 3),
            "days_learned": r.days_learned,
        },
        "trend": {
            "delta_kwh": _num(t.delta_kwh, 2),
            "reference_kwh": _num(t.reference_kwh, 2),
            "reference_time": _iso(t.reference_time),
            "current_kwh": _num(t.current_kwh, 2),
            "direction": t.direction,
        },
        "battery": {"live_soc": _num(snapshot.live_soc), "projected": bool(snapshot.battery_soc)},
        "weather": {k: _num(v) for k, v in snapshot.observed.items()},
        "problems": [problem_view(p, site_name) for p in snapshot.problems],
        "source": status,
        "timings": {k: _num(v, 2) for k, v in snapshot.timings.items()},
    }


def solcast_view(snapshot: Snapshot) -> Dict[str, Any]:
    """The forecast from now on in Solcast's rooftop "forecasts" shape (30-minute periods, kW, period_end
    in UTC), so a tool already reading Solcast, FreePowerMaximiser among them, can read this instead."""
    now = snapshot.refreshed_at
    first_end = now.replace(minute=30 if now.minute < 30 else 0, second=0, microsecond=0)
    if now.minute >= 30:
        first_end += timedelta(hours=1)
    buckets: Dict[datetime, List[ForecastPoint]] = {}
    for p in snapshot.points:
        t = p.t.astimezone(timezone.utc)
        if t < first_end - timedelta(minutes=30):
            continue
        # A point stands for the 15 minutes that start at it; its half hour ends at the next :00 or :30.
        end = t.replace(minute=30 if t.minute < 30 else 0, second=0, microsecond=0)
        if t.minute >= 30:
            end += timedelta(hours=1)
        buckets.setdefault(end, []).append(p)

    def avg_kw(points: List[ForecastPoint], attr: str) -> Optional[float]:
        values = [getattr(p, attr) for p in points]
        values = [v for v in values if v is not None and math.isfinite(v)]
        return round(sum(values) / len(values) / 1000.0, 4) if values else None

    forecasts = []
    for end in sorted(buckets):
        pts = buckets[end]
        est = avg_kw(pts, "pv_w") or 0.0
        p10 = avg_kw(pts, "pv_p10")
        p90 = avg_kw(pts, "pv_p90")
        forecasts.append(
            {
                "period_end": end.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                "period": "PT30M",
                "pv_estimate": est,
                "pv_estimate10": p10 if p10 is not None else est,
                "pv_estimate90": p90 if p90 is not None else est,
            }
        )
    return {"forecasts": forecasts}
