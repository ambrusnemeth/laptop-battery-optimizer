import streamlit as st
import sqlite3
import datetime
import plotly.graph_objects as go
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "battery.db"
HEL_TZ = ZoneInfo("Europe/Helsinki")

# ── helpers ──────────────────────────────────────────────
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

# ── page config ──────────────────────────────────────────
st.set_page_config(page_title="Laptop Energy Optimizer", layout="wide")

# ── sidebar & date selection ─────────────────────────────
st.sidebar.header("Navigation")
selected_date = st.sidebar.date_input("Select Date", datetime.date.today())
is_today = (selected_date == datetime.date.today())
st.title(f"⚡ Energy Dashboard: {selected_date}")

# ── live metrics (today only) ────────────────────────────
if is_today:
    col1, col2 = st.columns(2)
    with col1:
        st.metric("Live Battery", f"{get_real_battery()}%")
    with col2:
        st.metric("Plug Status", "CHARGING" if get_is_charging() else "DISCHARGING")
    st.divider()

# ── query database ───────────────────────────────────────
conn = get_db()
day_str = selected_date.isoformat()

# Main plot data
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

# Plan creation time
switch_time = None
if is_today:
    row = conn.execute(
        "SELECT start_time FROM plan_runs WHERE date(start_time) = ? ORDER BY start_time DESC LIMIT 1",
        (day_str,)
    ).fetchone()
    if row:
        switch_time = datetime.datetime.fromisoformat(row[0]).replace(tzinfo=HEL_TZ)

# Dynamic rates history (all time)
rate_rows = conn.execute(
    "SELECT timestamp, charge_rate, discharge_rate FROM rate_log ORDER BY timestamp"
).fetchall()

# Battery capacity history (all time)
cap_rows = conn.execute(
    "SELECT timestamp, capacity FROM capacity_log ORDER BY timestamp"
).fetchall()

# SoC‑bucket rate history (NEW)
bucket_rows = conn.execute(
    "SELECT timestamp, bucket, charge_rate, discharge_rate FROM bucket_rate_log ORDER BY timestamp, bucket"
).fetchall()

conn.close()

# ── build full‑day arrays (288 intervals of 5 minutes) ──
num_intervals = 288
prices = [None] * num_intervals
decisions = [0] * num_intervals

day_start = datetime.datetime.combine(selected_date, datetime.time(0, 0), tzinfo=HEL_TZ)

