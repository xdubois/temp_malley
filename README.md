# Malley apartment — heat & comfort dashboard

An interactive dashboard of the indoor climate of a **Minergie‑P‑Eco apartment** in
Malley (CH), built from a temperature sensor's 15‑minute export and live polls, overlaid
with measured outdoor weather from the MeteoSwiss station in Pully. It was made to
understand how the building handles summer heat — how much outdoor heat reaches inside,
how the flat cools (or doesn't) at night, and how it measures up to Minergie's
summer‑comfort requirements (SIA 180:2014 Fig. 3 and Fig. 4).

**Live dashboard:** https://xdubois.github.io/temp_malley/ (rebuilt automatically on every push.)

## What it produces

`uv run build_dashboard.py` generates a single self‑contained HTML file,
`temperature_dashboard.html` (Plotly inlined — opens offline in any browser), with:

Headline cards (latest reading, hours above the Fig. 4 comfort limit and the SIA 180 Fig. 3
limit, warm nights, tipping point, weather memory, night cooling used, damping), then:

1. **Indoor vs outdoor temperature** — every indoor reading vs hourly outdoor, zoomable
2. **Last 7 days** — the same, zoomed on the live polls
3. **Daily temperature vs the SIA 180 limits** — daily min/mean/max against the Fig. 3
   (the norm's limit) and Fig. 4 (comfort) curves
4. **Legally fine ≠ livable** — a table of the same measured hours against each yardstick
   (Vaud law, SIA 180, SIA 382/1, Minergie, plus warm nights / peak for contrast), every hour
   of April–October sorted into *comfortable / too warm yet accepted by SIA 180 / above the
   SIA 180 limit*, and the running total above Fig. 4 vs Minergie's 100 h and SIA 382/1's 400 h
5. **What drives the indoor temperature** — indoor daily mean vs the outdoor mean of the
   last few days, with the fitted response and the outdoor "tipping point" where it
   crosses 26.5 °C (Fig. 4's summer plateau)
6. **Night cooling** — cooling offered by the night air vs the drop achieved, coloured
   by the overnight humidity drop (a tracer of outdoor air getting in)
7. **Daily rhythm** — day × hour heatmap of the deviation from each day's mean

## Requirements

- [uv](https://docs.astral.sh/uv/) (manages the Python env and dependencies)

## Usage

```bash
uv run build_dashboard.py        # build the dashboard + rendered analysis
```

The build is fully offline — it reads everything from `data/`. To refresh the outdoor
weather locally (the poll workflow does it automatically), run:

```bash
uv run fetch_outdoor.py          # top up data/outdoor_hourly.csv from MeteoSwiss
```

### Configuration (top of `build_dashboard.py`)

| Setting | Default | Meaning |
|---|---|---|
| `START_DATE` | `2026-04-28` | Ignore readings before this date (move‑in). Override per‑run: `uv run build_dashboard.py --from=2026-05-01` |
| `APPLY_OFFSET` | `False` | If `True`, add `SENSOR_OFFSET` to every reading to estimate the living‑space temperature. `False` shows the raw entrance‑sensor data (the version to share externally). |
| `SENSOR_OFFSET` | `0.8` | The entrance sensor reads ~0.8 °C cooler than the rest of the flat. |
| `FIG3_UPPER`, `FIG4_UPPER` | see file | SIA 180 Fig. 3 / Fig. 4 upper limits as (θrm, °C) breakpoints, linear between and flat outside. |
| `FIG3_SEASON`, `FIG4_SEASON` | `04‑15…10‑15`, `04‑01…10‑31` | Periods in which each limit is assessed. |
| `MINERGIE_MAX_H` | `100` | Hours per year Fig. 4 may be exceeded under Minergie (Fig. 3: never). |
| `SIA_MAX_H` | `400` | Same under SIA 382/1 for homes with mechanical ventilation. |
| `RM_HOURS` | `48` | θrm = mean outdoor air temperature over this many preceding hours. |
| `MEMORY_TAUS_D` | `1…10` | Candidate time constants (days) for the weather‑memory fit; the best one is used. |

## Live data (automatic)

`fetch_sensor.py` polls the Hub 2 via the [SwitchBot Cloud API](https://github.com/OpenWonderLabs/SwitchBotAPI) and appends a row
to `data/sensor_auto.csv` — an append-only log kept separate from the manually
exported `sensor_15min.csv`, so re-dumping the manual export never clobbers
polled rows (`build_dashboard.py` merges both on read). `poll-sensor.yml` runs it
(together with `fetch_outdoor.py`, see below) at :07 and :37 (grid slots, so readings land on the dump's cadence — GitHub fires
cron late and skips runs, so two slots/hour ≈ hourly in practice) and commits
the new data, which rebuilds the dashboard. The API only returns the *current*
reading, so it accumulates going forward — the app's manual export remains the
only way to backfill older history.

Setup: in the SwitchBot app get a **token** and **key** (Profile → Preferences →
About → tap to open Developer Options); find the Hub 2 id with `uv run --env-file
.env fetch_sensor.py --list`; then add `SWITCHBOT_TOKEN`, `SWITCHBOT_SECRET`, and
`SWITCHBOT_DEVICE` as repo secrets (Settings → Secrets and variables → Actions).

## Project structure

```
.
├── build_dashboard.py     # read data/ → aggregate → render HTML (offline)
├── fetch_sensor.py        # poll the Hub 2 (Cloud API) → append one row to the CSV
├── fetch_outdoor.py       # MeteoSwiss Pully hourly data → data/outdoor_hourly.csv
├── data/
│   ├── sensor_15min.csv   # manual app exports (overwrite anytime)
│   ├── sensor_auto.csv    # append-only API poll log (merged with the above on read)
│   ├── sensor_1min.csv    # 1‑minute export (higher resolution, not yet used)
│   └── outdoor_hourly.csv # measured outdoor weather (Pully), refreshed by the poll
├── .github/workflows/
│   ├── poll-sensor.yml    # at :07/:37: fetch_sensor.py + fetch_outdoor.py → commit
│   └── pages.yml          # build + deploy the dashboard (on push / after a poll)
├── pyproject.toml / uv.lock
└── README.md
```

The generated `temperature_dashboard.html` is git‑ignored — rebuild it with the command
above.

## Comfort criterion (Minergie)

Minergie's summer‑comfort requirement ([Anwendungshilfe Gebäudestandards Minergie 2025](https://www.minergie.ch/media/250701_anwendungshilfe_gebaeudestandards_minergie_2025-2_de.pdf),
§6) is built on two limit curves from SIA 180:2014. Both rise with θrm, the mean outdoor air
temperature over the preceding 48 h:

| Curve | Limit | Allowed | Assessed |
|---|---|---|---|
| **Fig. 3** — comfort field | 25 °C up to θrm ≈ 10 °C, rising to 30 °C at θrm = 25 °C | never exceeded | mid‑April → mid‑October |
| **Fig. 4** — cooling need | 24.5 °C up to θrm = 12 °C, rising to 26.5 °C at θrm = 17.5 °C | ≤ 100 h/year (else cooling is required) | April → October |

The popular "100 h above 26.5 °C" rule is Fig. 4's summer plateau.

### What the law actually checks

In Vaud, the energy regulation ([RLVLEne](https://www.vd.ch/fileadmin/user_upload/organisation/dinf/sipal/fichiers_pdf/reglement_d_application_LEnE.pdf)
art. 19c) only requires that summer protection be *justified* per SIA 180 / 382/1. For rooms
without cooling, the one thing it regulates is the **g‑value of the sun protection** — a
design‑stage check (Valais's [EN‑VS‑102](https://www.vs.ch/documents/16739272/35472472/Aide+EN-VS-102_d%C3%A9f.pdf)
guide spells it out: without cooling, the requirements "are considered met if an external sun
protection is installed"). No indoor temperature is ever measured. SIA 180's own temperature
yardstick, used when the justification is a simulation, is Fig. 3 — which climbs to 30 °C
after hot spells. So a flat can be perfectly conformant and still unlivable; section 4 of the
dashboard shows how many hours fall in that gap. SIA itself allows 400 h
above Fig. 4 for homes with mechanical ventilation; Minergie tightens that to 100 h for every
building. Minergie checks both curves in a design simulation of the most exposed room with
2035 weather — the dashboard applies the same curves to what was actually measured, for the
entrance sensor and the living‑room estimate.

## Data

- **Indoor:** a temperature/humidity sensor exported at 15‑min and 1‑min intervals
  (columns: temperature, relative humidity, dew point, VPD, absolute humidity, light;
  European number format with comma decimals). Recent readings are polled live
  from the Hub 2 via the [SwitchBot Open API](https://github.com/OpenWonderLabs/SwitchBotAPI).
- **Outdoor:** hourly **measurements** from the MeteoSwiss SwissMetNet station
  [Pully (PUY)](https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/puy/) — lakeside,
  ~6 km from Malley — via MeteoSwiss open government data (no key needed): air
  temperature, global radiation, dew point and relative humidity. Stored in UTC,
  stamped at the start of each hourly mean. This replaced open‑meteo model data,
  whose ERA5 archive (used for days older than 92) ran ~1.6 °C colder at night than
  the station and than the forecast model used for recent days.
