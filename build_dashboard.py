#!/usr/bin/env python3
"""Build an interactive temperature dashboard for the Malley apartment.

Parses the indoor sensor CSVs (15-min export + live polls), reads the measured
outdoor weather (MeteoSwiss Pully, kept up to date by fetch_outdoor.py),
computes daily aggregates and renders a single self-contained HTML file
(Plotly inlined -> opens offline in any browser).

Run:   uv run build_dashboard.py
No network access needed — everything comes from data/.
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_15MIN = os.path.join(HERE, "data", "sensor_15min.csv")   # manual app exports
CSV_AUTO = os.path.join(HERE, "data", "sensor_auto.csv")     # append-only API poll log
CSV_1MIN = os.path.join(HERE, "data", "sensor_1min.csv")
CSV_OUTDOOR = os.path.join(HERE, "data", "outdoor_hourly.csv")  # MeteoSwiss Pully (fetch_outdoor.py)
OUT_HTML = os.path.join(HERE, "temperature_dashboard.html")

TZ = "Europe/Zurich"
OUTDOOR_STATION = "MeteoSwiss Pully (PUY)"

# Ignore readings before this date (earlier rows are from a different location).
# Override per run with --from=YYYY-MM-DD.
START_DATE = "2026-04-28"

# Drop days with thin coverage from the daily charts. Counted as distinct
# clock-hours that carry a reading — NOT a raw reading count — so the threshold
# is independent of sampling cadence: a full 15-min-export day (~24 h) and a full
# hourly-auto-poll day (~24 h) both qualify, while a barely-started day does not.
# 12 h ≈ the old "48 × 15-min readings = half a day" rule.
MIN_HOURS_DAY = 12

# The sensor sits at the entrance, ~0.8 °C cooler than the rest of the flat.
# When APPLY_OFFSET is True, SENSOR_OFFSET is added to every indoor reading so the
# WHOLE dashboard shows the living-space estimate; when False it shows the raw
# entrance-sensor reading everywhere. A constant offset only shifts absolute
# temperatures — swings, damping, correlation and lag are unaffected.
APPLY_OFFSET = False
SENSOR_OFFSET = 0.8
TEMP_REF = (f"living-room estimate (sensor +{SENSOR_OFFSET:g} °C)"
            if APPLY_OFFSET else "corridor sensor (measured)")
# The other reference, shown alongside for the comfort numbers.
REF_SHORT, ALT_SHORT = (("living rooms (est.)", "corridor sensor") if APPLY_OFFSET
                        else ("corridor sensor", "living rooms (est.)"))
ALT_SHIFT = -SENSOR_OFFSET if APPLY_OFFSET else SENSOR_OFFSET

# Minergie summer comfort (Anwendungshilfe Gebäudestandards Minergie 2025, §6),
# built on the two limit curves of SIA 180:2014. Both depend on θrm, the mean
# outdoor air temperature over the preceding 48 h, and are given here as
# (θrm, limit °C) breakpoints read off the figures — linear between, flat outside.
#   Fig. 3  comfort field: may never be exceeded (0 h), mid-April → mid-October.
#   Fig. 4  cooling need: cooling is required if exceeded > 100 h/a, April → October.
#           Minergie applies the 100 h to every building (SIA allows 400 h for homes
#           with mechanical ventilation). Its 26.5 °C plateau is the popular
#           "100 h above 26.5 °C" rule.
# Minergie certifies with design simulations (2035 weather, most exposed room); here
# the same curves are applied to measured reality.
FIG3_UPPER = [(9.7, 25.0), (25.0, 30.0)]
FIG4_UPPER = [(12.0, 24.5), (17.5, 26.5)]
FIG3_SEASON = ("04-15", "10-15")   # MM-DD, inclusive
FIG4_SEASON = ("04-01", "10-31")
MINERGIE_MAX_H = 100               # h/a above Fig. 4
RM_HOURS = 48

# The legal side, for contrast. The law itself sets no indoor temperature: Vaud's
# RLVLEne art. 19c only asks that summer protection be justified per SIA 180 / 382/1,
# and for rooms without cooling regulates just the g-value of the sun protection — a
# design-stage check. The norms' own yardsticks are laxer than Minergie's: Fig. 3
# reaches 30 °C after hot spells, and SIA 382/1 tolerates 400 h/a above Fig. 4 for homes
# with mechanical ventilation (as Minergie-P flats have) before cooling is "needed".
SIA_MAX_H = 400

# Nights are only judged when they matter: a warm night when the flat is above its
# comfort limit (Fig. 4) at 22:00 — should it cool? — and a cold night on a heating
# day, outdoor daily mean below 12 °C (the Swiss heating-degree-day convention,
# 20/12) — does it keep its heat? Mild nights in between need nothing.
HEATING_DAY_T = 12.0

# Fig. 4's summer plateau: used for "warm nights" and the weather-memory tipping point.
COMFORT_T = FIG4_UPPER[-1][1]

# Candidate time constants (days) for the "weather memory" fit — the one whose
# exponentially weighted outdoor mean best explains the indoor daily mean wins.
MEMORY_TAUS_D = [1, 2, 3, 4, 5, 7, 10]

# Main façade / glazing orientation (shown on the dashboard).
ORIENTATION = "North-West"

# How the flat was run while it was measured — shown above the cards, so the heat
# reads as what remains despite these habits. Passive, by-hand measures — edit to match.
COOLING_MEASURES = [
    "External blinds down on sunny days",
    "Loggia door open at night to let cool air in",
    "Heat-producing appliances (oven …) kept to a minimum",
]

# When counting hours above a threshold, each reading stands in for the time
# until the next one (0.25 h on the 15-min export, ~1 h on the live polls) —
# capped so an offline gap isn't all credited to the reading before it.
READING_CAP_H = 2.0

# The daily-rhythm heatmap is clipped to ± this many °C around each day's mean.
RHYTHM_RANGE = 1.0

# palette
C_IN = "#e8633a"      # indoor temperature (warm)
C_IN_FILL = "rgba(232,99,58,0.15)"
C_OUT = "#2f7ec4"     # outdoor temperature (cool)
C_FIG4 = "#e0a800"    # SIA 180 Fig. 4 limit (line + bars) — validated against C_IN / C_OUT
C_FIG3 = "#4a3aa7"    # SIA 180 Fig. 3 limit (line + bars) — violet: a red would clash with C_IN
C_OK = "#c9c8c2"      # hours within the Fig. 4 comfort limit — neutral, the zone that's fine
SEQ_BLUE = [[0, "#86b6ef"], [0.33, "#3987e5"], [0.66, "#1c5cab"], [1, "#0d366b"]]
DIVERGING = [[0, "#184f95"], [0.25, "#6da7ec"], [0.5, "#f0efec"],
             [0.75, "#f0957a"], [1, "#b8321a"]]
GRID = "#e6e6e6"
INK = "#2b2b2b"

CONFIG = {"responsive": True, "displaylogo": False,
          "modeBarButtonsToRemove": ["lasso2d", "select2d"]}


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def _read_indoor_csv(path: str) -> pd.DataFrame:
    """Parse one European-formatted sensor CSV ('25,1' comma decimals)."""
    df = pd.read_csv(path, decimal=",")
    df.columns = ["date", "temp", "hum", "dpt", "vpd", "abshum", "light"]
    df["date"] = pd.to_datetime(df["date"], format="%d/%m/%Y %H:%M")
    return df.dropna(subset=["date"]).set_index("date")


def load_indoor(paths: list[str], start: str | None = None) -> pd.DataFrame:
    """Merge the sensor CSVs onto a complete 15-min grid.

    ``paths`` are given in priority order — on a duplicate timestamp the earlier
    file wins (manual export over auto poll). Missing 15-min slots become NaN so
    the plotted line breaks across offline periods instead of jumping over them.
    Rows before ``start`` are dropped.
    """
    frames = [_read_indoor_csv(p) for p in paths if os.path.exists(p)]
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="first")].sort_index()  # first = highest priority
    if start:
        df = df[df.index >= start]
    full = pd.date_range(df.index.min(), df.index.max(), freq="15min")
    off_grid = df.index.difference(full)
    if len(off_grid):  # reindex would silently discard these — e.g. after a phase shift
        print(f"  WARNING: dropping {len(off_grid)} readings off the 15-min grid "
              f"(first: {off_grid[0]}) — check export/poll phase alignment.", file=sys.stderr)
    df = df.reindex(full)
    if APPLY_OFFSET:
        df["temp"] = df["temp"] + SENSOR_OFFSET  # estimate living-space temperature
    return df


def load_outdoor(path: str) -> pd.DataFrame:
    """Hourly measured outdoor weather (MeteoSwiss Pully) on local wall-clock time.

    Stored in UTC, stamped at the start of each hourly mean (see fetch_outdoor.py);
    converted to naive Europe/Zurich time to line up with the indoor readings. The
    hour that repeats when DST ends is averaged.
    """
    if not os.path.exists(path):
        raise SystemExit(f"  {os.path.relpath(path, HERE)} missing — run "
                         "`uv run fetch_outdoor.py` first")
    out = pd.read_csv(path)
    utc = pd.to_datetime(out.pop("hour_start_utc")).dt.tz_localize("UTC")
    out.index = pd.DatetimeIndex(utc.dt.tz_convert(TZ).dt.tz_localize(None), name="time")
    return out.groupby(level=0).mean().sort_index()


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def hours_over(temp: pd.Series, threshold: float | pd.Series) -> pd.Series:
    """Duration-weighted hours above ``threshold``, per reading.

    A flat 0.25 h/reading undercounts the live polls 4-8× (they arrive every
    1-2 h, not every 15 min) — so weight each reading by the gap to the next,
    capped at READING_CAP_H. On the 15-min export this reduces to 0.25 h.
    ``threshold`` may be a per-reading Series (e.g. a Minergie limit curve).
    """
    s = temp.dropna()
    if isinstance(threshold, pd.Series):
        threshold = threshold.reindex(s.index)
    gap = s.index.to_series().diff().shift(-1)
    gap = gap.clip(upper=pd.Timedelta(hours=READING_CAP_H)).fillna(pd.Timedelta("15min"))
    return (s > threshold) * (gap.dt.total_seconds() / 3600)


def per_reading(hourly: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Spread an hourly value onto every reading within that hour."""
    return hourly.reindex(index.floor("h")).set_axis(index)


