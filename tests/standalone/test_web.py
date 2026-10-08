from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from conftest import base_config, make_weather
from helios_standalone.engine import Engine
from helios_standalone.settings import parse_settings
from helios_standalone.sources import Source
from helios_standalone.web import make_app

NOW = datetime(2026, 10, 8, 3, 20, tzinfo=timezone.utc)


def test_api(tmp_path: Path):
    settings = parse_settings(base_config(), tmp_path)

    async def fetch(session, lat, lon, **kw):
        return make_weather(NOW)

    engine = Engine(settings, Source(), session=None, weather_fetcher=fetch, clock=lambda: NOW)

    async def go():
        async with TestClient(TestServer(make_app(engine))) as client:
            assert (await client.get("/api/summary")).status == 503
            assert (await client.get("/health")).status == 503
            page = await client.get("/")
            assert page.status == 200 and "Helios Forecast" in await page.text()

            refreshed = await client.post("/api/refresh")
            assert refreshed.status == 200
            assert (await refreshed.json())["days"][0]["date"] == "2026-10-08"
            assert (tmp_path / "data" / "out" / "solcast.json").exists()

            day = await (await client.get("/api/series?day=2026-10-09&days=1")).json()
            assert day["start"] == "2026-10-09T00:00:00+11:00" and day["end"] == "2026-10-10T00:00:00+11:00"
            assert len(day["points"]) == 96  # one local day of 15-minute points
            assert (await client.get("/api/series?day=tomorrow")).status == 400
            assert (await client.get("/api/series?start=2026-10-09T00:00:00")).status == 400
            assert (await client.get("/api/daily")).status == 200
            assert (await client.get("/api/solcast.json")).status == 200
            assert (await client.get("/health")).status == 200

    asyncio.run(go())
