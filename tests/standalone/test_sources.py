from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from helios_standalone.foxess import FoxessError
from helios_standalone.settings import CsvSettings, FoxessSettings
from helios_standalone.sources import CsvSource, FoxessSource, _hour_buckets

SYD = ZoneInfo("Australia/Sydney")


class FakeClient:
    def __init__(self, fail_days=(), fatal=False):
        self.calls = 0
        self.reports = []
        self.histories = []
        self.fail_days = set(fail_days)
        self.fatal = fatal

    async def report_day(self, day, variables):
        self.calls += 1
        self.reports.append(day)
        if self.fatal:
            raise FoxessError("/op/v0/device/list", None, "HTTP 401, the API key was refused", fatal=True)
        if day in self.fail_days:
            raise FoxessError("/op/v0/device/report/query", 40400, "busy")
        return {v: [1.0 if 8 <= h < 16 else 0.0 for h in range(24)] for v in variables}

    async def history(self, begin, end, variables, tz):
        self.calls += 1
        self.histories.append((begin.date(), tuple(variables)))
        out = {}
        if "SoC" in variables:
            out["SoC"] = [
                (begin + timedelta(hours=10, minutes=5), 40.0),
                (begin + timedelta(hours=10, minutes=50), 99.0),
            ]
        if "meterPower2" in variables:
            # A CT clamp fitted backwards: 1.5 kW of production read as -1.5, 09:00 to 15:00.
            out["meterPower2"] = [(begin + timedelta(hours=9, minutes=5 * i), -1.5) for i in range(72)]
        return out

    async def real(self, variables):
        self.calls += 1
        return {"SoC": 55.0}


def make_source(tmp_path: Path, client, battery=True):
    return FoxessSource(client, FoxessSettings(api_key="k"), SYD, tmp_path / "foxess", battery=battery)


def test_finished_days_are_fetched_once(tmp_path: Path):
    client = FakeClient()
    now = datetime(2026, 10, 8, 14, 30, tzinfo=SYD)
    source = make_source(tmp_path, client)
    asyncio.run(source.sync(now, 3))
    assert sorted(client.reports) == [date(2026, 10, d) for d in range(5, 9)]
    assert source.live_soc() == 55.0

    # A fresh process the next day reads the finished days from disk: only today and yesterday
    # (fetched before it was settled) go back to the API.
    client2 = FakeClient()
    source2 = make_source(tmp_path, client2)
    asyncio.run(source2.sync(now + timedelta(days=1), 3))
    assert sorted(client2.reports) == [date(2026, 10, 8), date(2026, 10, 9)]
    assert sorted(d for d, _ in client2.histories) == [date(2026, 10, 8), date(2026, 10, 9)]
    assert source2.status.ok and source2.status.days_cached == 4
    assert not (tmp_path / "foxess" / "report_2026-10-05.json").exists()  # pruned once outside the window


def test_buckets_only_complete_hours_in_the_window(tmp_path: Path):
    source = make_source(tmp_path, FakeClient())
    now = datetime(2026, 10, 8, 10, 30, tzinfo=SYD)
    asyncio.run(source.sync(now, 2))
    buckets = source.production(now - timedelta(days=2), now)
    today = [b for b in buckets if datetime.fromtimestamp(b.start_ms / 1000, SYD).date() == now.date()]
    # 08:00 and 09:00 are complete; 10:00 is still running.
    assert [datetime.fromtimestamp(b.start_ms / 1000, SYD).hour for b in today if b.kwh > 0] == [8, 9]
    assert max(b.end_ms for b in buckets) <= now.timestamp() * 1000
    assert source.consumption(now - timedelta(days=2), now)


def test_soc_maximum_per_hour(tmp_path: Path):
    source = make_source(tmp_path, FakeClient())
    now = datetime(2026, 10, 8, 20, 0, tzinfo=SYD)
    asyncio.run(source.sync(now, 1))
    soc = source.soc_max_by_hour(now - timedelta(days=1), now)
    ten = datetime(2026, 10, 8, 10, tzinfo=SYD).timestamp() * 1000
    assert soc[ten] == 99.0