def in_season(index: pd.DatetimeIndex, season: tuple[str, str]) -> np.ndarray:
    """True for timestamps inside a (MM-DD, MM-DD) window, any year."""
    md = index.strftime("%m-%d")
    return (md >= season[0]) & (md <= season[1])


def minergie_limits(out: pd.DataFrame) -> pd.DataFrame:
    """Hourly SIA 180 Fig. 3 / Fig. 4 upper limits (see FIG3_UPPER / FIG4_UPPER).

    θrm is the mean outdoor air temperature over the RM_HOURS hours *before*
    each hour, hence the shift.
    """
    rm = (out["temp"].rolling(f"{RM_HOURS}h", min_periods=RM_HOURS * 3 // 4)
          .mean().shift(1))
    return pd.DataFrame({"rm": rm,
                         "fig3": np.interp(rm, *zip(*FIG3_UPPER)),
                         "fig4": np.interp(rm, *zip(*FIG4_UPPER))}, index=out.index)


def minergie_hours(temp: pd.Series, limit: pd.Series, season: tuple[str, str]) -> pd.Series:
    """Hours above a Minergie limit curve, counted only inside its season."""
    return hours_over(temp[in_season(temp.index, season)], limit)


def diurnal_swing(temp: pd.Series) -> pd.Series:
    """Daily day↔night swing with multi-day drift removed.

    A plain daily max − min also counts a warm-up or cool-down spanning several
    days (the day's max then sits at 00:xx). Subtracting a centred 24-h running
    mean first leaves only the daily cycle.
    """
    s = temp.dropna()
    resid = s - s.rolling("24h", center=True, min_periods=12).mean()
    g = resid.resample("D")
    return g.max() - g.min()


def daily_indoor(df: pd.DataFrame) -> pd.DataFrame:
    g = df["temp"].resample("D").agg(["min", "max", "mean", "count"])
    # distinct clock-hours covered per day (cadence-independent — see MIN_HOURS_DAY)
    t = df.dropna(subset=["temp"])
    hours = t.groupby([t.index.normalize(), t.index.hour]).size().groupby(level=0).size()
    g["hours"] = hours.reindex(g.index).fillna(0)
    g = g[g["hours"] >= MIN_HOURS_DAY]
    g["swing"] = diurnal_swing(df["temp"]).reindex(g.index)
    return g


def daily_outdoor(out: pd.DataFrame) -> pd.DataFrame:
    g = out["temp"].resample("D").agg(["min", "max", "mean"])
    g["swing"] = diurnal_swing(out["temp"]).reindex(g.index)
    return g


def weather_memory(di: pd.DataFrame, do: pd.DataFrame) -> dict:
    """Fit indoor daily mean ≈ a + b × (outdoor daily mean, exponentially
    weighted over the last τ days), keeping the τ that fits best.

    The flat's heavy mass answers to the weather of the past few days, not the
    past few hours: τ is that memory, b the °C gained indoors per °C outdoors.
    """
    best = None
    for tau in MEMORY_TAUS_D:
        ew = do["mean"].ewm(alpha=1 - math.exp(-1 / tau)).mean().rename("x")
        j = pd.concat([di["mean"].rename("y"), ew], axis=1, join="inner").dropna()
        r = j["y"].corr(j["x"])
        if best is None or r > best[1]:
            best = (tau, r, j)
    tau, r, j = best
    b, a = np.polyfit(j["x"], j["y"], 1)
    return {"tau": tau, "r": float(r), "a": float(a), "b": float(b), "pts": j,
            # outdoor level where the fitted indoor mean crosses 26.5 °C
            "tip": (COMFORT_T - a) / b, "tip_alt": (COMFORT_T - ALT_SHIFT - a) / b}


def nightly_cooling(df: pd.DataFrame, out: pd.DataFrame, fig4: pd.Series) -> pd.DataFrame:
    """Per night (22:00 → 08:00): what the outdoor air offered, what the flat shed,
    how much outdoor air came in, and whether the night mattered.

    avail = indoor at ~22:00 minus the night's outdoor minimum (the usable gradient);
    gap = indoor at ~22:00 minus the night's outdoor mean; shed = indoor at ~22:00
    minus the night's indoor minimum; vent = the overnight fall in indoor absolute
    humidity — outdoor air coming in dilutes it, so it traces the air exchange.
    kind = "warm" (flat above the Fig. 4 comfort limit at 22:00), "cold" (heating
    day, see HEATING_DAY_T) or "mild".
    """
    rows = []
    for d in pd.date_range(df.index.min().normalize(),
                           df.index.max().normalize(), freq="D"):
        t0, t1 = d + pd.Timedelta(hours=22), d + pd.Timedelta(hours=32)
        night = df.loc[t0:t1].dropna(subset=["temp"])
        night_out = out["temp"].loc[t0:t1].dropna()
        # need an evening reading before midnight and coverage into the morning
        if (night.empty or night_out.empty
                or night.index[0] >= d + pd.Timedelta(hours=26)
                or night.index[-1] < d + pd.Timedelta(hours=30)):
            continue
        start_in = night["temp"].iloc[0]
        day_mean = out["temp"].loc[d:d + pd.Timedelta(hours=23)].mean()
        kind = ("warm" if start_in > fig4.asof(night.index[0])
                else "cold" if day_mean < HEATING_DAY_T else "mild")
        rows.append({"night": d, "kind": kind,
                     "avail": start_in - night_out.min(),
                     "gap": start_in - night_out.mean(),
                     "shed": start_in - night["temp"].min(),
                     "vent": night["abshum"].iloc[0] - night["abshum"].min()})
    return pd.DataFrame(rows).set_index("night")


def heatmap_matrix(df: pd.DataFrame):
    t = df.dropna(subset=["temp"]).copy()
    t["day"] = t.index.normalize()
    t["hour"] = t.index.hour
    # deviation from each day's own mean: removes the seasonal level, so the colour
    # shows the daily rhythm (when the flat warms and cools) rather than hot days
    t["dev"] = t["temp"] - t.groupby("day")["temp"].transform("mean")
    piv = t.pivot_table(index="day", columns="hour", values="dev", aggfunc="mean")
    days = pd.date_range(df.index.min().normalize(), df.index.max().normalize(), freq="D")
    piv = piv.reindex(index=days, columns=range(24)).round(2)
    return [d.strftime("%Y-%m-%d") for d in piv.index], list(range(24)), piv.values.tolist()


def zone_hours(temp: pd.Series, lim: pd.DataFrame) -> pd.DataFrame:
    """Per month (April → October, the Fig. 4 season), hours in each zone:
    ok = at or below Fig. 4 (comfortable); warm = above Fig. 4 but within the SIA 180
    Fig. 3 limit (accepted by the norm, too warm for comfort); over = above Fig. 3.
    ``lim`` holds the per-reading fig3 / fig4 limits (Fig. 3 is always the higher)."""
    t = temp[in_season(temp.index, FIG4_SEASON)]
    total, above4, above3 = (hours_over(t, x) for x in (-math.inf, lim["fig4"], lim["fig3"]))
    return pd.DataFrame({"ok": total - above4, "warm": above4 - above3,
                         "over": above3}).resample("MS").sum()


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #
def base_layout(fig, height=420, title=None):
    fig.update_layout(
        height=height, title=title, template="plotly_white",
        margin=dict(l=60, r=60, t=50 if title else 20, b=40),
        font=dict(family="Inter, system-ui, sans-serif", color=INK, size=13),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified", plot_bgcolor="white", paper_bgcolor="white",
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


def add_limit_lines(fig, lim, fig3=True):
    """Minergie's hourly limit curves (SIA 180 Fig. 4, optionally Fig. 3)."""
    curves = [("fig4", f"Fig. 4 comfort limit (Minergie: ≤ {MINERGIE_MAX_H} h/a above)",
               C_FIG4, "dash")]
    if fig3:
        curves.append(("fig3", "Fig. 3 SIA 180 limit (never above)", C_FIG3, "solid"))
    for col, name, color, dash in curves:
        fig.add_trace(go.Scatter(
            x=lim.index, y=lim[col].round(2), name=name, mode="lines",  # rounded for display only
            line=dict(color=color, width=1.6, dash=dash),
            hovertemplate=f"%{{y:.1f}} °C<extra>{col.replace('fig', 'Fig. ')} limit</extra>"))


def fig_overview(df, out, lim, rangeslider=True):
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=out.index, y=out["temp"], name="Outdoor (Pully)", mode="lines",
        line=dict(color=C_OUT, width=1.3), connectgaps=False,
        hovertemplate="%{y:.1f} °C<extra>Outdoor</extra>"))
    # Hold each reading flat across the missing 15-min slots until the next one, so
    # sparse data (e.g. hourly live polls) draws as a connected flat line instead of
    # lone dots. Capped at ~1 h (4 slots) so genuine offline gaps still break the line.
    indoor = df["temp"].ffill(limit=4)
    fig.add_trace(go.Scatter(
        x=indoor.index, y=indoor, name="Indoor", mode="lines",
        line=dict(color=C_IN, width=1.6, shape="hv"), connectgaps=False,
        legendgroup="indoor", hovertemplate="%{y:.1f} °C<extra>Indoor</extra>"))
    # Fallback: a reading still alone after the hold (first point after a long gap,
    # nothing yet after it) can't draw as a line — show that one as a dot.
    lone = indoor[indoor.notna() & indoor.shift().isna() & indoor.shift(-1).isna()]
    fig.add_trace(go.Scatter(
        x=lone.index, y=lone, mode="markers", name="Indoor", legendgroup="indoor",
        showlegend=False, marker=dict(color=C_IN, size=5),
        hovertemplate="%{y:.1f} °C<extra>Indoor</extra>"))
    add_limit_lines(fig, lim, fig3=False)
    base_layout(fig, 460)
    fig.update_yaxes(title_text="Temperature (°C)")
    if rangeslider:
        fig.update_xaxes(rangeslider=dict(visible=True), rangeslider_thickness=0.06)
    return fig


