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
TEMP_REF = (f"living-space estimate (sensor +{SENSOR_OFFSET:g} °C)"
            if APPLY_OFFSET else "entrance sensor (measured)")
# The other reference, shown alongside for the comfort numbers.
REF_SHORT, ALT_SHORT = (("living rooms (est.)", "entrance sensor") if APPLY_OFFSET
                        else ("entrance sensor", "living rooms (est.)"))
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

# Fig. 4's summer plateau: used for "warm nights" and the weather-memory tipping point.
COMFORT_T = FIG4_UPPER[-1][1]

# Candidate time constants (days) for the "weather memory" fit — the one whose
# exponentially weighted outdoor mean best explains the indoor daily mean wins.
MEMORY_TAUS_D = [1, 2, 3, 4, 5, 7, 10]

# Main façade / glazing orientation (shown on the dashboard).
ORIENTATION = "North-West"

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


def nightly_cooling(df: pd.DataFrame, out: pd.DataFrame) -> pd.DataFrame:
    """Per night (22:00 → 08:00): the cooling the outdoor air offered vs what
    the flat actually shed, and how much outdoor air came in.

    avail = indoor at ~22:00 minus the night's outdoor minimum (the usable
    gradient); shed = indoor at ~22:00 minus the night's indoor minimum;
    vent = the overnight fall in indoor absolute humidity — outdoor air coming
    in dilutes it, so it traces the air exchange.
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
        rows.append({"night": d, "avail": start_in - night_out.min(),
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
    piv = piv.reindex(index=days, columns=range(24))
    return [d.strftime("%Y-%m-%d") for d in piv.index], list(range(24)), piv.values.tolist()


def monthly_hours(temp: pd.Series, lim: pd.DataFrame) -> pd.DataFrame:
    """Per month: hours covered by readings and hours above each Minergie limit
    (``lim`` holds the per-reading fig3 / fig4 limits)."""
    m = pd.DataFrame({
        "covered": hours_over(temp, -math.inf).resample("MS").sum(),
        "fig4": minergie_hours(temp, lim["fig4"], FIG4_SEASON).resample("MS").sum(),
        "fig3": minergie_hours(temp, lim["fig3"], FIG3_SEASON).resample("MS").sum(),
    }).fillna(0)
    m["pct"] = 100 * m["fig4"] / m["covered"]
    return m


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
    curves = [("fig4", f"Minergie Fig. 4 limit (≤ {MINERGIE_MAX_H} h/a above)", C_FIG4, "dash")]
    if fig3:
        curves.append(("fig3", "Minergie Fig. 3 limit (never above)", C_FIG3, "solid"))
    for col, name, color, dash in curves:
        fig.add_trace(go.Scatter(
            x=lim.index, y=lim[col], name=name, mode="lines",
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


def fig_daily(di, do, lim):
    """Daily indoor temperature vs Minergie's two limit curves, emphasising the
    overnight floor (daily minimum)."""
    x = di.index
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
    # context only — hidden by default so it doesn't squash the indoor scale
    fig.add_trace(go.Scatter(
        x=do.index, y=do["mean"], name="Outdoor daily mean", mode="lines", visible="legendonly",
        line=dict(color=C_OUT, width=2, dash="dot"),
        hovertemplate="%{y:.1f} °C<extra>outdoor mean</extra>"))
    base_layout(fig, 480)
    fig.update_layout(legend_traceorder="normal")  # fill="tonexty" flips it otherwise
    fig.update_yaxes(title_text=f"Indoor temp — {TEMP_REF} (°C)")
    return fig


def fig_hours(mh, cum, cum_alt):
    """Hours above the Minergie limits per month (left) and the running total vs
    the 100 h/a limit (right) — two panels, each with its own scale."""
    fig = make_subplots(rows=1, cols=2, column_widths=[0.55, 0.45], horizontal_spacing=0.1,
                        subplot_titles=("Hours per month",
                                        "Running total, hours above SIA 180 Fig. 4"))
    months = [p.strftime("%b") for p in mh.index]
    for col, name, color in (("fig4", "above Fig. 4 (limit 100 h/a)", C_FIG4),
                             ("fig3", "above Fig. 3 (limit 0 h)", C_FIG3)):
        fig.add_trace(go.Bar(
            x=months, y=mh[col], name=name, marker_color=color,
            text=[f"{v:.0f} h" if v >= 0.5 else "" for v in mh[col]],
            textposition="outside", textfont_size=11, cliponaxis=False,
            customdata=mh["pct"] if col == "fig4" else None,
            hovertemplate=(f"%{{y:.0f}} h {name.split(' (')[0]}"
                           + (" (%{customdata:.0f} % of the month)" if col == "fig4" else "")
                           + "<extra></extra>")), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=cum.index, y=cum.values, name=REF_SHORT, mode="lines",
        line=dict(color=C_IN, width=2.6),
        hovertemplate=f"%{{y:.0f}} h<extra>{REF_SHORT}</extra>"), row=1, col=2)
    fig.add_trace(go.Scatter(
        x=cum_alt.index, y=cum_alt.values, name=ALT_SHORT, mode="lines",
        line=dict(color=C_IN, width=1.6, dash="dot"),
        hovertemplate=f"%{{y:.0f}} h<extra>{ALT_SHORT}</extra>"), row=1, col=2)
    fig.add_hline(y=MINERGIE_MAX_H, row=1, col=2,
                  line=dict(color=C_FIG4, width=1.8, dash="dash"),
                  annotation_text=f"Minergie limit: {MINERGIE_MAX_H} h / year",
                  annotation_position="top left")
    base_layout(fig, 440)
    fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08, hovermode="closest",
                      legend=dict(y=1.12), uniformtext=dict(minsize=11, mode="show"))
    fig.update_yaxes(title_text="Hours", row=1, col=1)
    fig.update_yaxes(rangemode="tozero", row=1, col=2)
    fig.update_yaxes(range=[0, mh[["fig4", "fig3"]].max().max() * 1.15 + 1],
                     row=1, col=1)  # room for labels
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
    fig = go.Figure(go.Scatter(
        x=nc["avail"], y=nc["shed"], mode="markers", name="One night",
        marker=dict(color=nc["vent"].clip(lower=0), colorscale=SEQ_BLUE, size=10,
                    cmin=0, cmax=float(nc["vent"].quantile(0.95)),
                    line=dict(color="white", width=1),
                    colorbar=dict(title=dict(text="Indoor<br>humidity<br>drop<br>(g/m³)"),
                                  thickness=12)),
        customdata=np.c_[[d.strftime("%a %d %b") for d in nc.index], nc["vent"]],
        hovertemplate=("night of %{customdata[0]}<br>offered %{x:.1f} °C, "
                       "achieved %{y:.1f} °C<br>humidity drop %{customdata[1]:.1f} g/m³"
                       "<extra></extra>")))
    base_layout(fig, 440)
    fig.update_layout(hovermode="closest")
    fig.update_xaxes(title_text="Cooling offered: evening indoor − night outdoor min (°C)")
    fig.update_yaxes(title_text="Indoor drop achieved (°C)", rangemode="tozero")
    return fig


def fig_heatmap(days, hours, z):
    fig = go.Figure(go.Heatmap(
        z=z, x=hours, y=days, colorscale=DIVERGING,
        zmid=0, zmin=-RHYTHM_RANGE, zmax=RHYTHM_RANGE,
        colorbar=dict(title="°C vs<br>day mean"), hoverongaps=False,
        hovertemplate="%{y}  %{x}:00<br>%{z:+.2f} °C vs that day's mean<extra></extra>"))
    base_layout(fig, max(520, len(days) * 5))
    fig.update_xaxes(title_text="Hour of day", dtick=2, side="top")
    fig.update_yaxes(title_text="", autorange="reversed")
    return fig


# --------------------------------------------------------------------------- #
# HTML assembly
# --------------------------------------------------------------------------- #
def div(fig, name):
    return pio.to_html(fig, full_html=False, include_plotlyjs=False,
                       div_id=name, config=CONFIG)


def render(summary, figs) -> str:
    s = summary
    built = pd.Timestamp.now(tz=TZ).strftime("%d %b %Y, %H:%M")
    off_note = (
        f"Readings have +{SENSOR_OFFSET:g} °C added to estimate living-space "
        f"temperature (the entrance sensor sits ~{SENSOR_OFFSET:g} °C cooler)."
        if APPLY_OFFSET else
        f"These are the raw entrance-sensor readings; the rest of the flat runs "
        f"~{SENSOR_OFFSET:g} °C warmer.")
    cards = [
        ("Latest reading", f"{s['last_t']:.1f} °C",
         f"{s['last_when']} · {s['last_rh']:.0f} % RH"),
        ("Minergie limit · Fig. 4", f"{s['fig4_h']:,.0f} h",
         f"above SIA 180 Fig. 4 · allowed {MINERGIE_MAX_H} h/a · {s['fig4_pct']:.0f} % of "
         f"the time · {ALT_SHORT} ≈ {s['fig4_h_alt']:,.0f} h"),
        ("Minergie comfort field · Fig. 3", f"{s['fig3_h']:,.0f} h",
         f"above SIA 180 Fig. 3 · allowed 0 h · {ALT_SHORT} ≈ {s['fig3_h_alt']:,.0f} h"),
        ("Warm nights", f"{s['warm_nights']} days",
         f"never dropped below 26.5 °C · longest run {s['warm_run']} days"),
        ("Tipping point", f"{s['tip']:.1f} °C outdoors",
         f"recent-days mean above which the {REF_SHORT} averages > {COMFORT_T:g} °C, "
         f"Minergie's summer limit ({ALT_SHORT}: {s['tip_alt']:.1f} °C)"),
        ("Weather memory", f"~{s['tau']} days",
         f"+{s['b']:.2f} °C indoors per +1 °C outdoors (r = {s['mem_r']:.2f})"),
        ("Night cooling used", f"{s['night_pct']:.0f} %",
         f"of what the night air offered (median) · follows air exchange "
         f"(r = {s['vent_r']:.2f}), not the gradient (r = {s['avail_r']:.2f})"),
        ("Damping", f"{s['buffer']:.0f}×",
         f"day↔night swing {s['swing_in']:.1f} °C indoors vs {s['swing_out']:.1f} °C outdoors"),
    ]
    card_html = "\n".join(
        f'<div class="card"><div class="k">{k}</div>'
        f'<div class="v">{v}</div><div class="s">{sub}</div></div>'
        for k, v, sub in cards)

    sections = [
        ("Indoor vs outdoor temperature",
         f"Every indoor reading against the measured hourly outdoor temperature at "
         f"{OUTDOOR_STATION}, ~6 km away on the same lakeshore. Dashed: Minergie's limit "
         "(SIA 180 Fig. 4), which follows the outdoor temperature of the previous 48 h. "
         "Drag on the chart or use the slider to zoom into any stretch.",
         "overview"),
        ("Last 7 days",
         "The same chart zoomed to the most recent week — mostly live polls "
         "(one reading every 30-60 min) rather than the 15-min export. A reading "
         "more than an hour from its neighbours shows as a lone dot.",
         "recent"),
        ("Daily temperature vs the Minergie limits",
         f"{off_note} Band = daily min→max, bold line = daily mean. Minergie's summer "
         "comfort rules use two SIA 180 curves that rise with the mean outdoor "
         "temperature of the previous 48 h: violet = Fig. 3, the comfort field, never to "
         "be exceeded (mid-April → mid-October); dashed amber = Fig. 4, which may be "
         f"exceeded at most {MINERGIE_MAX_H} h a year (April → October) before cooling is "
         "required. Minergie checks them in a design simulation of the most exposed room "
         "with 2035 weather; here they are applied to measured reality. Watch the "
         "overnight floor: when it climbs, the building can't shed the heat at night. "
         "Click “Outdoor daily mean” in the legend to add it.",
         "daily"),
        ("Hours above the Minergie limits",
         "Left: hours per month above SIA 180 Fig. 4 and Fig. 3, each counted within "
         "its own season (hover for the share of the month). Right: the running total "
         f"above Fig. 4 against Minergie's {MINERGIE_MAX_H} h/year limit, for the "
         f"{REF_SHORT} (solid) and the {ALT_SHORT} (dotted).",
         "hours"),
        ("What drives the indoor temperature",
         f"One dot per day: the indoor daily mean against the outdoor mean of the last "
         f"few days (weighted, τ ≈ {s['tau']} days — the memory that fits best, "
         f"r = {s['mem_r']:.2f}). The heavy flat follows the recent weather at "
         f"+{s['b']:.2f} °C per °C; the dotted line marks the outdoor level where it "
         f"crosses {COMFORT_T:g} °C, Minergie's Fig. 4 limit in summer. Dots above the black fit line ran hotter than the weather "
         "explains.",
         "memory"),
        ("Night cooling: what actually cools the flat",
         "Per night (22:00 → 08:00): the cooling the outdoor air offered (evening indoor "
         "minus the night's outdoor minimum) against the indoor drop achieved. Colour = "
         "how far indoor absolute humidity fell overnight — a tracer of outdoor air "
         "coming in. The drop follows the air exchange, not how cool the night was: a "
         "cool night only helps when the air actually gets in.",
         "night"),
        ("Daily rhythm",
         "Each row is a day, each column an hour. Colour = deviation from that day's "
         "own mean (blue cooler, red warmer), so the season's level is removed and the "
         f"daily cycle shows: coolest around dawn, warmest in the evening when the "
         f"{ORIENTATION.lower()} façade takes the low sun. Blank cells = missing readings.",
         "heatmap"),
    ]
    sec_html = "\n".join(
        f'<section><h2>{i+1}. {title}</h2><p class="desc">{desc}</p>'
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
  .cards {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
    max-width:1180px; margin:20px auto 8px; padding:0 24px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:14px 16px; }}
  .card .k {{ font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  .card .v {{ font-size:19px; font-weight:600; margin:4px 0 2px; }}
  .card .s {{ font-size:12.5px; color:var(--muted); }}
  section {{ background:#fff; border:1px solid var(--line); border-radius:14px;
    max-width:1180px; margin:18px auto; padding:18px 22px 8px; }}
  h2 {{ font-size:18px; margin:0 0 4px; }}
  .desc {{ color:var(--muted); font-size:13.5px; margin:0 0 10px; max-width:820px; line-height:1.45; }}
  .chart {{ width:100%; overflow-x:auto; }}
  footer {{ max-width:1180px; margin:8px auto 48px; padding:0 24px; color:var(--muted); font-size:12px; }}
</style></head>
<body>
<header>
  <h1>Malley apartment — heat impact dashboard</h1>
  <div class="sub">Indoor: {TEMP_REF} · {s['span']} · outdoor: {OUTDOOR_STATION},
    measured (to {s['out_last']}) · façade {ORIENTATION} · built {built}</div>
  <div class="note">Measured: air temperature at the entrance sensor, the coolest point
    of the flat — living rooms run ~{SENSOR_OFFSET:g} °C warmer. The high-inertia flat is
    thermally homogeneous (surfaces ≈ air), so operative temperature is close to the
    measured air temperature.</div>
</header>
<div class="cards">{card_html}</div>
{sec_html}
<footer>
  Indoor data: 15-min export + live SwitchBot polls ({s['n_indoor']:,} readings) from an
  entrance sensor, shown as {TEMP_REF}. Outdoor: {OUTDOOR_STATION}, hourly measurements
  (MeteoSwiss open data), {s['n_days']} days with data.
  Comfort limits: Minergie (Anwendungshilfe Gebäudestandards Minergie 2025, §6) after
  SIA 180:2014 — Fig. 3 never exceeded ({FIG3_SEASON[0]} → {FIG3_SEASON[1]}), Fig. 4 at
  most {MINERGIE_MAX_H} h/a ({FIG4_SEASON[0]} → {FIG4_SEASON[1]}); θrm = mean outdoor air
  temperature over the preceding {RM_HOURS} h at Pully.
  Days covering &lt;{MIN_HOURS_DAY} h are excluded from daily charts.
  Rebuild with <code>uv run build_dashboard.py</code>.
</footer>
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
    nc = nightly_cooling(df, out)
    mem = weather_memory(di, do_all)

    temp_alt = df["temp"] + ALT_SHIFT
    lim_r = pd.DataFrame({c: per_reading(lim_all[c], df.index) for c in ("fig3", "fig4")})
    mh = monthly_hours(df["temp"], lim_r)
    over4 = minergie_hours(df["temp"], lim_r["fig4"], FIG4_SEASON)
    over4_alt = minergie_hours(temp_alt, lim_r["fig4"], FIG4_SEASON)
    warm = di["min"] > COMFORT_T
    offered = nc[nc["avail"] > 1]  # skip nights with (almost) nothing on offer
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
        "fig4_pct": float(100 * over4.sum() / mh["covered"].sum()),
        "fig4_h_alt": float(over4_alt.sum()),
        "fig3_h": float(mh["fig3"].sum()),
        "fig3_h_alt": float(minergie_hours(temp_alt, lim_r["fig3"], FIG3_SEASON).sum()),
        "warm_nights": int(warm.sum()),
        "warm_run": int(warm.groupby((~warm).cumsum()).sum().max()),
        "tau": mem["tau"],
        "b": mem["b"],
        "mem_r": mem["r"],
        "tip": mem["tip"],
        "tip_alt": mem["tip_alt"],
        "night_pct": float(100 * (offered["shed"] / offered["avail"]).median()),
        "avail_r": float(nc["shed"].corr(nc["avail"])),
        "vent_r": float(nc["shed"].corr(nc["vent"])),
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
        "daily": div(fig_daily(di, do, lim), "daily"),
        # the Minergie limit is per calendar year, so the running total restarts each year
        "hours": div(fig_hours(mh, over4.groupby(over4.index.year).cumsum(),
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
