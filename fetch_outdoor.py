#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
"""Update ``data/outdoor_hourly.csv`` with measured hourly weather from the
MeteoSwiss SwissMetNet station **Pully (PUY)** — lakeside, ~6 km from Malley.

Replaces the open-meteo model data the dashboard used to fetch at build time:
that stitched two different models (ERA5 archive for days older than 92, the
forecast model after), whose nights disagree by ~3 °C, at a seam that moved
every day. One measured source, committed to the repo, keeps every build
consistent and offline.

MeteoSwiss Open Government Data (no key needed):
    https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/puy/
    ogd-smn_puy_h_recent.csv   1 Jan of the current year → yesterday
    ogd-smn_puy_h_now.csv      today, updated hourly

Source timestamps are UTC and mark the END of each hourly interval. They are
stored here as ``hour_start_utc`` (the start of the interval), so a row stamped
``2026-06-21 11:00`` is the mean over 11:00–12:00 UTC.

Idempotent: re-running only adds new hours (and refreshes any revised ones).

Usage:
    uv run fetch_outdoor.py
"""
import csv
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "data", "outdoor_hourly.csv")
BASE = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/puy/ogd-smn_puy_h_{}.csv"

# Keep a few weeks before the indoor START_DATE so the 48-h running-mean outdoor
# temperature (Minergie limits) and the 3-day memory fit are warmed up on day one.
KEEP_FROM = "2026-04-01 00:00"

# MeteoSwiss parameter → our column
PARAMS = {
    "tre200h0": "temp",      # air temperature 2 m, hourly mean (°C)
    "gre000h0": "rad",       # global radiation, hourly mean (W/m²)
    "tde200h0": "dewpoint",  # dew point 2 m, hourly mean (°C)
    "ure200h0": "rh",        # relative humidity 2 m, hourly mean (%)
}
HEADER = ["hour_start_utc", *PARAMS.values()]
FMT = "%Y-%m-%d %H:%M"


def load(path: str) -> dict[str, list[str]]:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return {r[0]: r[1:] for r in list(csv.reader(f))[1:] if r}
    except FileNotFoundError:
        return {}


def download(which: str) -> dict[str, list[str]]:
    r = requests.get(BASE.format(which), timeout=60)
    r.raise_for_status()
    rows = list(csv.DictReader(r.content.decode("latin-1").splitlines(), delimiter=";"))
    out = {}
    for row in rows:
        end = datetime.strptime(row["reference_timestamp"], "%d.%m.%Y %H:%M")
        stamp = (end - timedelta(hours=1)).strftime(FMT)
        if stamp >= KEEP_FROM:
            out[stamp] = [row.get(p, "").strip() for p in PARAMS]
    return out


def main() -> None:
    rows = load(CSV_PATH)
    # "recent" (~800 kB) is only needed when we're missing more than today —
    # a regular poll just tops up from the small "now" file.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00")
    last = max(rows) if rows else ""
    sources = ["now"] if last >= today else ["recent", "now"]
    try:
        for which in sources:  # later sources overwrite earlier ones on overlap
            rows.update(download(which))
    except (requests.RequestException, KeyError, ValueError) as e:
        sys.exit(f"MeteoSwiss download failed ({e}) — keeping the existing file.")

    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADER)
        for stamp in sorted(rows):
            w.writerow([stamp, *rows[stamp]])
    print(f"outdoor_hourly.csv: {len(rows)} hours, {min(rows)} → {max(rows)} UTC "
          f"(fetched {' + '.join(sources)})")


if __name__ == "__main__":
    main()