def fig_daily(di, do, lim, over3):
    """Daily indoor temperature vs the two SIA 180 limit curves, emphasising the
    overnight floor (daily minimum). ``over3`` = hours above Fig. 3 per day; those
    days get a marker, since a few hours are invisible in the band."""
    x = di.index
    # daily means of the hourly limits (θrm is a 48-h mean, so they barely move within a
    # day) — keeps every trace on the day grid, so the hover reads one row per day
    lim = lim.resample("D").mean().reindex(x).round(2)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, y=di["max"], name="Daily peak", mode="lines",
        line=dict(color="rgba(232,99,58,0.45)", width=1),
        hovertemplate="%{y:.1f} °C<extra>peak</extra>"))
    fig.add_trace(go.Scatter(
        x=x, y=di["min"], name="Overnight floor (daily min)", mode="lines",
        fill="tonexty", fillcolor=C_IN_FILL, line=dict(color="#a8551d", width=2),
        hovertemplate="%{y:.1f} °C<extra>overnight floor</extra>"))
    fig.add_trace(go.Scatter(
        x=x, y=di["mean"], name="Daily mean", mode="lines",
        line=dict(color=C_IN, width=2.6),
        hovertemplate="%{y:.1f} °C<extra>mean</extra>"))
    add_limit_lines(fig, lim)
    hit = over3[over3 > 0].reindex(x).dropna()
    fig.add_trace(go.Scatter(
        x=hit.index, y=di["max"].reindex(hit.index), name="Above the SIA 180 limit",
        mode="markers", marker=dict(symbol="diamond", size=11, color=C_FIG3,
                                    line=dict(color="white", width=1.5)),
        customdata=hit.values,
        hovertemplate="%{customdata:.1f} h above the SIA 180 limit<extra></extra>"))
    # context only — hidden by default so it doesn't squash the indoor scale
    fig.add_trace(go.Scatter(
        x=do.index, y=do["mean"], name="Outdoor daily mean", mode="lines", visible="legendonly",
        line=dict(color=C_OUT, width=2, dash="dot"),
        hovertemplate="%{y:.1f} °C<extra>outdoor mean</extra>"))
    base_layout(fig, 480)
    fig.update_layout(legend_traceorder="normal")  # fill="tonexty" flips it otherwise
    fig.update_yaxes(title_text=f"Indoor temp — {TEMP_REF} (°C)")
    return fig


