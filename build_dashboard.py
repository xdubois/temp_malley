#!/usr/bin/env python3
"""Build an interactive temperature dashboard for the Malley apartment.

Parses the 15-minute indoor sensor CSV, fetches outdoor weather for
Malley (open-meteo), computes daily aggregates and renders a single
self-contained HTML file (Plotly inlined -> opens offline in any browser).

Run:   uv run build_dashboard.py
Outdoor weather is cached locally (.outdoor_cache.json) after the first run.
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import requests
from plotly.offline import get_plotlyjs

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_15MIN = os.path.join(HERE, "data", "sensor_15min.csv")
CSV_1MIN = os.path.join(HERE, "data", "sensor_1min.csv")
OUT_HTML = os.path.join(HERE, "temperature_dashboard.html")
OUTDOOR_CACHE = os.path.join(HERE, ".outdoor_cache.json")

# Malley, Switzerland
LAT, LON, TZ = 46.53, 6.59, "Europe/Zurich"

# Ignore readings before this date (earlier rows are from a different location).
# Override per run with --from=YYYY-MM-DD.
START_DATE = "2026-04-28"

MIN_READINGS_DAY = 48  # drop days with fewer 15-min readings from daily charts

# The sensor sits at the entrance, ~0.8 °C cooler than the rest of the flat.
# When APPLY_OFFSET is True, SENSOR_OFFSET is added to every indoor reading so the
# WHOLE dashboard shows the living-space estimate; when False it shows the raw
# entrance-sensor reading everywhere. A constant offset only shifts absolute
# temperatures — swings, damping, correlation and lag are unaffected.
APPLY_OFFSET = False
SENSOR_OFFSET = 0.8
TEMP_REF = (f"living-space estimate (sensor +{SENSOR_OFFSET:g} °C)"
            if APPLY_OFFSET else "entrance sensor (measured)")

# Minergie summer-comfort design target: at most ~100 h/year above 26.5 °C.
COMFORT_T = 26.5
WARM_T = 28.0
MINERGIE_BUDGET_H = 100

# palette
C_IN = "#e8633a"      # indoor temperature (warm)
C_IN_FILL = "rgba(232,99,58,0.15)"
C_OUT = "#2f7ec4"     # outdoor temperature (cool)
C_SUN = "#f2b134"     # sun / radiation / light
GRID = "#e6e6e6"
INK = "#2b2b2b"

CONFIG = {"responsive": True, "displaylogo": False,
          "modeBarButtonsToRemove": ["lasso2d", "select2d"]}


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def load_indoor(path: str, start: str | None = None) -> pd.DataFrame:
    """Read the European-formatted sensor CSV onto a complete 15-min grid.

    Values use comma decimals ('25,1'); missing 15-min slots become NaN so the
    plotted line breaks across offline periods instead of jumping over them.
    Rows before ``start`` are dropped.
    """
    df = pd.read_csv(path, decimal=",")
    df.columns = ["date", "temp", "hum", "dpt", "vpd", "abshum", "light"]
    df["date"] = pd.to_datetime(df["date"], format="%d/%m/%Y %H:%M")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    if start:
        df = df[df.index >= start]
    full = pd.date_range(df.index.min(), df.index.max(), freq="15min")
    df = df.reindex(full)
    if APPLY_OFFSET:
        df["temp"] = df["temp"] + SENSOR_OFFSET  # estimate living-space temperature
    return df


def fetch_outdoor(start: str, end: str) -> pd.DataFrame:
    """Hourly outdoor temperature + solar radiation for Malley.

    Stitches the ERA5 archive (older days) with the forecast API's past_days
    (recent days the archive lags ~5 days behind). Cached locally.
    """
    if os.path.exists(OUTDOOR_CACHE):
        try:
            c = json.load(open(OUTDOOR_CACHE))
            if c.get("start") == start and c.get("end") == end:
                df = pd.DataFrame(c["rows"])
                df["time"] = pd.to_datetime(df["time"])
                return df.set_index("time")
        except (json.JSONDecodeError, KeyError):
            pass

    def grab(base, **params):
        params.update(latitude=LAT, longitude=LON, timezone=TZ,
                      hourly="temperature_2m,shortwave_radiation")
        h = requests.get(base, params=params, timeout=60).json()["hourly"]
        return pd.DataFrame(h).rename(
            columns={"temperature_2m": "temp", "shortwave_radiation": "rad"})

    arch = grab("https://archive-api.open-meteo.com/v1/archive",
                start_date=start, end_date=end)
    merged = arch.set_index("time")
    try:  # fill recent tail the archive lags behind
        fc = grab("https://api.open-meteo.com/v1/forecast",
                  past_days=92, forecast_days=1).set_index("time")
        fc = fc[(fc.index >= start) & (fc.index <= f"{end} 23:59")]
        merged = fc.combine_first(merged)
    except Exception as e:  # noqa: BLE001
        print(f"  (forecast tail fetch failed: {e})", file=sys.stderr)

    merged = merged.sort_index()
    out = merged.reset_index()
    out["time"] = out["time"].astype(str)
    json.dump({"start": start, "end": end, "rows": out.to_dict("records")},
              open(OUTDOOR_CACHE, "w"))
    merged.index = pd.to_datetime(merged.index)
    return merged


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def daily_indoor(df: pd.DataFrame) -> pd.DataFrame:
    g = df["temp"].resample("D").agg(["min", "max", "mean", "count"])
    g = g[g["count"] >= MIN_READINGS_DAY]
    g["amp"] = g["max"] - g["min"]
    g["lmax"] = df["light"].resample("D").max().reindex(g.index)
    return g


def daily_outdoor(out: pd.DataFrame) -> pd.DataFrame:
    g = out["temp"].resample("D").agg(["min", "max", "mean"])
    g["amp"] = g["max"] - g["min"]
    g["rad_mean"] = out["rad"].resample("D").mean()
    g["rad_peak"] = out["rad"].resample("D").max()
    return g


def heatmap_matrix(df: pd.DataFrame):
    t = df.dropna(subset=["temp"]).copy()
    t["day"] = t.index.normalize()
    t["hour"] = t.index.hour
    piv = t.pivot_table(index="day", columns="hour", values="temp", aggfunc="mean")
    days = pd.date_range(df.index.min().normalize(), df.index.max().normalize(), freq="D")
    piv = piv.reindex(index=days, columns=range(24))
    return [d.strftime("%Y-%m-%d") for d in piv.index], list(range(24)), piv.values.tolist()


def thermal_lag(df: pd.DataFrame, out: pd.DataFrame):
    indoor_h = df["temp"].resample("h").mean()
    j = pd.concat([indoor_h.rename("in"), out["temp"].rename("out")], axis=1, sort=True)
    best_lag, best_r = 0, -2.0
    for lag in range(13):
        r = j["in"].corr(j["out"].shift(lag))
        if pd.notna(r) and r > best_r:
            best_r, best_lag = float(r), lag
    return best_lag, best_r


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


def fig_overview(df, out):
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=out.index, y=out["temp"], name="Outdoor", mode="lines",
        line=dict(color=C_OUT, width=1.3), connectgaps=False,
        hovertemplate="%{y:.1f} °C<extra>Outdoor</extra>"))
    fig.add_trace(go.Scatter(
        x=df.index, y=df["temp"], name="Indoor", mode="lines",
        line=dict(color=C_IN, width=1.6), connectgaps=False,
        hovertemplate="%{y:.1f} °C<extra>Indoor</extra>"))
    base_layout(fig, 460)
    fig.update_yaxes(title_text="Temperature (°C)")
    fig.update_xaxes(rangeslider=dict(visible=True), rangeslider_thickness=0.06)
    return fig


def fig_seasonal(di, do):
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=di.index, y=di["max"], name="Indoor daily max",
                             mode="lines", line=dict(width=0), showlegend=False,
                             hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=di.index, y=di["min"], name="Indoor min–max range",
                             mode="lines", line=dict(width=0), fill="tonexty",
                             fillcolor=C_IN_FILL,
                             hovertemplate="min %{y:.1f} °C<extra></extra>"))
    fig.add_trace(go.Scatter(x=di.index, y=di["mean"], name="Indoor daily mean",
                             mode="lines+markers", line=dict(color=C_IN, width=2.5),
                             marker=dict(size=4),
                             hovertemplate="%{y:.1f} °C<extra>Indoor mean</extra>"))
    fig.add_trace(go.Scatter(x=do.index, y=do["mean"], name="Outdoor daily mean",
                             mode="lines", line=dict(color=C_OUT, width=2, dash="dot"),
                             hovertemplate="%{y:.1f} °C<extra>Outdoor mean</extra>"))
    base_layout(fig, 440)
    fig.update_yaxes(title_text="Temperature (°C)")
    return fig


def fig_heatmap(days, hours, z):
    fig = go.Figure(go.Heatmap(
        z=z, x=hours, y=days, colorscale="RdYlBu_r",
        colorbar=dict(title="°C"), hoverongaps=False,
        hovertemplate="%{y}  %{x}:00<br>%{z:.1f} °C<extra></extra>"))
    base_layout(fig, max(520, len(days) * 5))
    fig.update_xaxes(title_text="Hour of day", dtick=2, side="top")
    fig.update_yaxes(title_text="", autorange="reversed")
    return fig


def fig_amplitude(di, do):
    fig = go.Figure()
    fig.add_trace(go.Bar(x=di.index, y=di["amp"], name="Indoor day↔night swing",
                         marker_color=C_IN,
                         hovertemplate="%{y:.1f} °C<extra>Indoor swing</extra>"))
    fig.add_trace(go.Scatter(x=do.index, y=do["amp"], name="Outdoor swing",
                             mode="lines", line=dict(color=C_OUT, width=2, dash="dot"),
                             hovertemplate="%{y:.1f} °C<extra>Outdoor swing</extra>"))
    base_layout(fig, 420)
    fig.update_yaxes(title_text="Daily max − min (°C)")
    return fig


def fig_sun(di, do):
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=do.index, y=do["rad_mean"], name="Outdoor sun (mean radiation)",
        mode="lines", line=dict(width=0), fill="tozeroy",
        fillcolor="rgba(242,177,52,0.25)", yaxis="y2",
        hovertemplate="%{y:.0f} W/m²<extra>Sun</extra>"))
    fig.add_trace(go.Scatter(
        x=di.index, y=di["mean"], name="Indoor daily mean temp",
        mode="lines", line=dict(color=C_IN, width=2.5),
        hovertemplate="%{y:.1f} °C<extra>Indoor</extra>"))
    fig.add_trace(go.Scatter(
        x=di.index, y=di["lmax"], name="Indoor light (daily max)",
        mode="lines", line=dict(color=C_SUN, width=1.6, dash="dot"), yaxis="y3",
        hovertemplate="%{y:.0f}<extra>Light index</extra>"))
    base_layout(fig, 440)
    fig.update_layout(
        yaxis=dict(title="Indoor temp (°C)"),
        yaxis2=dict(title="Sun (W/m²)", overlaying="y", side="right",
                    showgrid=False),
        yaxis3=dict(overlaying="y", side="right", position=0.97,
                    showgrid=False, showticklabels=False, range=[0, 22]),
    )
    return fig


def fig_comfort(di):
    """Daily indoor temperature vs comfort thresholds, emphasising the rising
    overnight floor (daily minimum). Uses whichever reference APPLY_OFFSET sets."""
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
    base_layout(fig, 440)
    fig.update_yaxes(title_text=f"Indoor temp — {TEMP_REF} (°C)")
    fig.add_hline(y=COMFORT_T, line=dict(color="#e8a33a", width=1.6, dash="dash"),
                  annotation_text="26.5 °C — Minergie comfort",
                  annotation_position="bottom left")
    fig.add_hline(y=WARM_T, line=dict(color="#d6453a", width=1.6, dash="dash"),
                  annotation_text="28 °C — warm", annotation_position="top left")
    return fig


def fig_budget(df):
    """Running total of hours above 26.5 °C against the Minergie ~100 h budget."""
    s = df["temp"].dropna()
    H = 0.25  # hours per 15-min reading
    cum = ((s > COMFORT_T) * H).cumsum()
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=cum.index, y=cum.values, name=f"Hours over 26.5 °C — {TEMP_REF}",
        mode="lines", line=dict(color=C_IN, width=2.6), fill="tozeroy",
        fillcolor="rgba(232,99,58,0.12)",
        hovertemplate="%{y:.0f} h<extra></extra>"))
    base_layout(fig, 420)
    fig.update_yaxes(title_text="Cumulative hours above 26.5 °C")
    fig.add_hline(y=MINERGIE_BUDGET_H, line=dict(color=C_OUT, width=1.8, dash="dash"),
                  annotation_text=f"Minergie design budget ≈ {MINERGIE_BUDGET_H} h / year",
                  annotation_position="top right")
    return fig


# --------------------------------------------------------------------------- #
# HTML assembly
# --------------------------------------------------------------------------- #
def div(fig, name):
    return pio.to_html(fig, full_html=False, include_plotlyjs=False,
                       div_id=name, config=CONFIG)


def render(summary, figs) -> str:
    s = summary
    monthly = " · ".join(f"{m} {v:.1f}°C" for m, v in s["monthly"].items())
    off_note = (
        f"Readings have +{SENSOR_OFFSET:g} °C added to estimate living-space "
        f"temperature (the entrance sensor sits ~{SENSOR_OFFSET:g} °C cooler)."
        if APPLY_OFFSET else
        f"These are the raw entrance-sensor readings; the rest of the flat runs "
        f"~{SENSOR_OFFSET:g} °C warmer.")
    cards = [
        ("Period", s["span"], f"{s['n_days']} days with data"),
        ("Indoor range", f"{s['t_min']:.1f} – {s['t_max']:.1f} °C",
         f"mean {s['t_mean']:.1f} °C"),
        ("Peak indoor", f"{s['t_max']:.1f} °C", s["t_max_when"]),
        ("Hours over 26.5 °C", f"{s['comfort_h']:.0f} h",
         f"{TEMP_REF} · {s['n_days']} days · Minergie target ≈100 h/yr"),
        ("Monthly mean", monthly, "indoor warming through the season"),
        ("Day↔night swing", f"{s['amp_in']:.1f} °C indoor",
         f"vs {s['amp_out']:.1f} °C outdoors — building damps {s['buffer']:.1f}×"),
        ("Outdoor coupling", f"r = {s['corr']:.2f}",
         f"indoor lags outdoor by ~{s['lag_h']} h (r={s['lag_r']:.2f})"),
    ]
    card_html = "\n".join(
        f'<div class="card"><div class="k">{k}</div>'
        f'<div class="v">{v}</div><div class="s">{sub}</div></div>'
        for k, v, sub in cards)

    sections = [
        ("Indoor vs outdoor temperature",
         "Every 15-min indoor reading against hourly outdoor temperature for "
         "Malley. Drag on the chart or use the slider to zoom into any "
         "stretch.",
         "overview"),
        ("Seasonal warming trend",
         "One point per day: the shaded band is the indoor min→max, the solid "
         "line the daily mean, dotted is the outdoor mean. Shows the apartment "
         "slowly heating as summer arrives.",
         "seasonal"),
        ("Day × hour heatmap",
         "Each row is a day, each column an hour. Colour = average indoor "
         "temperature. Reveals when in the day the flat heats up and whether "
         "nights stay warm. Blank cells = missing readings.",
         "heatmap"),
        ("Daily day↔night swing",
         "How many degrees the apartment moves between its daily low and high "
         "(bars), against the outdoor swing (dotted). A small indoor swing means "
         "the building buffers heat well.",
         "amplitude"),
        ("Sun & light vs temperature",
         "Outdoor solar radiation (filled), indoor temperature (line) and the "
         "indoor light sensor (dotted) per day — to see solar gain driving the "
         "indoor temperature.",
         "sun"),
        ("Comfort: is the flat staying cool enough?",
         f"{off_note} Dashed lines mark 26.5 °C (Minergie summer-comfort target) "
         "and 28 °C (warm). Watch the overnight floor (daily minimum) climb — "
         "that's heat the building can't shed at night.",
         "comfort"),
        ("Hours above 26.5 °C vs the Minergie budget",
         "Minergie certifies a flat to spend at most ~100 h per year above "
         "26.5 °C (blue line). This is the running total over the period. Crossing "
         "the blue line early means the summer-comfort budget is already spent.",
         "budget"),
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
  <div class="sub">Indoor: {TEMP_REF}, 15-min · {s['span']} · outdoor: Malley (open-meteo)</div>
</header>
<div class="cards">{card_html}</div>
{sec_html}
<footer>
  Indoor data: 15-min export ({s['n_indoor']:,} readings) from an entrance sensor,
  shown as {TEMP_REF}. Outdoor: open-meteo ERA5 + forecast for {LAT}, {LON}.
  Minergie summer-comfort target ≈ {MINERGIE_BUDGET_H} h/year above {COMFORT_T} °C.
  Days with &lt;{MIN_READINGS_DAY} readings are excluded from daily charts.
  Rebuild with <code>uv run build_dashboard.py</code>.
</footer>
</body></html>"""


