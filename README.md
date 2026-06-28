# Malley apartment — heat & comfort dashboard

An interactive dashboard of the indoor climate of a **Minergie‑P‑Eco apartment** in
Malley (CH), built from a temperature sensor's 15‑minute export and overlaid
with outdoor weather. It was made to understand how the building handles summer heat —
how much outdoor heat reaches inside, how the flat cools (or doesn't) at night, and how
it compares to the SIA 180 / Minergie summer‑comfort expectations.

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

## Project structure

```
.
├── build_dashboard.py     # parse CSV → fetch weather → aggregate → render HTML
├── data/
│   ├── sensor_15min.csv   # 15‑minute indoor sensor export (primary)
│   └── sensor_1min.csv    # 1‑minute export (higher resolution, not yet used)
├── pyproject.toml / uv.lock
└── README.md
```

Generated files (`temperature_dashboard.html`, `.outdoor_cache.json`) are git‑ignored —
rebuild them with the command above.

## Data

- **Indoor:** a temperature/humidity sensor exported at 15‑min and 1‑min intervals
  (columns: temperature, relative humidity, dew point, VPD, absolute humidity, light;
  European number format with comma decimals).
- **Outdoor:** open‑meteo ERA5 archive + forecast, hourly temperature and solar
  radiation for the building's coordinates.