def fig_hours(zones, zones_alt, cum, cum_alt):
    """Left: hours per month by zone (comfortable / accepted by the norm but too warm /
    above the SIA 180 limit), for both references. Right: the running total above
    Fig. 4 against Minergie's 100 h/a and SIA 382/1's 400 h/a."""
    fig = make_subplots(rows=1, cols=2, column_widths=[0.58, 0.42], horizontal_spacing=0.09,
                        subplot_titles=("Hours per month, by zone",
                                        "Running total, hours above Fig. 4"))
    ref, alt = ("living", "sensor") if APPLY_OFFSET else ("sensor", "living")
    both = pd.concat([zones.assign(who=ref), zones_alt.assign(who=alt)]).sort_index(kind="stable")
    x = [[p.strftime("%b") for p in both.index], both["who"].tolist()]
    for col, name, color in (("ok", "comfortable (≤ Fig. 4)", C_OK),
                             ("warm", "too warm, yet accepted by SIA 180", C_FIG4),
                             ("over", "above the SIA 180 limit (Fig. 3)", C_FIG3)):
        fig.add_trace(go.Bar(
            x=x, y=both[col], name=name, marker=dict(color=color, line=dict(color="white", width=1)),
            # amber: hours inside the segment; violet: often a sliver, so its hours go on top
            text=[(f"{v:.0f}" if v >= 40 else "") if col == "warm"
                  else (f'<span style="color:{C_FIG3}">◆</span> {v:.0f} h' if v >= 0.5 else "")
                  if col == "over" else ""
                  for v in both[col]],
            textposition="outside" if col == "over" else "inside", cliponaxis=False,
            insidetextanchor="middle", textfont=dict(size=10, color=INK),
            hovertemplate=f"%{{y:.0f}} h {name}<extra>%{{x}}</extra>"), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=cum.index, y=cum.values, name=REF_SHORT, mode="lines",
        line=dict(color=C_IN, width=2.6),
        hovertemplate=f"%{{y:.0f}} h<extra>{REF_SHORT}</extra>"), row=1, col=2)
    fig.add_trace(go.Scatter(
        x=cum_alt.index, y=cum_alt.values, name=ALT_SHORT, mode="lines",
        line=dict(color=C_IN, width=1.6, dash="dot"),
        hovertemplate=f"%{{y:.0f}} h<extra>{ALT_SHORT}</extra>"), row=1, col=2)
    for y, text, color in ((MINERGIE_MAX_H, f"Minergie: {MINERGIE_MAX_H} h/a", C_FIG4),
                           (SIA_MAX_H, f"SIA 382/1, homes with ventilation: {SIA_MAX_H} h/a",
                            "#6b6a65")):
        fig.add_hline(y=y, row=1, col=2, line=dict(color=color, width=1.6, dash="dash"),
                      annotation_text=text, annotation_position="top left",
                      annotation_font_size=11)
    base_layout(fig, 460)
    fig.update_layout(barmode="stack", bargap=0.25, hovermode="closest",
                      legend=dict(y=1.12, traceorder="normal"),
                      uniformtext=dict(minsize=10, mode="hide"))  # drop labels that don't fit
    fig.update_xaxes(tickfont_size=11, tickangle=0, row=1, col=1)
    fig.update_yaxes(title_text="Hours", row=1, col=1)
    fig.update_yaxes(rangemode="tozero", row=1, col=2)
    return fig