for row in schedule_rows:
    ts = datetime.datetime.fromisoformat(row[0]).replace(tzinfo=HEL_TZ)
    i = int((ts - day_start).total_seconds() // 300)   # 300 seconds = 5 min
    if 0 <= i < num_intervals:
        decisions[i] = row[1]

for row in prices_rows:
    ts = datetime.datetime.fromisoformat(row[0]).replace(tzinfo=HEL_TZ)
    i = int((ts - day_start).total_seconds() // 300)
    if 0 <= i < num_intervals:
        prices[i] = row[1]

# actual SoC history
hist_dt = [datetime.datetime.fromisoformat(row[0]).replace(tzinfo=HEL_TZ) for row in soc_rows]
hist_soc = [row[1] for row in soc_rows]

# ── main plot ────────────────────────────────────────────
if any(p is not None for p in prices) or any(d for d in decisions):
    plot_times = [day_start + datetime.timedelta(minutes=i * 5) for i in range(num_intervals)]

    fig = go.Figure()

    # 1) Price line
    fig.add_trace(go.Scatter(
        x=plot_times,
        y=prices,
        name="Price (EUR/MWh)",
        mode='lines',
        line=dict(color='#38BDF8', width=3, shape='hv'),
        yaxis="y",
        hovertemplate='%{x|%H:%M}: <b>%{y:.2f} EUR/MWh</b><extra></extra>'
    ))

    # 2) Actual SoC line
    fig.add_trace(go.Scatter(
        x=hist_dt,
        y=hist_soc,
        name="Actual SoC (%)",
        mode='lines',
        line=dict(color='#F59E0B', width=3),
        yaxis="y2",
        hovertemplate='SoC: <b>%{y:.1f}%</b><extra></extra>'
    ))

    # 3) Dummy legend entry for planned charging
    fig.add_trace(go.Scatter(
        x=[None], y=[None],
        mode='markers',
        marker=dict(size=10, color='rgba(16, 185, 129, 0.4)', symbol='square'),
        name="Planned Charging",
        showlegend=True
    ))

    # 4) Green rectangles where charging is planned (5‑min width)
    for i in range(num_intervals):
        if decisions[i] > 0:
            block_start = plot_times[i]
            block_end = block_start + datetime.timedelta(minutes=5)
            fig.add_vrect(
                x0=block_start, x1=block_end,
                fillcolor="#10B981",
                opacity=0.12,
                layer="below",
                line_width=0
            )

    # 5) Dashed vertical line for plan creation (today only)
    if is_today and switch_time:
        fig.add_vline(
            x=switch_time,
            line_dash="dash",
            line_color="#10B981",
            line_width=2,
            opacity=1.0
        )
        fig.add_annotation(
            x=switch_time,
            y=1,
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
            dtick=3600000 * 2   # tick every 2 hours
        )
    )

    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("No data available for this date.")

# ── Dynamic rates & capacity charts (log time) ───────────
st.divider()
now_for_log = datetime.datetime.now(HEL_TZ)

col1, col2 = st.columns(2)

with col1:
    st.subheader("SoC‑Bucket Charge & Discharge Rates")
    if bucket_rows:
        fig_bucket = go.Figure()
        # One trace per bucket – colour coded
        colors = ['#EF4444', '#F97316', '#EAB308', '#22C55E', '#3B82F6']
        bucket_labels = ['0‑30%', '30‑50%', '50‑70%', '70‑90%', '90‑100%']
    
        now_for_log = datetime.datetime.now(HEL_TZ)
        # Separate data per bucket
        for b in range(5):
            b_data = [row for row in bucket_rows if row[1] == b]
            if not b_data:
                continue
            dates = [datetime.datetime.fromisoformat(row[0]).replace(tzinfo=HEL_TZ) for row in b_data]
            days_ago = [(now_for_log - d).total_seconds() / 86400 + 0.001 for d in dates]
            ch = [row[2] for row in b_data]
            dch = [row[3] for row in b_data]
            fig_bucket.add_trace(go.Scatter(
                x=days_ago, y=ch,
                mode='lines+markers',
                line=dict(color=colors[b], width=2),
                marker=dict(size=4),
                name=f"Charge {bucket_labels[b]}"
            ))
            fig_bucket.add_trace(go.Scatter(
                x=days_ago, y=dch,
                mode='lines+markers',
                line=dict(color=colors[b], width=2, dash='dot'),
                marker=dict(size=4),
                name=f"Discharge {bucket_labels[b]}"
            ))
        fig_bucket.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0F172A",
            plot_bgcolor="#0F172A",
            height=500,
            margin=dict(l=50, r=30, t=30, b=50),
            xaxis=dict(title="Days ago", type="log", tickformat=".1f", gridcolor="#1E293B"),
            yaxis=dict(title="% per 5 min", gridcolor="#1E293B"),
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02)
        )
        st.plotly_chart(fig_bucket, use_container_width=True)
    else:
        st.info("Bucket rate data will appear after the next planner run.")


with col2:
    st.subheader("Battery Capacity")
    if cap_rows:
        dates_aware = [datetime.datetime.fromisoformat(r[0]).replace(tzinfo=HEL_TZ) for r in cap_rows]
        days_ago = [(now_for_log - d).total_seconds() / 86400 + 0.001 for d in dates_aware]
        cap_vals = [r[1] for r in cap_rows]

        # Try to detect unit from the kernel file name (optional)
        unit = "µAh"   # default; you can change to "µWh" if your system uses energy_full
        fig_cap = go.Figure()
        fig_cap.add_trace(go.Scatter(
            x=days_ago, y=cap_vals,
            mode='lines+markers',
            line=dict(color='#38BDF8', width=2),
            marker=dict(size=4),
            name="Capacity"
        ))
        fig_cap.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0F172A",
            plot_bgcolor="#0F172A",
            height=350,
            margin=dict(l=50, r=30, t=50, b=50),
            xaxis=dict(title="Days ago", type="log", tickformat=".1f", gridcolor="#1E293B"),
            yaxis=dict(title="Capacity (%)", gridcolor="#1E293B", range=[0, 105]),
            hovermode="x unified"
        )
        st.plotly_chart(fig_cap, use_container_width=True)
    else:
        st.info("Capacity data will appear after the next planner run.")
