# Helios Forecast without Home Assistant

`helios_standalone/` runs the integration's own forecast model on its own: it learns from your
inverter's production history read straight from the inverter's cloud API, refreshes every 30
minutes, and serves a web UI and a JSON API. It imports the model from
`custom_components/helios_forecast`, so any fix to the integration applies here too, and the
numbers match what the integration would publish for the same site and history.

## What you need

- Python 3.13 (the version CI tests), with `aiohttp` (`pip install -r helios_standalone/requirements.txt`)
- For learning: a FoxESS Cloud API key (foxesscloud.com, User Profile > API Management), or
  hourly production in CSV files. Without either, you get the physical model alone.

## Run it

```sh
git clone https://github.com/ReikanYsora/Helios-Forecast && cd Helios-Forecast
python3 -m venv .venv && .venv/bin/pip install -r helios_standalone/requirements.txt
cp config.example.toml config.toml        # then edit: location, panels, API key
.venv/bin/python -m helios_standalone check -c config.toml   # proves the key and finds the inverter
.venv/bin/python -m helios_standalone -c config.toml         # serves http://localhost:8099/
```

| Command | What it does |
|---|---|
| `serve` (the default) | refreshes on a timer and serves the web UI and API |
| `once` | runs one refresh, writes the output files and prints the daily totals |
| `check` | prints the resolved panel layout and, for FoxESS, makes one call to prove the key |

On the first start with FoxESS, about 120 calls fetch the 60-day learning window (hourly
production, plus the state of charge when a battery is configured), two seconds apart, which takes
about four minutes. Finished days are cached under `data/foxess/` and never fetched again, so after
that a refresh costs about three calls.

### As a service

```ini
# /etc/systemd/system/helios-forecast.service
[Unit]
Description=Helios Forecast
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/Helios-Forecast
ExecStart=/opt/Helios-Forecast/.venv/bin/python -m helios_standalone -c config.toml
Restart=on-failure
User=helios

[Install]
WantedBy=multi-user.target
```

## The web UI

- Today's forecast, power now with its likely range, the rest of today, tomorrow, the reliability
  index, and how today's outlook has moved since the morning
- Power over yesterday, today, tomorrow or a week: the forecast, its P10 to P90 range, the model
  before learning, and the measured hourly production
- The battery's projected state of charge for the next 48 hours, when a battery is configured
- The next 7 days, and every past day of the learning window, forecast against measured
- The check-up: the same configuration and data problems the integration raises as repairs
- A table view for every chart, light and dark themes

## API and output files

| Endpoint | Content |
|---|---|
| `GET /api/summary` | headline figures, the 7 days, reliability, trend, battery, weather, problems, source status |
| `GET /api/series?day=YYYY-MM-DD&days=N` | the power curve for N local days (or `start`/`end` with a UTC offset), measured hours, battery projection |
| `GET /api/daily` | forecast against measured per day over the learning window |
| `GET /api/solcast.json` | the forecast from now on in Solcast's rooftop `forecasts` shape (30-minute periods, kW) |
| `POST /api/refresh` | refreshes now and returns the summary |
| `GET /health` | 200 once a forecast exists, 503 before |

The series follows the integration's `helios_forecast/series` websocket command (see
[CONTRACT.md](CONTRACT.md)). After every refresh the same documents are written to
`data/out/summary.json`, `series.json` and `solcast.json`, for tools that read files.

### Using it from a Solcast consumer

`/api/solcast.json` and `data/out/solcast.json` have the shape of Solcast's
`rooftop_sites/{id}/forecasts` answer: `period_end` in UTC, `period` `PT30M`, `pv_estimate` (and
`pv_estimate10` / `pv_estimate90` from the uncertainty band) as average kW over the period. A tool
that reads Solcast, FreePowerMaximiser among them, can read this instead of a Solcast site, with
no API key and no daily call limit.

## Configuration

`config.example.toml` documents every key. The parts that matter most:

- **`[[arrays]]`**: one section per panel line. Azimuth is degrees from north (0 = north, 90 =
  east, 180 = south, 270 = west), tilt is degrees from horizontal.
- **`[foxess] production_variable`**: `PVEnergyTotal` (the default) is the panels' DC yield.
  `generation` is the inverter's AC output, which on a hybrid includes battery discharge; use it
  only on an inverter without a battery.
- **`[battery] capacity_kwh`**: turns on the state-of-charge projection and the curtailment
  detection (hours when a full battery held the inverter back are not learned from). It reads the
  live state of charge and the house load (`loads`) from FoxESS.
- **`[foxess] production_variable`** can also name a power reading, such as `meterPower2` for a CT
  clamp: anything that is not one of the report's energy variables is read from the 5-minute samples
  and integrated into hourly kWh. `production_invert = true` flips a clamp that reads production as
  negative.
- **`[csv]`**: hourly files of `start,kwh` rows, `start` an ISO time with its UTC offset, for an
  inverter FoxESS does not cover.

## AC-coupled arrays

An array with its own inverter, connected on the AC side, is not in `PVEnergyTotal`: that is the
DC strings of the hybrid only. Listing it as another `[[arrays]]` in the same configuration would
make the model compare a forecast of both arrays with a measurement of one, and learn that the roof
underperforms; and the site's `inverter_max_kw` would wrongly cap its output too. Run it as a second
instance instead, with the same location and time zone:

```toml
# config-ac.toml
data_dir = "data-ac"                  # its own cache and state
[site]
inverter_max_kw = 3.0                 # the AC array's own inverter
[[arrays]]                            # the AC array only
tilt = 20
azimuth = 270
kwp = 3.3
[source]
type = "foxess"
[foxess]
api_key = "..."                       # the same key
production_variable = "meterPower2"   # the CT clamp on the AC array's output
[web]
port = 8100
```

Keep the `[battery]` section in the hybrid's configuration only. Run
`python -m helios_standalone check -c config-ac.toml` first: it prints today's production from the
clamp and says when it reads negative, which `production_invert = true` fixes. The two instances then
give two forecasts, `localhost:8099/api/solcast.json` and `localhost:8100/api/solcast.json`, which is
the shape FreePowerMaximiser's `siteid1` and `siteid2` already expect.

## How it maps to the integration

| Integration | Standalone |
|---|---|
| Config flow, options | `config.toml`, translated into the same entry dict (`settings.py`) |
| Recorder statistics (production, state of charge, Energy dashboard consumption) | a history source (`sources.py`): FoxESS Open API with a day cache, or CSV |
| `DataUpdateCoordinator` refresh | `engine.py`, the same steps and the same pure functions |
| `helpers.storage.Store` (trend reference, day-ahead record) | JSON files under `data/state/` |
| Sensors, Energy provider, websocket, repairs | the web UI, the JSON API and the output files |

The opt-in community benchmark is not part of the standalone runner: it identifies an
installation by its Home Assistant config entry.