def fig_memory(mem):
    """Indoor daily mean against the recent outdoor weather, with the fit."""
    p = mem["pts"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=p["x"], y=p["y"], name="One day", mode="markers",
        marker=dict(color=C_IN, size=9, opacity=0.8, line=dict(color="white", width=1)),
        customdata=[d.strftime("%a %d %b") for d in p.index],
        hovertemplate=("%{customdata}<br>indoor mean %{y:.1f} °C"
                       "<br>outdoor, recent days %{x:.1f} °C<extra></extra>")))
    xs = np.linspace(p["x"].min(), p["x"].max(), 2)
    fig.add_trace(go.Scatter(
        x=xs, y=mem["a"] + mem["b"] * xs, mode="lines", hoverinfo="skip",
        name=f"Fit: +{mem['b']:.2f} °C per °C outdoors (r = {mem['r']:.2f})",
        line=dict(color=INK, width=2)))
    base_layout(fig, 460)
    fig.update_layout(hovermode="closest")
    fig.update_xaxes(title_text=f"Outdoor daily mean, weighted over the last ~{mem['tau']} days (°C)")
    fig.update_yaxes(title_text=f"Indoor daily mean — {REF_SHORT} (°C)")
    fig.add_hline(y=COMFORT_T, line=dict(color=C_FIG4, width=1.6, dash="dash"),
                  annotation_text=f"{COMFORT_T:g} °C — Minergie Fig. 4 limit in summer",
                  annotation_position="top left")
    fig.add_vline(x=mem["tip"], line=dict(color=C_FIG4, width=1.4, dash="dot"),
                  annotation_text=f"tipping point ≈ {mem['tip']:.1f} °C",
                  annotation_position="bottom right")
    return fig


def fig_night(nc):
    """Left: warm nights — how much of the cooling on offer the flat used, coloured by
    the humidity drop. Right: cold nights — how much heat it lost overnight."""
    warm, cold = nc[nc["kind"] == "warm"], nc[nc["kind"] == "cold"]
    fig = make_subplots(rows=1, cols=2, column_widths=[0.6, 0.4], horizontal_spacing=0.22,
                        subplot_titles=(f"Warm nights: flat too warm at 22:00 ({len(warm)})",
                                        f"Cold nights: heating days ({len(cold)})"))
    fig.add_trace(go.Scatter(
        x=warm["avail"], y=warm["shed"], mode="markers", name="warm night",
        marker=dict(color=warm["vent"].clip(lower=0), colorscale=SEQ_BLUE, size=10,
                    cmin=0, cmax=float(nc["vent"].quantile(0.95)),
                    line=dict(color="white", width=1),
                    colorbar=dict(title=dict(text="Humidity<br>drop<br>(g/m³)"),
                                  thickness=10, len=0.8, x=0.505)),
        customdata=np.c_[[d.strftime("%a %d %b") for d in warm.index], warm["vent"].round(1)],
        hovertemplate=("%{customdata[0]}<br>offered %{x:.1f} °C, cooled %{y:.1f} °C"
                       "<br>humidity drop %{customdata[1]} g/m³<extra></extra>")), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=cold["gap"], y=cold["shed"], mode="markers", name="cold night",
        marker=dict(color=C_OUT, size=10, line=dict(color="white", width=1)),
        customdata=[d.strftime("%a %d %b") for d in cold.index],
        hovertemplate=("%{customdata}<br>%{x:.1f} °C warmer than outside, "
                       "lost %{y:.1f} °C<extra></extra>")), row=1, col=2)
    base_layout(fig, 440)
    fig.update_layout(hovermode="closest", showlegend=False)
    fig.update_xaxes(title_text="Cooling offered: evening indoor − night outdoor min (°C)",
                     row=1, col=1)
    fig.update_xaxes(title_text="Indoor − outdoor, night mean (°C)", row=1, col=2)
    fig.update_yaxes(title_text="Indoor drop overnight (°C)", rangemode="tozero", row=1, col=1)
    fig.update_yaxes(title_text="Heat lost overnight (°C)", rangemode="tozero", row=1, col=2)
    return fig


def fig_heatmap(days, hours, z):
    # pre-formatted labels: plotly's heatmap hover ignores a %{z:+.2f} format and
    # prints the raw float (e.g. -0.7343749999999964)
    labels = [["" if v is None or v != v else f"{v:+.2f}" for v in row] for row in z]
    fig = go.Figure(go.Heatmap(
        z=z, x=hours, y=days, text=labels, colorscale=DIVERGING,
        zmid=0, zmin=-RHYTHM_RANGE, zmax=RHYTHM_RANGE,
        colorbar=dict(title="°C vs<br>day mean"), hoverongaps=False,
        hovertemplate="%{y}  %{x}:00<br>%{text} °C vs that day's mean<extra></extra>"))
    base_layout(fig, max(520, len(days) * 5))
    fig.update_layout(hovermode="closest")  # one cell per hover, not a whole column
    fig.update_xaxes(title_text="Hour of day", dtick=2, side="top")
    fig.update_yaxes(title_text="", autorange="reversed")
    return fig


# --------------------------------------------------------------------------- #
# HTML assembly
# --------------------------------------------------------------------------- #
def div(fig, name):
    return pio.to_html(fig, full_html=False, include_plotlyjs=False,
                       div_id=name, config=CONFIG)


