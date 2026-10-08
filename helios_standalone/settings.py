"""Read the TOML configuration into the inputs the forecast model and the runner need.

The model's own config helpers (custom_components/helios_forecast/config.py) read the flat dict a
Home Assistant config entry stores, so the site and panel sections are translated into exactly that
dict and every resolution rule (kWp shares, caps, defaults) stays the integration's.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from custom_components.helios_forecast.config import (
    CONF_ARRAYS,
    CONF_AZIMUTH,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_EFFICIENCY,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_SOC_ENTITY,
    CONF_INVERTER_MAX_KW,
    CONF_KWP,
    CONF_LATITUDE,
    CONF_LINE_INVERTER_MAX_KW,
    CONF_LONGITUDE,
    CONF_PRODUCTION_ENTITY,
    CONF_TILT,
    CONF_TRACKER,
    CONF_TREND_ANCHOR_HOUR,
)

SOURCES = ("foxess", "csv", "none")


class SettingsError(ValueError):
    """The configuration file cannot be used as written."""


@dataclass(frozen=True)
class FoxessSettings:
    api_key: str
    # Empty picks the only inverter on the account (and fails, naming them, when there are several).
    device_sn: str = ""
    # Report variable read as the panels' hourly production. PVEnergyTotal is the DC yield of the
    # strings; "generation" is the inverter's AC output, which on a hybrid includes battery discharge
    # and would teach the model that the roof produces at night.
    production_variable: str = "PVEnergyTotal"
    # Report variable read as the house's hourly consumption, for the battery projection.
    consumption_variable: str = "loads"
    # Minimum seconds between two calls: the Open API refuses bursts.
    min_interval_s: float = 2.0


@dataclass(frozen=True)
class CsvSettings:
    # Hourly production: "start,kwh" rows, start an ISO timestamp with a UTC offset.
    production_path: str
    # Optional hourly consumption in the same format, for the battery projection.
    consumption_path: str = ""


@dataclass(frozen=True)
class Settings:
    name: str
    latitude: float
    longitude: float
    tz: ZoneInfo
    # The flat dict the model's config helpers read, as a config entry would store it.
    entry: Dict[str, Any]
    source: str
    data_dir: Path
    host: str = "0.0.0.0"
    port: int = 8099
    refresh_minutes: int = 30
    foxess: Optional[FoxessSettings] = None
    csv: Optional[CsvSettings] = None
    battery_enabled: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


def _float(section: Dict[str, Any], key: str, where: str, required: bool = False) -> Optional[float]:
    value = section.get(key)
    if value is None or value == "":
        if required:
            raise SettingsError(f"[{where}] {key} is required")
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SettingsError(f"[{where}] {key} must be a number, got {value!r}") from None


def _lines(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    lines = []
    for i, arr in enumerate(raw, start=1):
        where = f"arrays #{i}"
        line: Dict[str, Any] = {
            CONF_TILT: _float(arr, "tilt", where, required=True),
            CONF_AZIMUTH: _float(arr, "azimuth", where, required=True),
            CONF_KWP: _float(arr, "kwp", where, required=True),
            CONF_TRACKER: str(arr.get("tracker") or "none"),
        }
        for key, conf in (
            ("inverter_max_kw", CONF_LINE_INVERTER_MAX_KW),
            ("latitude", CONF_LATITUDE),
            ("longitude", CONF_LONGITUDE),
        ):
            value = _float(arr, key, where)
            if value is not None:
                line[conf] = value
        lines.append(line)
    return lines


def parse_settings(raw: Dict[str, Any], base_dir: Path) -> Settings:
    """Validate the parsed TOML and resolve it into Settings."""
    site = raw.get("site") or {}
    lat = _float(site, "latitude", "site", required=True)
    lon = _float(site, "longitude", "site", required=True)
    tz_name = site.get("timezone")
    if not tz_name:
        raise SettingsError('[site] timezone is required, e.g. "Australia/Sydney"')
    try:
        tz = ZoneInfo(str(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        raise SettingsError(f"[site] timezone {tz_name!r} is not a known IANA time zone") from None

    arrays = raw.get("arrays") or []
    if not isinstance(arrays, list) or not arrays:
        raise SettingsError("at least one [[arrays]] section is required")

    source = str((raw.get("source") or {}).get("type") or "none").lower()
    if source not in SOURCES:
        raise SettingsError(f"[source] type must be one of {', '.join(SOURCES)}, got {source!r}")

    entry: Dict[str, Any] = {CONF_LATITUDE: lat, CONF_LONGITUDE: lon, CONF_ARRAYS: _lines(arrays)}
    for key, conf in (("inverter_max_kw", CONF_INVERTER_MAX_KW), ("trend_anchor_hour", CONF_TREND_ANCHOR_HOUR)):
        value = _float(site, key, "site")
        if value is not None:
            entry[conf] = value

    foxess = csv = None
    if source == "foxess":
        section = raw.get("foxess") or {}
        api_key = str(section.get("api_key") or "").strip()
        if not api_key:
            raise SettingsError(
                "[foxess] api_key is required (generate one at foxesscloud.com, User Profile > API Management)"
            )
        foxess = FoxessSettings(
            api_key=api_key,
            device_sn=str(section.get("device_sn") or "").strip(),
            production_variable=str(section.get("production_variable") or "PVEnergyTotal"),
            consumption_variable=str(section.get("consumption_variable") or "loads"),
            min_interval_s=_float(section, "min_interval_s", "foxess") or 2.0,
        )
        # The model's check-up and learning key on a named production source; any non-empty name does.
        entry[CONF_PRODUCTION_ENTITY] = f"foxess:{foxess.production_variable}"
    elif source == "csv":
        section = raw.get("csv") or {}
        path = str(section.get("production") or "")
        if not path:
            raise SettingsError("[csv] production is required: the path to the hourly production file")
        consumption = str(section.get("consumption") or "")
        csv = CsvSettings(
            production_path=str((base_dir / path).resolve()),
            consumption_path=str((base_dir / consumption).resolve()) if consumption else "",
        )
        entry[CONF_PRODUCTION_ENTITY] = f"csv:{Path(path).name}"

    battery = raw.get("battery") or {}
    capacity = _float(battery, "capacity_kwh", "battery")
    battery_enabled = capacity is not None and capacity > 0
    if battery_enabled:
        if source != "foxess":
            raise SettingsError(
                "[battery] needs a live state of charge and a consumption history, which only the foxess "
                "source provides; remove capacity_kwh to run without the battery projection"
            )
        entry[CONF_BATTERY_CAPACITY_KWH] = capacity
        entry[CONF_BATTERY_SOC_ENTITY] = f"{source}:SoC"
        for key, conf in (
            ("max_charge_kw", CONF_BATTERY_MAX_CHARGE_KW),
            ("max_discharge_kw", CONF_BATTERY_MAX_DISCHARGE_KW),
            ("min_soc", CONF_BATTERY_MIN_SOC),
            ("efficiency", CONF_BATTERY_EFFICIENCY),
        ):
            value = _float(battery, key, "battery")
            if value is not None:
                entry[conf] = value

    web = raw.get("web") or {}
    port = _float(web, "port", "web")
    refresh = _float(raw.get("schedule") or {}, "refresh_minutes", "schedule")
    if refresh is not None and not 5 <= refresh <= 180:
        raise SettingsError("[schedule] refresh_minutes must be between 5 and 180")

    data_dir = Path(str(raw.get("data_dir") or "data"))
    if not data_dir.is_absolute():
        data_dir = (base_dir / data_dir).resolve()

    return Settings(
        name=str(site.get("name") or "Helios Forecast"),
        latitude=lat,  # type: ignore[arg-type]
        longitude=lon,  # type: ignore[arg-type]
        tz=tz,
        entry=entry,
        source=source,
        data_dir=data_dir,
        host=str(web.get("host") or "0.0.0.0"),
        port=int(port) if port is not None else 8099,
        refresh_minutes=int(refresh) if refresh is not None else 30,
        foxess=foxess,
        csv=csv,
        battery_enabled=battery_enabled,
        raw=raw,
    )


def load_settings(path: str | Path) -> Settings:
    path = Path(path)
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise SettingsError(f"configuration file {path} not found (copy config.example.toml to start)") from None
    except tomllib.TOMLDecodeError as err:
        raise SettingsError(f"{path} is not valid TOML: {err}") from None
    return parse_settings(raw, path.resolve().parent)
