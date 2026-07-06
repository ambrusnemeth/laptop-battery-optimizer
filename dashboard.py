import streamlit as st
import sqlite3
import datetime
import plotly.graph_objects as go
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "battery.db"
HEL_TZ = ZoneInfo("Europe/Helsinki")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def get_real_battery():
    try:
        with open('/sys/class/power_supply/BAT0/capacity', 'r') as f:
            return int(f.read().strip())
    except:
        return 0

def get_is_charging():
    try:
        with open('/sys/class/power_supply/BAT0/status', 'r') as f:
            return f.read().strip() == "Charging"
    except:
        return False

st.set_page_config(page_title="Laptop Energy Optimizer", layout="wide")

# --- Sidebar & date selection ---
st.sidebar.header("Navigation")
selected_date = st.sidebar.date_input("Select Date", datetime.date.today())
is_today = (selected_date == datetime.date.today())
st.title(f"⚡ Energy Dashboard: {selected_date}")

# --- Live metrics (today only) ---
if is_today:
    col1, col2 = st.columns(2)
    with col1:
        st.metric("Live Battery", f"{get_real_battery()}%")
    with col2:
        st.metric("Plug Status", "CHARGING" if get_is_charging() else "DISCHARGING")
    st.divider()

# --- Query database for the selected day ---
conn = get_db()
day_str = selected_date.isoformat()

prices_rows = conn.execute(
    "SELECT interval_start, price FROM prices WHERE date(interval_start) = ? ORDER BY interval_start",
    (day_str,)
).fetchall()

schedule_rows = conn.execute(
    "SELECT interval_start, decision, soc_forecast FROM schedule WHERE date(interval_start) = ? ORDER BY interval_start",
    (day_str,)
).fetchall()

soc_rows = conn.execute(
    "SELECT timestamp, soc FROM soc_log WHERE date(timestamp) = ? ORDER BY timestamp",
    (day_str,)
).fetchall()

# Get plan creation (switch) time for today, if any
switch_time = None
if is_today:
    row = conn.execute(
        "SELECT start_time FROM plan_runs WHERE date(start_time) = ? ORDER BY start_time DESC LIMIT 1",
        (day_str,)
    ).fetchone()
    if row:
        # Attach Helsinki timezone
        switch_time = datetime.datetime.fromisoformat(row[0]).replace(tzinfo=HEL_TZ)

conn.close()

# --- Build full‑day arrays (96 intervals) ---
num_intervals = 96
prices = [None] * num_intervals
decisions = [0] * num_intervals
forecast_soc = [None] * num_intervals

day_start = datetime.datetime.combine(selected_date, datetime.time(0, 0), tzinfo=HEL_TZ)

for row in schedule_rows:
    ts = datetime.datetime.fromisoformat(row[0]).replace(tzinfo=HEL_TZ)   # ← make aware!
    i = int((ts - day_start).total_seconds() // 900)
    if 0 <= i < num_intervals:
        decisions[i] = row[1]
        forecast_soc[i] = row[2] if row[2] is not None else None

for row in prices_rows:
    ts = datetime.datetime.fromisoformat(row[0]).replace(tzinfo=HEL_TZ)   # ← make aware!
    i = int((ts - day_start).total_seconds() // 900)
    if 0 <= i < num_intervals:
        prices[i] = row[1]

# SoC history – also aware
hist_dt = [datetime.datetime.fromisoformat(row[0]).replace(tzinfo=HEL_TZ) for row in soc_rows]
hist_soc = [row[1] for row in soc_rows]

# --- Plotting ---
if any(p is not None for p in prices) or any(d for d in decisions):
    plot_times = [day_start + datetime.timedelta(minutes=i * 15) for i in range(num_intervals)]

    fig = go.Figure()

    # Price line
    fig.add_trace(go.Scatter(
        x=plot_times,
        y=prices,
        name="Price (EUR/MWh)",
        mode='lines',
        line=dict(color='#38BDF8', width=3, shape='hv'),
        yaxis="y",
        hovertemplate='%{x|%H:%M}: <b>%{y:.2f} EUR/MWh</b><extra></extra>'
    ))

    # Actual SoC line
    fig.add_trace(go.Scatter(
        x=hist_dt,
        y=hist_soc,
        name="Actual SoC (%)",
        mode='lines',
        line=dict(color='#F59E0B', width=3),
        yaxis="y2",
        hovertemplate='SoC: <b>%{y:.1f}%</b><extra></extra>'
    ))

    # Dummy legend entry for charging blocks
    fig.add_trace(go.Scatter(
        x=[None], y=[None],
        mode='markers',
        marker=dict(size=10, color='rgba(16, 185, 129, 0.4)', symbol='square'),
        name="Planned Charging",
        showlegend=True
    ))

    # Green rectangles where charging is planned
    for i in range(num_intervals):
        if decisions[i] > 0:
            block_start = plot_times[i]
            block_end = block_start + datetime.timedelta(minutes=15)
            fig.add_vrect(
                x0=block_start, x1=block_end,
                fillcolor="#10B981",
                opacity=0.12,
                layer="below",
                line_width=0
            )

    # Dashed vertical line when the plan was created (today only)
    if is_today and switch_time:
        fig.add_vline(
            x=switch_time,
            line_dash="dash",
            line_color="#10B981",
            line_width=2,
            opacity=1.0
            # NO annotation_text here – we'll add it manually
        )
        fig.add_annotation(
            x=switch_time,
            y=1,                    # top of the plot (paper coordinates)
            yref="paper",
            text="New plan",
            showarrow=False,
            xanchor="left",
            font=dict(color="#10B981", size=12)
        )

    # Layout
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0F172A",
        plot_bgcolor="#0F172A",
        height=650,
        margin=dict(l=50, r=50, t=80, b=50),
        legend=dict(orientation="h", yanchor="bottom", y=1.05, xanchor="right", x=1),
        hovermode="x unified",
        yaxis=dict(
            title=dict(text="Price (EUR/MWh)", font=dict(color="#38BDF8")),
            tickfont=dict(color="#38BDF8"),
            gridcolor="#1E293B",
            zeroline=False
        ),
        yaxis2=dict(
            title=dict(text="SoC %", font=dict(color="#F59E0B")),
            tickfont=dict(color="#F59E0B"),
            overlaying="y",
            side="right",
            range=[0, 105],
            showgrid=False,
            zeroline=False
        ),
        xaxis=dict(
            range=[day_start, day_start + datetime.timedelta(days=1)],
            showgrid=True,
            gridcolor="#1E293B",
            tickformat="%H:%M",
            dtick=3600000 * 3
        )
    )

    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("No data available for this date.")