def ladder_html(s) -> str:
    """The same measured hours against each yardstick, from the law to Minergie."""
    def verdict(value, limit):
        ok = value <= limit
        return (f'<span class="{"ok" if ok else "ko"}">{"✓" if ok else "✗"}</span> '
                f'{value:,.0f} h' + ("" if ok or not limit else f" ({value / limit:.0f}×)"))
    rows = [
        ("Vaud law<br><small>RLVLEne art. 19c</small>",
         "Only the g-value of the sun blinds (design check)",
         "no temperature limit", "not measurable", "—"),
        ("SIA 180<br><small>Fig. 3</small>",
         "Moves with the last 48 h outdoors: 25 °C when cool, 30 °C once they average 25 °C",
         "0 h",
         verdict(s["fig3_h"], 0), verdict(s["fig3_h_alt"], 0)),
        ("SIA 382/1<br><small>homes with ventilation</small>",
         "Comfort limit, Fig. 4, same principle: 24.5 °C when cool, 26.5 °C once they average 17.5 °C",
         f"{SIA_MAX_H} h/year",
         verdict(s["fig4_h"], SIA_MAX_H), verdict(s["fig4_h_alt"], SIA_MAX_H)),
        ("Minergie<br><small>guide 2025 §6</small>",
         "Same comfort limit (Fig. 4)", f"{MINERGIE_MAX_H} h/year",
         verdict(s["fig4_h"], MINERGIE_MAX_H), verdict(s["fig4_h_alt"], MINERGIE_MAX_H)),
        ("Livability<br><small>no rule</small>",
         f"Nights that stayed above {COMFORT_T:g} °C · hottest moment", "—",
         f"{s['warm_nights']} nights · {s['t_max']:.1f} °C<br><small>{s['t_max_when']}</small>",
         f"{s['warm_nights_alt']} nights · {s['t_max'] + ALT_SHIFT:.1f} °C"),
    ]
    head = ("<tr><th>Rule</th><th>Checks</th><th>Allowed</th>"
            f"<th>{REF_SHORT.capitalize()}</th><th>{ALT_SHORT.capitalize()}</th></tr>")
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="ladder"><table>{head}{body}</table></div>'


def sources_html(s) -> str:
    links = {
        "switchbot": "https://github.com/OpenWonderLabs/SwitchBotAPI",
        "meteoswiss": "https://opendatadocs.meteoswiss.ch/a-data-groundbased/a1-automatic-weather-stations",
        "pully": "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/puy/ogd-smn_puy_h_recent.csv",
        "minergie": "https://www.minergie.ch/media/250701_anwendungshilfe_gebaeudestandards_minergie_2025-2_de.pdf",
        "sia180": "https://shop.sia.ch/normenwerk/architekt/sia%20180/d/2014/D/Product",
        "rlvlene": "https://www.vd.ch/fileadmin/user_upload/organisation/dinf/sipal/fichiers_pdf/reglement_d_application_LEnE.pdf",
        "envs": "https://www.vs.ch/documents/16739272/35472472/Aide+EN-VS-102_d%C3%A9f.pdf",
        "repo": "https://github.com/xdubois/temp_malley",
    }
    a = lambda key, text: f'<a href="{links[key]}">{text}</a>'  # noqa: E731
    items = [
        ("Indoor", f"SwitchBot Hub 2 in the entrance corridor, out of direct sun — app export "
                   f"(15 min) + live polls via the {a('switchbot', 'SwitchBot API')}. "
                   f"{s['n_indoor']:,} readings, {s['span']}. Living rooms = sensor "
                   f"+{SENSOR_OFFSET:g} °C."),
        ("Outdoor", f"{a('meteoswiss', 'MeteoSwiss')} station Pully (PUY), hourly measurements "
                    f"({a('pully', 'raw data')}), up to {s['out_last']}."),
        ("Comfort limits", f"{a('minergie', 'Minergie guide 2025, §6')}, using "
                           f"{a('sia180', 'SIA 180:2014')} Fig. 3 and 4 with the outdoor mean of the "
                           f"previous {RM_HOURS} h. The published Fig. 3 curve stops at a 25 °C "
                           "average; above that the limit is held at 30 °C. SIA 382/1 allows "
                           f"{SIA_MAX_H} h/year above Fig. 4 in homes with mechanical ventilation."),
        ("Law", f"Vaud {a('rlvlene', 'RLVLEne art. 19c')}; the Valais guide "
                f"{a('envs', 'EN-VS-102')} explains the same rules."),
        ("Method", f"Each reading counts until the next one (max {READING_CAP_H:g} h). Days with "
                   f"under {MIN_HOURS_DAY} h of data are left out of daily charts. Heating day = "
                   f"outdoor daily mean below {HEATING_DAY_T:g} °C."),
        ("Code", f"{a('repo', 'github.com/xdubois/temp_malley')} — rebuild with "
                 "<code>uv run build_dashboard.py</code>."),
    ]
    lis = "".join(f"<li><b>{k}</b> — {v}</li>" for k, v in items)
    return f'<section class="sources"><h2>Data sources</h2><ul>{lis}</ul></section>'


