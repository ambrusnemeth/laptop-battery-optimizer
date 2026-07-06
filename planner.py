import pulp
import json
import os
import datetime
import sqlite3
import requests
import logging
from pathlib import Path
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "battery.db"
load_dotenv(dotenv_path=BASE_DIR / ".env")

HEL_TZ = ZoneInfo("Europe/Helsinki")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def create_tables(conn):
    """Ensure all required tables exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS prices (
            interval_start TEXT PRIMARY KEY,
            price REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schedule (
            interval_start TEXT PRIMARY KEY,
            decision INTEGER NOT NULL,
            soc_forecast REAL
        );
        CREATE TABLE IF NOT EXISTS soc_log (
            timestamp TEXT PRIMARY KEY,
            soc INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL
        );
    """)
    conn.commit()


def fetch_nordpool_json(target_date):
    """Fetches 96-interval price data from the modern Nord Pool API."""
    date_str = target_date.strftime("%Y-%m-%d")
    url = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices"
    params = {
        "date": date_str,
        "market": "DayAhead",
        "deliveryArea": "FI",
        "currency": "EUR"
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Network error fetching prices: {e}")
        return None


def extract_prices(raw_data):
    """Extracts price list from API response, adds surcharge."""
    SURCHARGE_EUR_PER_MWH = 4.9   # 0.49 c/kWh = 4.9 €/MWh
    prices = []
    for entry in raw_data.get('multiAreaEntries', []):
        val = entry.get('entryPerArea', {}).get('FI')
        if val is not None:
            prices.append(val + SURCHARGE_EUR_PER_MWH)
    return prices


def get_current_soc():
    """Reads the actual physical battery level of the laptop."""
    try:
        with open('/sys/class/power_supply/BAT0/capacity', 'r') as f:
            return int(f.read().strip())
    except Exception as e:
        logging.warning(f"Could not read physical battery. Defaulting to 50. Error: {e}")
        return 50


def to_hel_naive(dt_aware):
    """Convert an aware datetime to naive Helsinki time ISO string."""
    hel_dt = dt_aware.astimezone(HEL_TZ)
    return hel_dt.replace(tzinfo=None).isoformat()


def create_charging_plan():
    """Main planner function – fetch prices, solve MILP, store results."""
    now_hel = datetime.datetime.now(HEL_TZ)

    # 1. Determine start_time = next 15-min boundary after now
    minutes_since_midnight = now_hel.hour * 60 + now_hel.minute
    next_block_index = (minutes_since_midnight // 15) + 1
    next_block_minutes = next_block_index * 15
    start_time = now_hel.replace(hour=0, minute=0, second=0, microsecond=0) \
                 + datetime.timedelta(minutes=next_block_minutes)

    today_date = start_time.date()
    tomorrow_date = today_date + datetime.timedelta(days=1)

    logging.info(f"Plan start time (Helsinki): {start_time}")
    logging.info(f"Horizon: {today_date} (from {start_time.time()}) to end of {tomorrow_date}")

    # 2. Fetch prices
    raw_today = fetch_nordpool_json(today_date)
    raw_tomorrow = fetch_nordpool_json(tomorrow_date)

    if not raw_today or not raw_tomorrow:
        logging.error("Failed to fetch required price data.")
        return

    prices_today = extract_prices(raw_today)
    prices_tomorrow = extract_prices(raw_tomorrow)

    if len(prices_today) != 96 or len(prices_tomorrow) != 96:
        logging.error("Price lists do not have the expected 96 intervals.")
        return

    # 3. Build the price list for the horizon
    midnight_today = datetime.datetime.combine(today_date, datetime.time(0, 0), tzinfo=HEL_TZ)
    all_prices = []
    all_intervals = []

    for i in range(96):
        interval_start = midnight_today + datetime.timedelta(minutes=i * 15)
        if interval_start >= start_time:
            all_prices.append(prices_today[i])
            all_intervals.append(interval_start)

    for i in range(96):
        interval_start = midnight_today + datetime.timedelta(days=1, minutes=i * 15)
        all_prices.append(prices_tomorrow[i])
        all_intervals.append(interval_start)

    num_steps = len(all_prices)
    logging.info(f"Planning {num_steps} intervals ({num_steps * 15 / 60:.1f} hours)")

    # 4. Store prices in database (both full days)
    conn = get_db()
    create_tables(conn)

    for i, price in enumerate(prices_today):
        ts = midnight_today + datetime.timedelta(minutes=i * 15)
        conn.execute(
            "INSERT OR REPLACE INTO prices(interval_start, price) VALUES (?, ?)",
            (to_hel_naive(ts), price)
        )

    for i, price in enumerate(prices_tomorrow):
        ts = midnight_today + datetime.timedelta(days=1, minutes=i * 15)
        conn.execute(
            "INSERT OR REPLACE INTO prices(interval_start, price) VALUES (?, ?)",
            (to_hel_naive(ts), price)
        )

    # 5. Preserve old schedule for today (up to start_time)
    old_rows = conn.execute(
        "SELECT interval_start, decision, soc_forecast FROM schedule "
        "WHERE date(interval_start) = ? AND interval_start < ?",
        (today_date.isoformat(), to_hel_naive(start_time))
    ).fetchall()

    # 6. MILP setup
    INTERVALS_PER_HOUR = 4
    DISCHARGE = 33.33 / INTERVALS_PER_HOUR
    CHARGE = 50.00 / INTERVALS_PER_HOUR

    start_soc = get_current_soc()
    logging.info(f"Current battery: {start_soc}%")

    prob = pulp.LpProblem("Battery_Optimization", pulp.LpMinimize)
    x = pulp.LpVariable.dicts("charge", range(num_steps), cat='Binary')
    soc = pulp.LpVariable.dicts("soc", range(num_steps + 1), lowBound=20, upBound=100)

    prob += soc[0] == start_soc
    for t in range(num_steps):
        prob += soc[t + 1] == soc[t] + x[t] * CHARGE - (1 - x[t]) * DISCHARGE

    prob += pulp.lpSum([x[t] * all_prices[t] for t in range(num_steps)])

    logging.info("Solving MILP...")
    solver = pulp.HiGHS(msg=1, timeLimit=30)
    prob.solve(solver)

    if pulp.LpStatus[prob.status] != 'Optimal':
        logging.error(f"Solver failed. Status: {pulp.LpStatus[prob.status]}")
        conn.close()
        return

    logging.info(f"Optimal solution found. Cost: {pulp.value(prob.objective):.2f}")

    plan = [int(x[t].varValue) for t in range(num_steps)]
    soc_forecast = [soc[t].varValue for t in range(num_steps)]

    # 7. Write schedule – keep old intervals, overwrite new ones
    for idx, interval_start in enumerate(all_intervals):
        naive_str = to_hel_naive(interval_start)
        conn.execute(
            "INSERT OR REPLACE INTO schedule(interval_start, decision, soc_forecast) VALUES (?, ?, ?)",
            (naive_str, plan[idx], soc_forecast[idx])
        )

    # Re-insert old intervals that start_time may have skipped (belt and braces)
    for row in old_rows:
        conn.execute(
            "INSERT OR IGNORE INTO schedule(interval_start, decision, soc_forecast) VALUES (?, ?, ?)",
            (row[0], row[1], row[2])
        )

    # 8. Record plan run
    end_time = all_intervals[-1]
    conn.execute(
        "INSERT INTO plan_runs(run_time, start_time, end_time) VALUES (?, ?, ?)",
        (to_hel_naive(now_hel), to_hel_naive(start_time), to_hel_naive(end_time))
    )

    conn.commit()
    conn.close()

    logging.info(f"Plan written to database. {num_steps} intervals.")
    logging.info(f"Charging blocks: {sum(plan)} out of {num_steps}")


if __name__ == "__main__":
    create_charging_plan()
