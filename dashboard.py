import streamlit as st
import json
import datetime
import plotly.graph_objects as go
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "dashboard_data.json"
HISTORY_PATH = BASE_DIR / "history.json"
ARCHIVE_DIR = BASE_DIR / "archive"
ARCHIVE_DIR.mkdir(exist_ok=True)

st.set_page_config(page_title="Laptop Energy Optimizer", layout="wide")

HEL_TZ = ZoneInfo("Europe/Helsinki")

def get_real_battery():
    try:
        with open('/sys/class/power_supply/BAT0/capacity', 'r') as f:
            return int(f.read().strip())
    except: return 0

def get_is_charging():
    try:
        with open('/sys/class/power_supply/BAT0/status', 'r') as f:
            return f.read().strip() == "Charging"
    except: return False

st.sidebar.header("Navigation")
selected_date = st.sidebar.date_input("Select Date", datetime.date.today())
is_today = (selected_date == datetime.date.today())
st.title(f"⚡ Energy Dashboard: {selected_date}")

if is_today:
    col1, col2 = st.columns(2)
    with col1:
        st.metric("Live Battery", f"{get_real_battery()}%")
    with col2:
        st.metric("Plug Status", "CHARGING" if get_is_charging() else "DISCHARGING")
    st.divider()

plot_data = None
hist_dt = []
hist_soc = []
plan_start = None

if is_today:
    if DATA_PATH.exists():
        try:
            with open(DATA_PATH, 'r') as f:
                plot_data = json.load(f)
            if HISTORY_PATH.exists():
                with open(HISTORY_PATH, 'r') as f:
                    raw_hist = json.load(f)
                    hist_dt = [datetime.datetime.fromisoformat(e['iso_time']) for e in raw_hist]
                    hist_soc = [e['soc'] for e in raw_hist]
            if plot_data and "start_time" in plot_data:
                plan_start = datetime.datetime.fromisoformat(plot_data["start_time"])
        except Exception as e:
            st.error(f"Error reading live data: {e}")
else:
    archive_file = ARCHIVE_DIR / f"{selected_date}.json"
    if archive_file.exists():
        try:
            with open(archive_file, 'r') as f:
                archived = json.load(f)
                plot_data = {"prices": archived['prices'], "plan": archived['plan']}
                hist_dt = [datetime.datetime.fromisoformat(e['iso_time']) for e in archived['actual_soc']]
                hist_soc = [e['soc'] for e in archived['actual_soc']]
                # For archived days, assume the plan covers the full day (midnight to midnight)
                plan_start = datetime.datetime.combine(selected_date, datetime.time(0, 0), tzinfo=HEL_TZ)
        except Exception as e:
            st.error(f"Error reading archive: {e}")
    else:
        st.warning(f"No archive file found for {selected_date}")

if plot_data and plan_start:
    prices = plot_data['prices']
    plan = plot_data['plan']
    n = len(prices)
    plot_times = [plan_start + datetime.timedelta(minutes=i * 15) for i in range(n)]

    fig = go.Figure()

    # Price trace
    fig.add_trace(go.Scatter(
        x=plot_times,
        y=prices,
        name="Price (EUR/MWh)",
        mode='lines',
        line=dict(color='#38BDF8', width=3, shape='hv'),
        yaxis="y",
        hovertemplate='%{x|%H:%M}: <b>%{y:.2f} EUR/MWh</b><extra></extra>'
    ))

    # Actual SoC trace
    fig.add_trace(go.Scatter(
        x=hist_dt,
        y=hist_soc,
        name="Actual SoC (%)",
        mode='lines',
        line=dict(color='#F59E0B', width=3),
        yaxis="y2",
        hovertemplate='SoC: <b>%{y:.1f}%</b><extra></extra>'
    ))

    # Legend dummy for charging blocks
    fig.add_trace(go.Scatter(
        x=[None], y=[None],
        mode='markers',
        marker=dict(size=10, color='rgba(16, 185, 129, 0.4)', symbol='square'),
        name="Planned Charging",
        showlegend=True
    ))

    # Green vrects for planned charging intervals
    for i in range(n):
        if plan[i] > 0:
            block_start = plot_times[i]
            block_end = block_start + datetime.timedelta(minutes=15)
            fig.add_vrect(
                x0=block_start, x1=block_end,
                fillcolor="#10B981",
                opacity=0.12,
                layer="below",
                line_width=0
            )

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
            range=[plot_times[0], plot_times[-1] + datetime.timedelta(minutes=15)],
            showgrid=True,
            gridcolor="#1E293B",
            tickformat="%H:%M\n%d %b",
            dtick=3600000 * 3
        )
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Select a date in the sidebar to view data.")
