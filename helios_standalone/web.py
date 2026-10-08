"""The web UI and JSON API, plus the output files written after every refresh."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web

from .engine import Engine
from .views import daily_comparison, series_view, solcast_view, summary_view

_LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"

ENGINE_KEY = web.AppKey("engine", Engine)


def _summary(engine: Engine) -> Dict[str, Any]:
    assert engine.snapshot is not None
    return summary_view(
        engine.snapshot,
        site_name=engine.settings.name,
        tz_name=engine.settings.tz.key,
        source_status=engine.source.status,
        last_error=engine.last_error,
    )


def write_outputs(engine: Engine) -> None:
    """summary.json, series.json and solcast.json under <data_dir>/out, for tools that read files."""
    if engine.snapshot is None:
        return
    out = engine.settings.data_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("summary.json", _summary(engine)),
        ("series.json", series_view(engine.snapshot)),
        ("solcast.json", solcast_view(engine.snapshot)),
    ):
        tmp = out / (name + ".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(out / name)


def _not_ready(engine: Engine) -> web.Response:
    return web.json_response(
        {"error": "no forecast yet", "last_error": engine.last_error}, status=503, headers={"Retry-After": "30"}
    )


def _parse_time(request: web.Request, key: str) -> Optional[datetime]:
    value = request.query.get(key)
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        raise web.HTTPBadRequest(text=f"'{key}' is not an ISO 8601 time") from None
    if t.tzinfo is None:
        raise web.HTTPBadRequest(text=f"'{key}' must include a UTC offset")
    return t


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def api_summary(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    if engine.snapshot is None:
        return _not_ready(engine)
    return web.json_response(_summary(engine))


def _day_range(request: web.Request, engine: Engine) -> tuple[Optional[datetime], Optional[datetime]]:
    """`day=YYYY-MM-DD&days=N`: N whole local days of the site from that day, in the site's time zone."""
    try:
        first = date.fromisoformat(request.query["day"])
        days = int(request.query.get("days", "1"))
    except ValueError:
        raise web.HTTPBadRequest(text="'day' must be YYYY-MM-DD and 'days' a whole number") from None
    if not 1 <= days <= 70:
        raise web.HTTPBadRequest(text="'days' must be between 1 and 70")
    tz = engine.settings.tz
    start = datetime(first.year, first.month, first.day, tzinfo=tz)
    last = first + timedelta(days=days)
    return start, datetime(last.year, last.month, last.day, tzinfo=tz)


async def api_series(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    if engine.snapshot is None:
        return _not_ready(engine)
    if "day" in request.query:
        start, end = _day_range(request, engine)
    else:
        start, end = _parse_time(request, "start"), _parse_time(request, "end")
    return web.json_response(series_view(engine.snapshot, start, end))


async def api_daily(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    if engine.snapshot is None:
        return _not_ready(engine)
    return web.json_response({"days": daily_comparison(engine.snapshot, engine.settings.tz)})


async def api_solcast(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    if engine.snapshot is None:
        return _not_ready(engine)
    return web.json_response(solcast_view(engine.snapshot))


async def api_refresh(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    try:
        await engine.refresh()
    except Exception as err:  # noqa: BLE001 - reported to the caller, the server keeps the last forecast
        _LOGGER.warning("Refresh failed: %s", err)
        return web.json_response({"error": str(err)}, status=502)
    write_outputs(engine)
    return web.json_response(_summary(engine))


async def health(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    snapshot = engine.snapshot
    ok = snapshot is not None
    return web.json_response(
        {
            "ok": ok,
            "refreshed_at": snapshot.refreshed_at.isoformat() if snapshot is not None else None,
            "last_error": engine.last_error,
        },
        status=200 if ok else 503,
    )


def make_app(engine: Engine) -> web.Application:
    app = web.Application()
    app[ENGINE_KEY] = engine
    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/summary", api_summary),
            web.get("/api/series", api_series),
            web.get("/api/daily", api_daily),
            web.get("/api/solcast.json", api_solcast),
            web.post("/api/refresh", api_refresh),
            web.get("/health", health),
            web.static("/static", STATIC_DIR),
        ]
    )
    return app
