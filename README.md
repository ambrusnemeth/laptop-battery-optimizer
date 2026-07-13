# Spot‑Price Battery Optimizer

Optimize the charging schedule of a laptop (or any battery‑backed device) based on the hourly electricity spot price.  
A mixed‑integer linear program (MILP) decides when to charge to minimise cost, while keeping the battery within safe limits.  
A Home Assistant smart plug does the actual switching.

## How It Works

- **Planner** – Fetches day‑ahead Nord Pool prices, builds a long‑horizon MILP (with SoC‑dependent charge/discharge rates), and writes an optimal on/off schedule to a SQLite database.  
- **Controller** – Runs every 5 minutes, reads the current battery level and the planned schedule, and toggles a Home Assistant plug. It logs the battery state of charge.  
- **Dashboard** – A Streamlit web app that visualises prices, planned charging blocks, real battery history, dynamic rate estimates, and battery health over time.

All components share a single SQLite database. Historic data is kept permanently and can be browsed in the dashboard.

## Project Structure

| File | Role |
|------|------|
| `planner.py` | Fetches prices, solves MILP, stores schedule and adaptive rates. |
| `controller.py` | Monitors battery, controls the plug, logs SoC every 5 min. |
| `dashboard.py` | Streamlit dashboard with calendar‑day view, rate charts, and capacity tracking. |
| `battery.db` | SQLite database containing prices, schedule, SoC logs, plan metadata, and dynamic rate tables. |

## Live Dashboard

👉 **[Click the link!](https://nemethambrus.ddns.net/dashboard)**

## Key Features

- **Spot‑price optimization** using Nord Pool day‑ahead prices (Finnish bidding area).  
- **SoC‑dependent rates** – the solver uses per‑bucket charge/discharge speeds that are recalculated daily from historical data.  
- **Calendar‑day view** with a green dashed line marking when a new plan was generated.  
- **Battery health tracking** – capacity as percentage of design is logged daily.  
- **Logarithmic time charts** for rates and capacity (more detail near the present).

## Requirements

- Python 3.10+  
- Home Assistant with a compatible smart plug  
- A laptop or server with a battery (read via `/sys/class/power_supply/BAT0/`)  
- [PuLP](https://github.com/coin-or/pulp) with the HiGHS solver  
- Streamlit, Plotly, Requests, python‑dotenv

## Setup

1. Clone the repository and install dependencies:
   ```bash
   pip install -r requirements.txt