def test_a_failed_day_is_reported_and_the_rest_still_learns(tmp_path: Path):
    now = datetime(2026, 10, 8, 14, 0, tzinfo=SYD)
    source = make_source(tmp_path, FakeClient(fail_days={date(2026, 10, 7)}), battery=False)
    asyncio.run(source.sync(now, 3))
    assert not source.status.ok and "busy" in source.status.last_error
    days = {
        datetime.fromtimestamp(b.start_ms / 1000, SYD).date() for b in source.production(now - timedelta(days=3), now)
    }
    assert date(2026, 10, 7) not in days and date(2026, 10, 6) in days
    assert source.consumption(now - timedelta(days=3), now) is None


def test_daylight_saving_days_have_their_real_length():
    start, end = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert len(_hour_buckets(date(2026, 10, 4), SYD, [1.0] * 24, start, end)) == 23  # clocks forward
    assert len(_hour_buckets(date(2026, 4, 5), SYD, [1.0] * 25, start, end)) == 25  # clocks back


def test_csv_source(tmp_path: Path):
    path = tmp_path / "p.csv"
    path.write_text("start,kwh\n2026-10-08T10:00:00+11:00,1.5\n# a comment\n2026-10-08T09:00:00+11:00,1.0\n")
    source = CsvSource(CsvSettings(production_path=str(path)))
    asyncio.run(source.sync(datetime.now(timezone.utc), 60))
    buckets = source.production(datetime(2026, 10, 1, tzinfo=SYD), datetime(2026, 10, 9, tzinfo=SYD))
    assert [b.kwh for b in buckets] == [1.0, 1.5]
    path.write_text("2026-10-08T10:00:00,1.5\n")
    asyncio.run(source.sync(datetime.now(timezone.utc), 60))
    assert not source.status.ok and "UTC offset" in source.status.last_error


def test_a_refused_key_stops_at_the_first_call_and_keeps_the_cache(tmp_path: Path):
    now = datetime(2026, 10, 8, 14, 0, tzinfo=SYD)
    asyncio.run(make_source(tmp_path, FakeClient(), battery=False).sync(now - timedelta(days=1), 10))
    client = FakeClient(fatal=True)
    source = make_source(tmp_path, client, battery=False)
    asyncio.run(source.sync(now, 10))
    assert client.calls == 1
    assert "refused" in source.status.last_error
    # The days fetched the day before are still there to learn from.
    assert (
        len(
            {
                datetime.fromtimestamp(b.start_ms / 1000, SYD).date()
                for b in source.production(now - timedelta(days=10), now)
            }
        )
        >= 9
    )


def test_repeated_failures_stop_the_backfill(tmp_path: Path):
    now = datetime(2026, 10, 8, 14, 0, tzinfo=SYD)
    client = FakeClient(fail_days={now.date() - timedelta(days=i) for i in range(60)})
    source = make_source(tmp_path, client, battery=False)
    asyncio.run(source.sync(now, 59))
    assert client.calls == 3 and not source.status.ok


def test_a_ct_clamp_power_variable_is_integrated_and_its_sign_applied(tmp_path: Path):
    now = datetime(2026, 10, 8, 20, 0, tzinfo=SYD)
    client = FakeClient()
    settings = FoxessSettings(api_key="k", production_variable="meterPower2", production_invert=True)
    source = FoxessSource(client, settings, SYD, tmp_path / "foxess", battery=False)
    asyncio.run(source.sync(now, 2))
    assert client.reports == []  # nothing asked of the report: meterPower2 is not one of its variables
    assert {v for _, v in client.histories} == {("meterPower2",)}
    midnight_ms = datetime(2026, 10, 8, tzinfo=SYD).timestamp() * 1000
    today = [b for b in source.production(now - timedelta(days=2), now) if b.start_ms >= midnight_ms]
    by_hour = {datetime.fromtimestamp(b.start_ms / 1000, SYD).hour: b.kwh for b in today}
    assert set(by_hour) == set(range(9, 15)) and all(abs(k - 1.5) < 1e-6 for k in by_hour.values())

    # Without the flip the same readings are negative, and production never is: every hour is zero.
    plain_settings = FoxessSettings(api_key="k", production_variable="meterPower2")
    plain = FoxessSource(client, plain_settings, SYD, tmp_path / "foxess", battery=False)
    asyncio.run(plain.sync(now, 2))
    assert all(b.kwh == 0.0 for b in plain.production(now - timedelta(days=2), now))
