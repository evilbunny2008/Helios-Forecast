from __future__ import annotations

import asyncio
import hashlib
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from helios_standalone import foxess
from helios_standalone.foxess import FoxessClient, FoxessError, clean_energy, parse_history_time, signature

SYD = ZoneInfo("Australia/Sydney")


def test_signature_joins_with_a_literal_backslash_r_backslash_n():
    expected = hashlib.md5(b"/op/v0/device/list" + b"\\r\\n" + b"KEY" + b"\\r\\n" + b"123").hexdigest()
    assert signature("/op/v0/device/list", "KEY", "123") == expected


def test_history_times_use_the_given_offset_or_the_site_zone():
    with_offset = parse_history_time("2026-10-08 13:05:00 AEDT+1100", SYD)
    assert with_offset == datetime(2026, 10, 8, 2, 5, tzinfo=timezone.utc)
    without = parse_history_time("2026-07-01 12:00:00", SYD)
    assert without.utcoffset() == timedelta(hours=10)
    assert parse_history_time("garbage", SYD) is None


@pytest.mark.parametrize(
    "value, expected", [(1.5, 1.5), ("2", 2.0), (None, None), (-1, None), (5e8, None), ("x", None)]
)
def test_clean_energy(value, expected):
    assert clean_energy(value) == expected


class FakeApi:
    """A stand-in Open API that records what it was asked and answers from a script."""

    def __init__(self):
        self.requests = []
        self.devices = ["SN1"]
        self.fail_next = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.path, body, dict(request.headers)))
        if self.fail_next:
            return web.json_response(self.fail_next.pop(0))
        if request.path == "/op/v0/device/list":
            return web.json_response({"errno": 0, "result": {"data": [{"deviceSN": d} for d in self.devices]}})
        if request.path == "/op/v0/device/report/query":
            return web.json_response(
                {
                    "errno": 0,
                    "result": [{"variable": v, "unit": "kWh", "values": [0.5] * 24} for v in body["variables"]],
                }
            )
        if request.path == "/op/v0/device/history/query":
            data = [
                {"time": "2026-10-08 10:05:00 AEDT+1100", "value": 50},
                {"time": "2026-10-08 10:55:00 AEDT+1100", "value": 72},
            ]
            return web.json_response({"errno": 0, "result": [{"datas": [{"variable": "SoC", "data": data}]}]})
        if request.path == "/op/v1/device/real/query":
            return web.json_response({"errno": 0, "result": [{"datas": [{"variable": "SoC", "value": 64.0}]}]})
        return web.json_response({"errno": 40257, "msg": "unknown path"})


def run_with_api(api: FakeApi, fn, **client_kwargs):
    async def go():
        server = TestServer(api.app())
        await server.start_server()
        try:
            async with ClientSession() as session:
                client = FoxessClient(
                    session,
                    "K" * 36,
                    tz_name="Australia/Sydney",
                    min_interval_s=0,
                    base_url=str(server.make_url("")),
                    **client_kwargs,
                )
                return await fn(client)
        finally:
            await server.close()

    return asyncio.run(go())


def test_reads_report_history_and_real_time_and_signs_every_call():
    api = FakeApi()

    async def go(client):
        report = await client.report_day(date(2026, 10, 8), ["PVEnergyTotal", "loads"])
        hist = await client.history(
            datetime(2026, 10, 8, tzinfo=SYD), datetime(2026, 10, 8, 23, 59, tzinfo=SYD), ["SoC"], SYD
        )
        real = await client.real(["SoC"])
        return report, hist, real

    report, hist, real = run_with_api(api, go)
    assert report == {"PVEnergyTotal": [0.5] * 24, "loads": [0.5] * 24}
    assert [v for _, v in hist["SoC"]] == [50.0, 72.0]
    assert real == {"SoC": 64.0}
    paths = [p for p, _, _ in api.requests]
    assert paths[0] == "/op/v0/device/list" and paths.count("/op/v0/device/list") == 1  # serial looked up once
    report_body = api.requests[1][1]
    assert report_body == {
        "sn": "SN1",
        "dimension": "day",
        "variables": ["PVEnergyTotal", "loads"],
        "year": 2026,
        "month": 10,
        "day": 8,
    }
    for path, _, headers in api.requests:
        assert headers["signature"] == signature(path, "K" * 36, headers["timestamp"])


def test_several_inverters_need_a_serial():
    api = FakeApi()
    api.devices = ["SN1", "SN2"]
    with pytest.raises(FoxessError, match="SN1, SN2"):
        run_with_api(api, lambda c: c.device_sn())
    assert run_with_api(api, lambda c: c.device_sn(), device_sn="SN2") == "SN2"


def test_errno_is_an_error_and_rate_limits_are_retried_once(monkeypatch):
    monkeypatch.setattr(foxess, "_RATE_LIMIT_BACKOFF_S", 0)
    api = FakeApi()
    api.fail_next = [{"errno": 41200, "msg": "too frequent"}]
    assert run_with_api(api, lambda c: c.real(["SoC"]), device_sn="SN1") == {"SoC": 64.0}
    api.fail_next = [{"errno": 41809, "msg": "invalid token"}]
    with pytest.raises(FoxessError, match="invalid token"):
        run_with_api(api, lambda c: c.real(["SoC"]), device_sn="SN1")


def test_hourly_energy_from_five_minute_power_samples():
    day = datetime(2026, 10, 8, tzinfo=SYD)
    # 2 kW from 10:00 to 12:00, sampled every 5 minutes; nothing before (the API sends no samples).
    samples = [(day + timedelta(hours=10, minutes=5 * i), 2.0) for i in range(24)]
    hours = foxess.hourly_energy(samples, day, 24)
    assert hours[10] == pytest.approx(2.0) and hours[11] == pytest.approx(2.0)
    assert hours[9] is None and hours[12] is None  # no samples is no data, not zero


def test_hourly_energy_splits_at_the_hour_and_leaves_thin_hours_out():
    day = datetime(2026, 10, 8, tzinfo=SYD)
    # Every 10 minutes from 13:30 to 15:00: 13:00 is half covered, 14:00 fully.
    samples = [(day + timedelta(hours=13, minutes=30 + 10 * i), -1.2) for i in range(10)]
    hours = foxess.hourly_energy(samples, day, 24)
    assert hours[13] is None
    assert hours[14] == pytest.approx(-1.2)  # signed as measured


def test_a_gap_does_not_stretch_one_reading():
    day = datetime(2026, 10, 8, tzinfo=SYD)
    samples = [(day + timedelta(hours=9), 3.0), (day + timedelta(hours=12), 3.0)]
    hours = foxess.hourly_energy(samples, day, 24)
    assert hours[9] is None and hours[10] is None and hours[11] is None
