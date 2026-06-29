# Malley apartment — heat & comfort dashboard

An interactive dashboard of the indoor climate of a **Minergie‑P‑Eco apartment** in
Malley (CH), built from a temperature sensor's 15‑minute export and overlaid
with outdoor weather. It was made to understand how the building handles summer heat —
how much outdoor heat reaches inside, how the flat cools (or doesn't) at night, and how
it compares to the SIA 180 / Minergie summer‑comfort expectations.

**Live dashboard:** https://xdubois.github.io/temp_malley/ (rebuilt automatically on every push.)

## What it produces

`uv run build_dashboard.py` generates a single self‑contained HTML file,
`temperature_dashboard.html` (Plotly inlined — opens offline in any browser), with:

1. **Indoor vs outdoor temperature** — every 15‑min reading vs hourly outdoor, zoomable
2. **Seasonal warming trend** — daily min/mean/max
3. **Day × hour heatmap** — when in the day the flat heats up
4. **Daily day↔night swing** — indoor vs outdoor amplitude (the building's damping)
5. **Sun & light vs temperature** — solar gain
6. **Comfort vs thresholds** — against 26.5 °C (Minergie) and 28 °C
7. **Hours above 26.5 °C** — cumulative, vs the ~100 h/year Minergie design budget

## Requirements

- [uv](https://docs.astral.sh/uv/) (manages the Python env and dependencies)

## Usage

```bash
uv run build_dashboard.py        # build the dashboard + rendered analysis
```

First run fetches outdoor weather from [open-meteo](https://open-meteo.com/) and
caches it locally (`.outdoor_cache.json`); later runs are offline.

### Configuration (top of `build_dashboard.py`)

| Setting | Default | Meaning |
|---|---|---|
| `START_DATE` | `2026-04-28` | Ignore readings before this date (move‑in). Override per‑run: `uv run build_dashboard.py --from=2026-05-01` |
| `APPLY_OFFSET` | `False` | If `True`, add `SENSOR_OFFSET` to every reading to estimate the living‑space temperature. `False` shows the raw entrance‑sensor data (the version to share externally). |
| `SENSOR_OFFSET` | `0.8` | The entrance sensor reads ~0.8 °C cooler than the rest of the flat. |
| `LAT`, `LON` | `46.53, 6.59` | Location for the outdoor weather (Malley). |

## Live data (automatic)

`fetch_sensor.py` polls the Hub 2 via the [SwitchBot Cloud API](https://github.com/OpenWonderLabs/SwitchBotAPI) and appends a row
to `data/sensor_auto.csv` — an append-only log kept separate from the manually
exported `sensor_15min.csv`, so re-dumping the manual export never clobbers
polled rows (`build_dashboard.py` merges both on read). `poll-sensor.yml` runs it
every 15 min and commits the reading, which rebuilds the dashboard. The API only
returns the *current* reading, so it accumulates going forward — the app's manual
export remains the only way to backfill older history.

Setup: in the SwitchBot app get a **token** and **key** (Profile → Preferences →
About → tap to open Developer Options); find the Hub 2 id with `uv run --env-file
.env fetch_sensor.py --list`; then add `SWITCHBOT_TOKEN`, `SWITCHBOT_SECRET`, and
`SWITCHBOT_DEVICE` as repo secrets (Settings → Secrets and variables → Actions).

## Project structure

```
.
├── build_dashboard.py     # parse CSV → fetch weather → aggregate → render HTML
├── fetch_sensor.py        # poll the Hub 2 (Cloud API) → append one row to the CSV
├── data/
│   ├── sensor_15min.csv   # manual app exports (overwrite anytime)
│   ├── sensor_auto.csv    # append-only API poll log (merged with the above on read)
│   └── sensor_1min.csv    # 1‑minute export (higher resolution, not yet used)
├── .github/workflows/
│   ├── poll-sensor.yml    # every 15 min: fetch_sensor.py → commit the reading
│   └── pages.yml          # build + deploy the dashboard (on push / after a poll)
├── pyproject.toml / uv.lock
└── README.md
```

Generated files (`temperature_dashboard.html`, `.outdoor_cache.json`) are git‑ignored —
rebuild them with the command above.

## Data

- **Indoor:** a temperature/humidity sensor exported at 15‑min and 1‑min intervals
  (columns: temperature, relative humidity, dew point, VPD, absolute humidity, light;
  European number format with comma decimals). Recent readings are polled live
  from the Hub 2 via the [SwitchBot Open API](https://github.com/OpenWonderLabs/SwitchBotAPI).
- **Outdoor:** open‑meteo ERA5 archive + forecast, hourly temperature and solar
  radiation for the building's coordinates.
