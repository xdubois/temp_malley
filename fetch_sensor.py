#!/usr/bin/env python3
"""Poll the SwitchBot Hub 2 once over the Cloud API and append one reading to
``data/sensor_auto.csv`` — in the exact European format the app exports and
``build_dashboard.py`` reads. Replaces the tedious manual "Export Data → CSV".

Kept separate from the manually exported ``sensor_15min.csv`` so a fresh manual
dump never clobbers polled rows; ``build_dashboard.py`` merges both on read.

The SwitchBot Cloud API only ever returns the *latest* reading (it can't bulk
export history), so this is meant to run on a schedule and accumulate one row
per poll. For the pre-existing backlog, the app's manual CSV export is still the
only route — run this going forward.

Credentials come from environment variables (never commit them). Get the token
and secret in the app: Profile → Preferences → About → tap to open Developer
Options.

    SWITCHBOT_TOKEN    Open Token
    SWITCHBOT_SECRET   Secret Key
    SWITCHBOT_DEVICE   deviceId of the Hub 2 (discover it with --list)

Usage:
    uv run fetch_sensor.py --list      # print your devices + ids, then exit
    uv run fetch_sensor.py             # poll once, append a row (idempotent per slot)
"""
import base64
import csv
import hashlib
import hmac
import math
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

API = "https://api.switch-bot.com"
TZ = ZoneInfo("Europe/Zurich")
HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "data", "sensor_auto.csv")    # append-only auto-poll log
PHASE_REF = os.path.join(HERE, "data", "sensor_15min.csv")  # align to the manual export's grid
HEADER = ["Date", "Temperature_Celsius(℃)", "Relative_Humidity(%)",
          "DPT(℃)", "VPD(kPa)", "Abs Humidity(g/m³)", "Light_Value"]


# --------------------------------------------------------------------------- #
# SwitchBot Cloud API v1.1
# --------------------------------------------------------------------------- #
def _auth_headers(token: str, secret: str, upper: bool) -> dict:
    """v1.1 signature: base64(HMAC-SHA256(token + t + nonce, secret)).

    The official docs disagree on whether to uppercase the base64 — the Python
    sample doesn't, the written procedure and JS/Go/C#/PHP samples do — so the
    caller tries one casing and falls back to the other on an auth failure.
    """
    t = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    mac = hmac.new(secret.encode(), (token + t + nonce).encode(), hashlib.sha256).digest()
    sign = base64.b64encode(mac).decode()
    return {"Authorization": token, "sign": sign.upper() if upper else sign,
            "t": t, "nonce": nonce, "Content-Type": "application/json; charset=utf8"}


def _get(path: str, token: str, secret: str) -> dict:
    for upper in (True, False):  # try uppercased sign first, fall back on auth failure
        r = requests.get(API + path, headers=_auth_headers(token, secret, upper), timeout=30)
        if r.status_code == 401:
            continue
        r.raise_for_status()
        j = r.json()
        if j.get("statusCode") in (401, 190):
            continue
        if j.get("statusCode") != 100:
            sys.exit(f"API error {j.get('statusCode')}: {j.get('message')}")
        return j["body"]
    sys.exit("Authentication failed — check SWITCHBOT_TOKEN / SWITCHBOT_SECRET.")


# --------------------------------------------------------------------------- #
# Derived columns (the app computes these from temperature + humidity)
# --------------------------------------------------------------------------- #
def dew_point(t: float, rh: float) -> float:
    a, b = 17.62, 243.12  # Magnus coefficients
    g = math.log(max(rh, 1e-6) / 100) + a * t / (b + t)
    return b * g / (a - g)


def vpd(t: float, rh: float) -> float:
    es = 0.6108 * math.exp(17.27 * t / (t + 237.3))  # saturation vapour pressure, kPa
    return es * (1 - rh / 100)


def abs_humidity(t: float, rh: float) -> float:
    return 216.7 * (rh / 100 * 6.112 * math.exp(17.62 * t / (243.12 + t))) / (273.15 + t)


def eu(x: float, nd: int) -> str:
    """European number format: fixed decimals, comma decimal separator."""
    return f"{x:.{nd}f}".replace(".", ",")


# --------------------------------------------------------------------------- #
# 15-min grid alignment
# --------------------------------------------------------------------------- #
def grid_phase(path: str) -> int:
    """Minute-of-hour offset (0-14) the existing rows sit on.

    build_dashboard.py reindexes onto a 15-min grid anchored at the first
    reading, so appended rows must land on that same phase or they get dropped.
    Derived from the last row; defaults to 0 for an empty/new file.
    """
    try:
        with open(path, encoding="utf-8") as f:
            rows = [r for r in f.read().splitlines() if r]
        last = rows[-1].split(",")[0]
        return datetime.strptime(last, "%d/%m/%Y %H:%M").minute % 15
    except (FileNotFoundError, IndexError, ValueError):
        return 0


def snap(now: datetime, phase: int) -> datetime:
    """Floor ``now`` to the most recent 15-min slot on ``phase``.

    Flooring (rather than rounding to nearest) never stamps a reading in the
    future, and attributes a late scheduled run to its intended slot — e.g. the
    :07 cron firing late at :16 still lands on :07, not :22.
    """
    now = now.replace(second=0, microsecond=0, tzinfo=None)
    midnight = now.replace(hour=0, minute=0)
    mins = now.hour * 60 + now.minute
    k = math.floor((mins - phase) / 15)
    return midnight + timedelta(minutes=phase + k * 15)


def last_stamp(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            rows = [r for r in f.read().splitlines() if r]
        return rows[-1].split(",")[0] if len(rows) > 1 else None
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------------- #
def main() -> None:
    token = os.environ.get("SWITCHBOT_TOKEN")
    secret = os.environ.get("SWITCHBOT_SECRET")
    if not token or not secret:
        sys.exit("Set SWITCHBOT_TOKEN and SWITCHBOT_SECRET (see the module docstring).")

    if "--list" in sys.argv:
        body = _get("/v1.1/devices", token, secret)
        for d in body.get("deviceList", []):
            print(f"{d.get('deviceId')}  {str(d.get('deviceType')):<16} {d.get('deviceName')}")
        return

    device = os.environ.get("SWITCHBOT_DEVICE")
    if not device:
        sys.exit("Set SWITCHBOT_DEVICE to your Hub 2 deviceId (find it with --list).")

    s = _get(f"/v1.1/devices/{device}/status", token, secret)
    t = float(s["temperature"])
    rh = float(s["humidity"])
    light = s.get("lightLevel", "")  # Hub 2 reports lightLevel (1-20); Meters don't

    stamp = snap(datetime.now(TZ), grid_phase(PHASE_REF)).strftime("%d/%m/%Y %H:%M")
    if stamp == last_stamp(CSV_PATH):
        print(f"slot {stamp} already recorded — skipping")
        return

    row = [stamp, eu(t, 1), str(round(rh)), eu(dew_point(t, rh), 1),
           eu(vpd(t, rh), 2), eu(abs_humidity(t, rh), 2), str(light)]

    new_file = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)  # QUOTE_MINIMAL → quotes only the comma-decimal fields, like the app
        if new_file:
            w.writerow(HEADER)
        w.writerow(row)
    print(f"appended {stamp}: {t} °C, {round(rh)} %RH, light {light}")


if __name__ == "__main__":
    main()
