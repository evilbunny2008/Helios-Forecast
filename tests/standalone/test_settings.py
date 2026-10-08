from __future__ import annotations

from pathlib import Path

import pytest

from conftest import base_config
from custom_components.helios_forecast.config import layout_from_config, inverter_max_w_from_config
from helios_standalone.settings import SettingsError, load_settings, parse_settings


def test_resolves_into_the_entry_dict_the_model_reads(tmp_path: Path):
    s = parse_settings(
        base_config(arrays=[{"tilt": 20, "azimuth": 0, "kwp": 4}, {"tilt": 20, "azimuth": 270, "kwp": 2}]), tmp_path
    )
    layout = layout_from_config(s.entry)
    assert layout.total_kwp == 6
    assert [round(x, 3) for x in layout.shares] == [0.667, 0.333]
    assert inverter_max_w_from_config(s.entry) == 5000
    assert s.tz.key == "Australia/Sydney"
    assert s.data_dir == (tmp_path / "data").resolve()
    assert s.source == "none" and not s.battery_enabled


@pytest.mark.parametrize(
    "change, message",
    [
        ({"site": {"latitude": 1, "longitude": 2}}, "timezone"),
        ({"site": {"latitude": 1, "longitude": 2, "timezone": "Mars/Olympus"}}, "not a known"),
        ({"arrays": []}, "arrays"),
        ({"arrays": [{"tilt": 20, "azimuth": 0}]}, "kwp"),
        ({"source": {"type": "solcast"}}, "type must be"),
        ({"source": {"type": "foxess"}}, "api_key"),
        ({"source": {"type": "csv"}}, "production"),
        ({"battery": {"capacity_kwh": 10}}, "foxess"),
        ({"schedule": {"refresh_minutes": 1}}, "refresh_minutes"),
    ],
)
def test_rejects_what_cannot_run(tmp_path: Path, change, message):
    with pytest.raises(SettingsError, match=message):
        parse_settings(base_config(**change), tmp_path)


def test_foxess_with_battery_names_the_sources(tmp_path: Path):
    s = parse_settings(
        base_config(
            source={"type": "foxess"}, foxess={"api_key": "k" * 36}, battery={"capacity_kwh": 10.4, "min_soc": 15}
        ),
        tmp_path,
    )
    assert s.battery_enabled
    assert s.entry["production_entity"] == "foxess:PVEnergyTotal"
    assert s.entry["battery_soc_entity"] == "foxess:SoC"
    assert s.entry["battery_min_soc"] == 15


def test_the_example_configuration_loads_once_a_key_is_set(tmp_path: Path):
    example = (Path(__file__).resolve().parents[2] / "config.example.toml").read_text()
    with pytest.raises(SettingsError, match="api_key"):
        (tmp_path / "a.toml").write_text(example)
        load_settings(tmp_path / "a.toml")
    (tmp_path / "b.toml").write_text(example.replace('api_key = ""', 'api_key = "' + "k" * 36 + '"'))
    s = load_settings(tmp_path / "b.toml")
    assert s.source == "foxess" and s.port == 8099 and len(s.entry["arrays"]) == 1


def test_missing_file_says_how_to_start(tmp_path: Path):
    with pytest.raises(SettingsError, match="config.example.toml"):
        load_settings(tmp_path / "nope.toml")