def main():
    start_date = START_DATE
    for a in sys.argv[1:]:
        if a.startswith("--from="):
            start_date = a.split("=", 1)[1]

    print(f"Loading indoor 15-min data (from {start_date})…", file=sys.stderr)
    df = load_indoor(CSV_15MIN, start_date)
    start = df.index.min().strftime("%Y-%m-%d")
    end = df.index.max().strftime("%Y-%m-%d")
    n_indoor = int(df["temp"].notna().sum())
    print(f"  {n_indoor} readings, {start} → {end}", file=sys.stderr)

    print("Fetching outdoor weather (Malley)…", file=sys.stderr)
    out = fetch_outdoor(start, end)
    print(f"  {len(out)} hourly rows", file=sys.stderr)

    di = daily_indoor(df)
    do = daily_outdoor(out)
    hm_days, hm_hours, hm_z = heatmap_matrix(df)
    lag, lag_r = thermal_lag(df, out)

    monthly = (di["mean"].groupby(di.index.strftime("%B")).mean())
    month_order = ["March", "April", "May", "June"]
    monthly = {m: monthly[m] for m in month_order if m in monthly.index}

    corr = di["mean"].reindex(do.index).corr(do["mean"])
    tmax = df["temp"].idxmax()

    comfort_h = float((df["temp"].dropna() > COMFORT_T).sum() * 0.25)

    summary = {
        "span": f"{df.index.min():%d %b} → {df.index.max():%d %b %Y}",
        "n_indoor": n_indoor,
        "n_days": int(len(di)),
        "t_min": float(df["temp"].min()),
        "t_max": float(df["temp"].max()),
        "t_mean": float(df["temp"].mean()),
        "t_max_when": f"{tmax:%a %d %b, %H:%M}",
        "monthly": monthly,
        "amp_in": float(di["amp"].mean()),
        "amp_out": float(do["amp"].mean()),
        "buffer": float(do["amp"].mean() / di["amp"].mean()),
        "corr": float(corr),
        "lag_h": lag,
        "lag_r": lag_r,
        "comfort_h": comfort_h,
    }
    print("Summary:\n" + json.dumps(
        {k: (round(v, 2) if isinstance(v, float) else v) for k, v in summary.items()
         if k != "monthly"}, indent=2, default=str), file=sys.stderr)

    figs = {
        "overview": div(fig_overview(df, out), "overview"),
        "seasonal": div(fig_seasonal(di, do), "seasonal"),
        "heatmap": div(fig_heatmap(hm_days, hm_hours, hm_z), "heatmap"),
        "amplitude": div(fig_amplitude(di, do), "amplitude"),
        "sun": div(fig_sun(di, do), "sun"),
        "comfort": div(fig_comfort(di), "comfort"),
        "budget": div(fig_budget(df), "budget"),
    }
    html = render(summary, figs)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  wrote {OUT_HTML} ({os.path.getsize(OUT_HTML)/1e6:.1f} MB)", file=sys.stderr)


if __name__ == "__main__":
    main()