def render(summary, figs) -> str:
    s = summary
    built = pd.Timestamp.now(tz=TZ).strftime("%d %b %Y, %H:%M")
    both = lambda a, b, unit="": f'{a}{unit} <span class="alt">/ {b}{unit}</span>'  # noqa: E731
    pair = f"{REF_SHORT.split(' (')[0]} / {ALT_SHORT.split(' (')[0]}"
    cards = [
        ("Latest reading", f"{s['last_t']:.1f} °C",
         f"{s['last_when']} · {s['last_rh']:.0f} % RH"),
        ("Above the comfort limit", both(f"{s['fig4_h']:,.0f}", f"{s['fig4_h_alt']:,.0f}", " h"),
         f"{pair} · Minergie allows {MINERGIE_MAX_H} h, SIA {SIA_MAX_H} h"),
        ("Above the SIA 180 limit", both(f"{s['fig3_h']:,.0f}", f"{s['fig3_h_alt']:,.0f}", " h"),
         f"{pair} · allowed 0 h"),
        ("Warm nights", both(s["warm_nights"], s["warm_nights_alt"]),
         f"{pair} · never below {COMFORT_T:g} °C"),
        ("Tipping point", f"{s['tip']:.1f} °C",
         f"outdoor 3-day mean above which the {REF_SHORT.split(' (')[0]} passes "
         f"{COMFORT_T:g} °C ({ALT_SHORT.split(' (')[0]}: {s['tip_alt']:.1f} °C)"),
        ("Weather memory", f"~{s['tau']} days",
         f"+{s['b']:.2f} °C indoors per °C outdoors"),
        ("Night cooling used", f"{s['night_pct']:.0f} %",
         "of what the night air offered, when the flat was too warm"),
        ("Damping", f"{s['buffer']:.0f}×",
         f"daily swing {s['swing_in']:.1f} °C indoors vs {s['swing_out']:.1f} °C outdoors"),
    ]
    card_html = "\n".join(
        f'<div class="card"><div class="k">{k}</div>'
        f'<div class="v">{v}</div><div class="s">{sub}</div></div>'
        for k, v, sub in cards)

    sections = [
        ("Indoor vs outdoor",
         "Indoor readings against the measured outdoor temperature. Dashed: the comfort "
         "limit (SIA 180 Fig. 4), which follows the last 48 h of weather. Drag to zoom.",
         "overview"),
        ("Last 7 days", "Mostly live polls, one reading every 30–60 min.", "recent"),
        ("Daily temperature vs the limits",
         f"{REF_SHORT.capitalize()} readings. Violet: the SIA 180 limit (Fig. 3), never to be "
         "exceeded. It follows the last 48 h of outdoor temperature — 25 °C in cool weather, "
         "rising to 30 °C once those 48 h average 25 °C. Dashed amber: the comfort limit "
         "(Fig. 4), 24.5 → 26.5 °C on the same principle. ◆ = days above the violet line.",
         "daily"),
        ("Legally fine ≠ livable",
         "The same hours judged by each rule. The law only checks the sun blinds. SIA 180's "
         "limit rises with the weather: whenever the flat was too warm, it stood at "
         f"{s['fig3_when_hot']} °C. Minergie allows {MINERGIE_MAX_H} h a year above the "
         "comfort limit. Amber = too warm, yet accepted by SIA 180; ◆ = hours above the SIA 180 limit.",
         "hours"),
        ("What drives the indoor temperature",
         f"One dot per day. The flat follows the last ~{s['tau']} days of weather, "
         f"+{s['b']:.2f} °C per °C outdoors. Right of the dotted line it averages above "
         f"{COMFORT_T:g} °C.",
         "memory"),
        ("Nights",
         "Left: nights when the flat was too warm at 22:00 — how much of the cool night "
         "air it used. Colour = drop in indoor humidity, a sign that outdoor air got in. "
         f"Right: cold nights (outdoor daily mean below {HEATING_DAY_T:g} °C) — how much heat "
         "it lost; this fills in over winter. Mild nights are left out.",
         "night"),
        ("Daily rhythm",
         "Each cell: that hour vs the day's mean. Coolest at dawn, warmest around 20:00, "
         f"when the {ORIENTATION.lower()} façade gets the evening sun.",
         "heatmap"),
    ]
    extra = {"hours": ladder_html(s)}  # the comparison table sits above its chart
    sec_html = "\n".join(
        f'<section><h2>{i+1}. {title}</h2><p class="desc">{desc}</p>{extra.get(key, "")}'
        f'<div class="chart">{figs[key]}</div></section>'
        for i, (title, desc, key) in enumerate(sections))

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Malley apartment — temperature dashboard</title>
<script>{get_plotlyjs()}</script>
<style>
  :root {{ --ink:#2b2b2b; --muted:#777; --line:#ececec; --bg:#f7f7f5; --accent:#e8633a; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
  header {{ padding:32px 24px 8px; max-width:1180px; margin:0 auto; }}
  h1 {{ margin:0 0 4px; font-size:26px; }}
  .sub {{ color:var(--muted); font-size:14px; }}
  .note {{ color:var(--muted); font-size:12.5px; margin-top:6px; max-width:820px; line-height:1.45; }}
  .measures {{ max-width:1132px; margin:14px auto 0; padding:10px 16px; background:#fff;
    border:1px solid var(--line); border-left:3px solid #e0a800; border-radius:10px;
    font-size:13px; color:var(--ink); }}
  .measures ul {{ margin:4px 0 0; padding-left:18px; color:var(--muted); line-height:1.5; }}
  @media (max-width:1180px) {{ .measures {{ margin:14px 24px 0; }} }}
  .cards {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
    max-width:1180px; margin:20px auto 8px; padding:0 24px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:14px 16px; }}
  .card .k {{ font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  .card .v {{ font-size:19px; font-weight:600; margin:4px 0 2px; }}
  .card .v .alt {{ font-size:15px; font-weight:500; color:var(--muted); }}
  .card .s {{ font-size:12.5px; color:var(--muted); }}
  section {{ background:#fff; border:1px solid var(--line); border-radius:14px;
    max-width:1180px; margin:18px auto; padding:18px 22px 8px; }}
  h2 {{ font-size:18px; margin:0 0 4px; }}
  .desc {{ color:var(--muted); font-size:13.5px; margin:0 0 10px; max-width:820px; line-height:1.45; }}
  .chart {{ width:100%; overflow-x:auto; }}
  .ladder {{ overflow-x:auto; margin:4px 0 12px; }}
  .ladder table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  .ladder th, .ladder td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line);
    vertical-align:top; }}
  .ladder th {{ font-size:12px; text-transform:uppercase; letter-spacing:.03em; color:var(--muted);
    font-weight:600; }}
  .ladder td:nth-child(n+4) {{ white-space:nowrap; font-variant-numeric:tabular-nums; }}
  .ladder small {{ color:var(--muted); }}
  .ladder .ok {{ color:#0ca30c; font-weight:700; }}
  .ladder .ko {{ color:#d03b3b; font-weight:700; }}
  .sources ul {{ margin:6px 0 12px; padding-left:18px; font-size:13px; line-height:1.55;
    color:var(--muted); }}
  .sources b {{ color:var(--ink); font-weight:600; }}
  .sources a {{ color:#256abf; }}
  footer {{ max-width:1180px; margin:8px auto 48px; padding:0 24px; color:var(--muted); font-size:12px; }}
</style></head>
<body>
<header>
  <h1>Malley apartment — heat impact dashboard</h1>
  <div class="sub">{s['span']} · updated {built}</div>
  <div class="note">The sensor sits in the entrance corridor, out of direct sun. The living
    rooms face {ORIENTATION.lower()} with about half of each outer wall in glass; they are
    estimated at sensor +{SENSOR_OFFSET:g} °C and probably run hotter on sunny evenings, so
    read their figures as a minimum.</div>
</header>
<div class="measures"><b>Cooling measures in place.</b> Measured while the flat was kept
  cool by hand; the heat shown is what remains despite them:
  <ul>{"".join(f"<li>{m}</li>" for m in COOLING_MEASURES)}</ul></div>
<div class="cards">{card_html}</div>
{sec_html}
{sources_html(s)}
<footer>Built {built}.</footer>
</body></html>"""


def main():
    start_date = START_DATE
    for a in sys.argv[1:]:
        if a.startswith("--from="):
            start_date = a.split("=", 1)[1]

    print(f"Loading indoor 15-min data (from {start_date})…", file=sys.stderr)
    df = load_indoor([CSV_15MIN, CSV_AUTO], start_date)
    start = df.index.min().strftime("%Y-%m-%d")
    end = df.index.max().strftime("%Y-%m-%d")
    n_indoor = int(df["temp"].notna().sum())
    print(f"  {n_indoor} readings, {start} → {end}", file=sys.stderr)

    print(f"Loading outdoor weather ({OUTDOOR_STATION})…", file=sys.stderr)
    out_all = load_outdoor(CSV_OUTDOOR)
    if out_all.index.max() < df.index.max() - pd.Timedelta(hours=3):
        print(f"  WARNING: outdoor data ends {out_all.index.max()} — run "
              "`uv run fetch_outdoor.py` to refresh.", file=sys.stderr)
    # the weeks before the first indoor reading only warm up the running means
    do_all = daily_outdoor(out_all[out_all.index <= df.index.max()])
    out = out_all[(out_all.index >= df.index.min()) & (out_all.index <= df.index.max())]
    do = do_all[do_all.index >= df.index.min().normalize()]
    lim_all = minergie_limits(out_all[out_all.index <= df.index.max()])
    lim = lim_all[lim_all.index >= df.index.min().floor("h")]
    print(f"  {len(out)} hourly rows", file=sys.stderr)

    di = daily_indoor(df)
    hm_days, hm_hours, hm_z = heatmap_matrix(df)
    nc = nightly_cooling(df, out, lim_all["fig4"])
    mem = weather_memory(di, do_all)

    temp_alt = df["temp"] + ALT_SHIFT
    lim_r = pd.DataFrame({c: per_reading(lim_all[c], df.index) for c in ("fig3", "fig4")})
    zones = zone_hours(df["temp"], lim_r)
    zones_alt = zone_hours(temp_alt, lim_r)
    over4 = minergie_hours(df["temp"], lim_r["fig4"], FIG4_SEASON)
    over4_alt = minergie_hours(temp_alt, lim_r["fig4"], FIG4_SEASON)
    warm = di["min"] > COMFORT_T
    tmax = df["temp"].idxmax()
    warm_n = nc[nc["kind"] == "warm"]
    offered = warm_n[warm_n["avail"] > 1]  # skip nights with (almost) nothing on offer
    over3 = minergie_hours(df["temp"], lim_r["fig3"], FIG3_SEASON)
    # the Fig. 3 limit at the times it mattered: while the flat was above the comfort limit
    lo, hi = lim_r["fig3"][df["temp"] > lim_r["fig4"]].quantile([0.1, 0.9]).round(1)
    last = df.dropna(subset=["temp"]).iloc[-1]
    swing_in, swing_out = di["swing"].mean(), do["swing"].reindex(di.index).mean()

    summary = {
        "span": f"{df.index.min():%d %b} → {df.index.max():%d %b %Y}",
        "out_last": f"{out.index.max():%d %b %H:%M}",
        "n_indoor": n_indoor,
        "n_days": int(len(di)),
        "last_t": float(last["temp"]),
        "last_rh": float(last["hum"]),
        "last_when": f"{last.name:%a %d %b, %H:%M}",
        "fig4_h": float(over4.sum()),
        "fig4_h_alt": float(over4_alt.sum()),
        "fig3_h": float(over3.sum()),
        "fig3_when_hot": f"{lo:.1f}" if lo == hi else f"{lo:.1f}–{hi:.1f}",
        "fig3_h_alt": float(minergie_hours(temp_alt, lim_r["fig3"], FIG3_SEASON).sum()),
        "warm_nights": int(warm.sum()),
        "warm_nights_alt": int((di["min"] + ALT_SHIFT > COMFORT_T).sum()),
        "t_max": float(df["temp"].max()),
        "t_max_when": f"{tmax:%a %d %b, %H:%M}",
        "tau": mem["tau"],
        "b": mem["b"],
        "mem_r": mem["r"],
        "tip": mem["tip"],
        "tip_alt": mem["tip_alt"],
        "night_pct": float(100 * (offered["shed"] / offered["avail"]).median()),
        "night_kinds": nc["kind"].value_counts().to_dict(),
        "avail_r": float(warm_n["shed"].corr(warm_n["avail"])),
        "vent_r": float(warm_n["shed"].corr(warm_n["vent"])),
        "swing_in": float(swing_in),
        "swing_out": float(swing_out),
        "buffer": float(swing_out / swing_in),
    }
    print("Summary:\n" + json.dumps(
        {k: (round(v, 2) if isinstance(v, float) else v) for k, v in summary.items()},
        indent=2, default=str), file=sys.stderr)

    week_ago = df.index.max() - pd.Timedelta(days=7)
    figs = {
        "overview": div(fig_overview(df, out, lim), "overview"),
        "recent": div(fig_overview(df[df.index >= week_ago],
                                   out[out.index >= week_ago],
                                   lim[lim.index >= week_ago], rangeslider=False), "recent"),
        "daily": div(fig_daily(di, do, lim, over3.resample("D").sum()), "daily"),
        # the Minergie limit is per calendar year, so the running total restarts each year
        "hours": div(fig_hours(zones, zones_alt, over4.groupby(over4.index.year).cumsum(),
                               over4_alt.groupby(over4_alt.index.year).cumsum()), "hours"),
        "memory": div(fig_memory(mem), "memory"),
        "night": div(fig_night(nc), "night"),
        "heatmap": div(fig_heatmap(hm_days, hm_hours, hm_z), "heatmap"),
    }
    html = render(summary, figs)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  wrote {OUT_HTML} ({os.path.getsize(OUT_HTML)/1e6:.1f} MB)", file=sys.stderr)


if __name__ == "__main__":
    main()
