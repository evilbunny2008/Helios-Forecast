"""Command line: `python -m helios_standalone [serve|once|check] -c config.toml`.

serve  refresh on a timer and serve the web UI and API (the default)
once   run one refresh, write the output files, print the daily totals
check  read the configuration and, for FoxESS, make one call to prove the key and the inverter
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession, ClientTimeout, web

from custom_components.helios_forecast.config import layout_from_config

from .engine import Engine
from .foxess import REPORT_VARIABLES, FoxessClient, FoxessError, hourly_energy
from .settings import FoxessSettings, Settings, SettingsError, load_settings
from .sources import CsvSource, FoxessSource, Source
from .web import make_app, write_outputs

_LOGGER = logging.getLogger("helios_standalone")
# Minutes past the hour of the first refresh slot: off the hour, when every scheduled job and every
# weather model update lands at once (see the integration's __init__.py).
SLOT_OFFSET_MIN = 7


def _foxess_client(settings: Settings, fox: FoxessSettings, session: ClientSession) -> FoxessClient:
    return FoxessClient(
        session, fox.api_key, device_sn=fox.device_sn, tz_name=settings.tz.key, min_interval_s=fox.min_interval_s
    )


def build_source(settings: Settings, session: ClientSession) -> Source:
    if settings.foxess is not None:
        client = _foxess_client(settings, settings.foxess, session)
        return FoxessSource(
            client, settings.foxess, settings.tz, settings.data_dir / "foxess", battery=settings.battery_enabled
        )
    if settings.csv is not None:
        return CsvSource(settings.csv)
    return Source()


def next_slot(now: datetime, every_min: int) -> datetime:
    """The next refresh time: every `every_min` minutes from SLOT_OFFSET_MIN past the hour."""
    slot = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=SLOT_OFFSET_MIN % every_min)
    while slot <= now:
        slot += timedelta(minutes=every_min)
    return slot


async def _refresh_and_write(engine: Engine) -> bool:
    try:
        snapshot = await engine.refresh()
    except Exception as err:  # noqa: BLE001 - logged; the server keeps serving the last forecast
        _LOGGER.error("Refresh failed: %s", err)
        return False
    write_outputs(engine)
    today = snapshot.summary.days[0] if snapshot.summary.days else None
    _LOGGER.info(
        "Refreshed in %.1fs: today %.1f kWh, reliability %.0f, %s",
        snapshot.timings.get("total_s", 0.0),
        today.energy_kwh if today else 0.0,
        snapshot.reliability.overall,
        "learning from %d hours" % len(snapshot.production) if snapshot.learning else "physical model only",
    )
    return True


async def serve(settings: Settings) -> None:
    async with ClientSession(timeout=ClientTimeout(total=60)) as session:
        engine = Engine(settings, build_source(settings, session), session)
        runner = web.AppRunner(make_app(engine), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, settings.host, settings.port)
        await site.start()
        _LOGGER.info("Web UI on http://%s:%d/", settings.host, settings.port)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover - Windows
                pass

        async def scheduler() -> None:
            await _refresh_and_write(engine)
            while True:
                now = datetime.now(timezone.utc)
                # A failed refresh retries sooner than a full interval, so a brief outage is brief here too.
                wait = (next_slot(now, settings.refresh_minutes) - now).total_seconds()
                if engine.last_error is not None:
                    wait = min(wait, 300.0)
                await asyncio.sleep(wait)
                await _refresh_and_write(engine)

        task = asyncio.create_task(scheduler())
        await stop.wait()
        task.cancel()
        await runner.cleanup()


async def once(settings: Settings) -> int:
    async with ClientSession(timeout=ClientTimeout(total=60)) as session:
        engine = Engine(settings, build_source(settings, session), session)
        if not await _refresh_and_write(engine):
            return 1
        snapshot = engine.snapshot
        assert snapshot is not None
        for day in snapshot.summary.days:
            print(
                f"{day.date}  {day.energy_kwh:6.1f} kWh  (model {day.energy_raw_kwh:6.1f}, peak {day.peak_power_w:5.0f} W)"
            )
        for problem in snapshot.problems:
            print(f"{problem.severity}: {problem.key} {problem.placeholders or ''}")
        print(f"Output written to {settings.data_dir / 'out'}")
        return 0


async def check(settings: Settings) -> int:
    layout = layout_from_config(settings.entry)
    print(
        f"Site {settings.name}: {settings.latitude}, {settings.longitude} ({settings.tz.key}), {layout.total_kwp:g} kWp"
    )
    for i, o in enumerate(layout.orientations, start=1):
        print(f"  array {i}: tilt {o.tilt_deg:g}, azimuth {o.azimuth_deg:g}, share {layout.shares[i - 1]:.0%}")
    print(f"History source: {settings.source}; data in {settings.data_dir}")
    fox = settings.foxess
    if fox is not None:
        async with ClientSession(timeout=ClientTimeout(total=60)) as session:
            client = _foxess_client(settings, fox, session)
            var = fox.production_variable
            now = datetime.now(settings.tz)
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            try:
                sn = await client.device_sn()
                if var in REPORT_VARIABLES:
                    today = (await client.report_day(now.date(), [var])).get(var) or []
                else:
                    samples = (await client.history(midnight, now, [var], settings.tz)).get(var) or []
                    if not samples:
                        print(f"FoxESS: inverter {sn} returned no '{var}' samples today; check the variable name")
                        return 1
                    today = hourly_energy(samples, midnight, 24)
            except FoxessError as err:
                print(f"FoxESS: {err}")
                return 1
            total = sum(v or 0 for v in today)
            if var not in REPORT_VARIABLES and fox.production_invert:
                total = -total
            print(f"FoxESS: inverter {sn}, {var} today so far {total:.2f} kWh")
            if var not in REPORT_VARIABLES and total < -0.05:
                print(
                    "  That is negative: the clamp reads production the other way round. Set production_invert = true."
                )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="helios_standalone", description="Helios Forecast without Home Assistant")
    parser.add_argument("command", nargs="?", default="serve", choices=("serve", "once", "check"))
    parser.add_argument("-c", "--config", default="config.toml", help="configuration file (default: config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = load_settings(args.config)
    except SettingsError as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 2
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "check":
        return asyncio.run(check(settings))
    if args.command == "once":
        return asyncio.run(once(settings))
    asyncio.run(serve(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
