import pulp
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def fetch_nordpool_json(target_date):
    date_str = target_date.strftime("%Y-%m-%d")
    url = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices"
    params = {"date": date_str, "market": "DayAhead", "deliveryArea": "FI", "currency": "EUR"}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"Network error: {e}")
        return None

def extract_prices(raw_data):
    surcharge = 4.9
    prices = []
    for entry in raw_data.get('multiAreaEntries', []):
        val = entry.get('entryPerArea', {}).get('FI')
        if val is not None:
            prices.append(val + surcharge)
    return prices

def get_current_soc():
    try:
        with open('/sys/class/power_supply/BAT0/capacity', 'r') as f:
            return int(f.read().strip())
    except:
        return 50

def to_hel_naive(dt_aware):
    hel_dt = dt_aware.astimezone(HEL_TZ)
    return hel_dt.replace(tzinfo=None).isoformat()

def create_charging_plan():
    now_hel = datetime.datetime.now(HEL_TZ)

    # 1. Determine start_time = next 5‑minute boundary after now
    minute_of_day = now_hel.hour * 60 + now_hel.minute
    next_block_minute = ((minute_of_day // 5) + 1) * 5
    start_time = now_hel.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(minutes=next_block_minute)

    today_date = start_time.date()
    tomorrow_date = today_date + datetime.timedelta(days=1)

    logging.info(f"Plan start: {start_time}")

    # 2. Fetch prices (15‑min resolution, 96 values)
    raw_today = fetch_nordpool_json(today_date)
    raw_tomorrow = fetch_nordpool_json(tomorrow_date)
    if not raw_today or not raw_tomorrow:
        logging.error("Price fetch failed.")
        return

    prices15_today = extract_prices(raw_today)
    prices15_tomorrow = extract_prices(raw_tomorrow)
    if len(prices15_today) != 96 or len(prices15_tomorrow) != 96:
        logging.error("Price lists malformed.")
        return

    # 3. Build 5‑minute price horizon from start_time to end of tomorrow
    midnight_today = datetime.datetime.combine(today_date, datetime.time(0, 0), tzinfo=HEL_TZ)
    all_prices_5min = []
    all_intervals = []

    # Today: from start_time onward (5‑min steps)
    # We iterate over 5‑minute slots; the price is the same for 3 consecutive slots
    for i in range(96):   # 15‑min index
        base = midnight_today + datetime.timedelta(minutes=i * 15)
        price = prices15_today[i]
        for j in range(3):
            slot = base + datetime.timedelta(minutes=j * 5)
            if slot >= start_time:
                all_prices_5min.append(price)
                all_intervals.append(slot)

    # Tomorrow: full day
    for i in range(96):
        base = midnight_today + datetime.timedelta(days=1, minutes=i * 15)
        price = prices15_tomorrow[i]
        for j in range(3):
            slot = base + datetime.timedelta(minutes=j * 5)
            all_prices_5min.append(price)
            all_intervals.append(slot)

    num_steps = len(all_prices_5min)
    logging.info(f"Horizon: {num_steps} intervals of 5 min ({num_steps * 5 / 60:.1f} hours)")

    # 4. Store prices in database (both full days as 5‑min entries)
    conn = get_db()
    # Insert today's 288 prices
    for i in range(96):
        base = midnight_today + datetime.timedelta(minutes=i * 15)
        for j in range(3):
            slot = base + datetime.timedelta(minutes=j * 5)
            conn.execute("INSERT OR REPLACE INTO prices(interval_start, price) VALUES (?, ?)",
                         (to_hel_naive(slot), prices15_today[i]))
    # Tomorrow
    for i in range(96):
        base = midnight_today + datetime.timedelta(days=1, minutes=i * 15)
        for j in range(3):
            slot = base + datetime.timedelta(minutes=j * 5)
            conn.execute("INSERT OR REPLACE INTO prices(interval_start, price) VALUES (?, ?)",
                         (to_hel_naive(slot), prices15_tomorrow[i]))
    conn.commit()

    # 5. Preserve old schedule for today up to start_time
    old_rows = conn.execute(
        "SELECT interval_start, decision, soc_forecast FROM schedule "
        "WHERE date(interval_start) = ? AND interval_start < ?",
        (today_date.isoformat(), to_hel_naive(start_time))
    ).fetchall()

    # 6. MILP with 5‑minute parameters
    CHARGE_5 = 50.0 / 12   # 4.1667 % per 5 min
    DISCHARGE_5 = 33.33 / 12   # 2.7775 % per 5 min

    start_soc = get_current_soc()
    prob = pulp.LpProblem("Battery_5min", pulp.LpMinimize)
    x = pulp.LpVariable.dicts("charge", range(num_steps), cat='Binary')
    soc = pulp.LpVariable.dicts("soc", range(num_steps + 1), lowBound=20, upBound=100)

    prob += soc[0] == start_soc
    for t in range(num_steps):
        prob += soc[t+1] == soc[t] + x[t] * CHARGE_5 - (1 - x[t]) * DISCHARGE_5

    prob += pulp.lpSum([x[t] * all_prices_5min[t] for t in range(num_steps)])

    logging.info("Solving MILP...")
    solver = pulp.HiGHS(msg=1, timeLimit=60)   # slightly larger, so a bit more time
    prob.solve(solver)

    if pulp.LpStatus[prob.status] != 'Optimal':
        logging.error(f"Solver failed: {pulp.LpStatus[prob.status]}")
        conn.close()
        return

    plan = [int(x[t].varValue) for t in range(num_steps)]
    soc_forecast = [soc[t].varValue for t in range(num_steps)]
    logging.info(f"Optimal. Charging blocks: {sum(plan)} / {num_steps}")

    # 7. Write schedule
    for idx, slot in enumerate(all_intervals):
        conn.execute(
            "INSERT OR REPLACE INTO schedule(interval_start, decision, soc_forecast) VALUES (?, ?, ?)",
            (to_hel_naive(slot), plan[idx], soc_forecast[idx])
        )
    # Re-insert old rows for the part we didn't touch
    for row in old_rows:
        conn.execute("INSERT OR IGNORE INTO schedule(interval_start, decision, soc_forecast) VALUES (?, ?, ?)",
                     row)

    # 8. Plan run record
    end_time = all_intervals[-1]
    conn.execute("INSERT INTO plan_runs(run_time, start_time, end_time) VALUES (?, ?, ?)",
                 (to_hel_naive(now_hel), to_hel_naive(start_time), to_hel_naive(end_time)))
    conn.commit()
    conn.close()
    logging.info("Plan saved.")

if __name__ == "__main__":
    create_charging_plan()
