import pulp
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
        CREATE TABLE IF NOT EXISTS rate_log (
            timestamp TEXT PRIMARY KEY,
            charge_rate REAL,
            discharge_rate REAL
        );
        CREATE TABLE IF NOT EXISTS capacity_log (
            timestamp TEXT PRIMARY KEY,
            capacity REAL
        );
        CREATE TABLE IF NOT EXISTS bucket_rate_log (
            timestamp TEXT NOT NULL,
            bucket INTEGER NOT NULL,   -- 0 to 4
            charge_rate REAL,
            discharge_rate REAL,
            PRIMARY KEY (timestamp, bucket)
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
    SURCHARGE_EUR_PER_MWH = 4.9
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

def read_battery_capacity_pct():
    """Return battery health as percentage of design capacity, or None if unreadable."""
    base = '/sys/class/power_supply/BAT0'
    pairs = [
        ('charge_full', 'charge_full_design'),
        ('energy_full', 'energy_full_design')
    ]
    for full_file, design_file in pairs:
        full_path = os.path.join(base, full_file)
        design_path = os.path.join(base, design_file)
        if os.path.exists(full_path) and os.path.exists(design_path):
            try:
                with open(full_path, 'r') as f:
                    full_val = float(f.read().strip())
                with open(design_path, 'r') as f:
                    design_val = float(f.read().strip())
                if design_val <= 0:
                    return None
                pct = (full_val / design_val) * 100.0
                return round(pct, 2)
            except Exception as e:
                logging.warning(f"Error reading battery capacity percentage: {e}")
                return None
    return None

def to_hel_naive(dt_aware):
    """Convert an aware datetime to naive Helsinki time ISO string."""
    hel_dt = dt_aware.astimezone(HEL_TZ)
    return hel_dt.replace(tzinfo=None).isoformat()

def compute_bucket_rates(conn, now_hel, lookback_days=7):
    """
    Compute average charge and discharge rates (% per 5 min) for each SoC bucket
    from the last `lookback_days`. Falls back to default values if no data in a bucket.
    """
    start_date = (now_hel - datetime.timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    # Default fallback rates (will be used if no data in a bucket)
    # These can be tuned later
    default_charge = [5.0, 5.0, 4.5, 2.0, 1.0]  # for buckets 0..4
    default_discharge = [2.5, 2.8, 3.0, 3.2, 3.5]

    rows = conn.execute("""
        SELECT timestamp, soc FROM soc_log
        WHERE date(timestamp) >= ?
        ORDER BY timestamp
    """, (start_date,)).fetchall()

    if len(rows) < 2:
        logging.warning("Not enough SoC data for bucket rates.")
        return default_charge, default_discharge

    sched_rows = conn.execute("""
        SELECT interval_start, decision FROM schedule
        WHERE date(interval_start) >= ?
    """, (start_date,)).fetchall()
    sched_dict = {row[0]: row[1] for row in sched_rows}

    # Accumulators per bucket: [ [charge_deltas], [discharge_deltas] ]
    charge_deltas = [[] for _ in range(5)]
    discharge_deltas = [[] for _ in range(5)]

    for i in range(len(rows) - 1):
        ts1_str, soc1 = rows[i]
        ts2_str, soc2 = rows[i+1]
        ts1 = datetime.datetime.fromisoformat(ts1_str).replace(tzinfo=HEL_TZ)
        ts2 = datetime.datetime.fromisoformat(ts2_str).replace(tzinfo=HEL_TZ)
        delta_sec = (ts2 - ts1).total_seconds()
        if not (270 <= delta_sec <= 330):
            continue

        # Determine bucket from starting SoC
        bucket = None
        if 0 <= soc1 < 30:
            bucket = 0
        elif 30 <= soc1 < 50:
            bucket = 1
        elif 50 <= soc1 < 70:
            bucket = 2
        elif 70 <= soc1 < 90:
            bucket = 3
        elif 90 <= soc1 <= 100:
            bucket = 4
        else:
            continue

        # Find schedule decision for this 5‑min block
        minute_of_day = ts1.hour * 60 + ts1.minute
        block_start = ts1.replace(hour=0, minute=0, second=0, microsecond=0) \
                       + datetime.timedelta(minutes=(minute_of_day // 5) * 5)
        naive_ts = block_start.replace(tzinfo=None).isoformat()
        decision = sched_dict.get(naive_ts, None)
        if decision is None:
            continue

        delta_soc = soc2 - soc1
        if decision == 1:      # charging
            charge_deltas[bucket].append(delta_soc)
        else:                  # discharging
            discharge_deltas[bucket].append(delta_soc)

    # Compute averages per bucket (with fallback)
    raw_charge = []
    raw_discharge = []
    for b in range(5):
        if charge_deltas[b]:
            raw_charge.append(sum(charge_deltas[b]) / len(charge_deltas[b]))
        else:
            raw_charge.append(None)
        if discharge_deltas[b]:
            # discharge deltas are negative; store as positive rate
            raw_discharge.append(-sum(discharge_deltas[b]) / len(discharge_deltas[b]))
        else:
            raw_discharge.append(None)

    # Load previous day's bucket rates to cap change
    prev_rows = conn.execute(
        "SELECT bucket, charge_rate, discharge_rate FROM bucket_rate_log "
        "WHERE date(timestamp) = ? ORDER BY bucket",
        ((now_hel - datetime.timedelta(days=1)).strftime("%Y-%m-%d"),)
    ).fetchall()
    prev_charge = {}
    prev_discharge = {}
    for row in prev_rows:
        prev_charge[row[0]] = row[1]
        prev_discharge[row[0]] = row[2]

    max_change = 0.2  # % per 5 min daily change limit

    final_charge = []
    final_discharge = []
    for b in range(5):
        # Fallback chain: computed → previous day → default
        ch = raw_charge[b]
        dch = raw_discharge[b]
        if ch is None:
            ch = prev_charge.get(b, None)
        if dch is None:
            dch = prev_discharge.get(b, None)
        if ch is None:
            ch = default_charge[b]
        if dch is None:
            dch = default_discharge[b]

        # Cap change from previous day
        if b in prev_charge:
            ch = max(prev_charge[b] - max_change, min(prev_charge[b] + max_change, ch))
            dch = max(prev_discharge[b] - max_change, min(prev_discharge[b] + max_change, dch))

        final_charge.append(round(ch, 4))
        final_discharge.append(round(dch, 4))

    logging.info(f"Bucket rates: charge {final_charge}, discharge {final_discharge}")
    return final_charge, final_discharge

def create_charging_plan():
    """Main planner function – fetch prices, solve MILP, store results."""
    now_hel = datetime.datetime.now(HEL_TZ)
    # 1. Determine start_time = next 5‑minute boundary after now
    minute_of_day = now_hel.hour * 60 + now_hel.minute
    next_block_minute = ((minute_of_day // 5) + 1) * 5
    start_time = now_hel.replace(hour=0, minute=0, second=0, microsecond=0) \
                 + datetime.timedelta(minutes=next_block_minute)

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
    # 3. Build 5‑minute price horizon
    midnight_today = datetime.datetime.combine(today_date, datetime.time(0, 0), tzinfo=HEL_TZ)
    all_prices_5min = []
    all_intervals = []
    # Today: from start_time onward
    for i in range(96):
        base = midnight_today + datetime.timedelta(minutes=i * 15)
        price = prices_today[i]
        for j in range(3):
            slot = base + datetime.timedelta(minutes=j * 5)
            if slot >= start_time:
                all_prices_5min.append(price)
                all_intervals.append(slot)
    # Tomorrow: full day
    for i in range(96):
        base = midnight_today + datetime.timedelta(days=1, minutes=i * 15)
        price = prices_tomorrow[i]
        for j in range(3):
            slot = base + datetime.timedelta(minutes=j * 5)
            all_prices_5min.append(price)
            all_intervals.append(slot)

    num_steps = len(all_prices_5min)
    logging.info(f"Planning {num_steps} intervals ({num_steps * 5 / 60:.1f} hours)")
    # 4. Open database connection (will be used throughout the function)
    conn = get_db()
    create_tables(conn)
    # 5. Store prices in database (both full days as 5‑min entries)
    for i in range(96):
        base = midnight_today + datetime.timedelta(minutes=i * 15)
        for j in range(3):
            slot = base + datetime.timedelta(minutes=j * 5)
            conn.execute("INSERT OR REPLACE INTO prices(interval_start, price) VALUES (?, ?)",
                         (to_hel_naive(slot), prices_today[i]))

    for i in range(96):
        base = midnight_today + datetime.timedelta(days=1, minutes=i * 15)
        for j in range(3):
            slot = base + datetime.timedelta(minutes=j * 5)
            conn.execute("INSERT OR REPLACE INTO prices(interval_start, price) VALUES (?, ?)",
                         (to_hel_naive(slot), prices_tomorrow[i]))
    conn.commit()
    # 6. Preserve old schedule for today up to start_time
    old_rows = conn.execute(
        "SELECT interval_start, decision, soc_forecast FROM schedule "
        "WHERE date(interval_start) = ? AND interval_start < ?",
        (today_date.isoformat(), to_hel_naive(start_time))
    ).fetchall()
    # 7b. Compute bucket rates dynamically
    ch_rates, dch_rates = compute_bucket_rates(conn, now_hel)

    # 8. MILP with bucket‑dependent constant rates
    start_soc = get_current_soc()
    prob = pulp.LpProblem("Battery_5min_buckets", pulp.LpMinimize)

    # Binary charge decision per step
    x = pulp.LpVariable.dicts("charge", range(num_steps), cat='Binary')

    # SOC variables
    soc = pulp.LpVariable.dicts("soc", range(num_steps + 1), lowBound=0, upBound=100)

    # Bucket indicators: b[t][b] = 1 if soc[t] is in bucket b (0..4)
    b = pulp.LpVariable.dicts("bucket", (range(num_steps), range(5)), cat='Binary')

    # Auxiliary variables y[t][b] = x[t] * b[t][b] (binary)
    y = pulp.LpVariable.dicts("y", (range(num_steps), range(5)), cat='Binary')

    # Bucket boundaries
    L = [0, 30, 50, 70, 90]
    U = [30, 50, 70, 90, 100]

    prob += soc[0] == start_soc

    for t in range(num_steps):
        # Exactly one bucket
        prob += pulp.lpSum([b[t][k] for k in range(5)]) == 1

        # SOC bounds from the chosen bucket
        prob += soc[t] >= pulp.lpSum([b[t][k] * L[k] for k in range(5)])
        prob += soc[t] <= pulp.lpSum([b[t][k] * U[k] for k in range(5)])

        # Linearise y[t][k] = x[t] * b[t][k]
        for k in range(5):
            prob += y[t][k] <= x[t]
            prob += y[t][k] <= b[t][k]
            prob += y[t][k] >= x[t] + b[t][k] - 1

        # Charge / discharge amount
        charge_amount = pulp.lpSum([y[t][k] * ch_rates[k] for k in range(5)])
        discharge_amount = pulp.lpSum([(b[t][k] - y[t][k]) * dch_rates[k] for k in range(5)])

        # SOC dynamics
        prob += soc[t+1] == soc[t] + charge_amount - discharge_amount

    # Objective: minimise cost
    prob += pulp.lpSum([x[t] * all_prices_5min[t] for t in range(num_steps)])

    logging.info("Solving MILP with bucket rates...")
    solver = pulp.HiGHS(msg=1, timeLimit=60)
    prob.solve(solver)

    if pulp.LpStatus[prob.status] != 'Optimal':
        logging.error(f"Solver failed: {pulp.LpStatus[prob.status]}")
        conn.close()
        return

    plan = [int(x[t].varValue) for t in range(num_steps)]
    soc_forecast = [soc[t].varValue for t in range(num_steps)]
    logging.info(f"Optimal. Charging blocks: {sum(plan)} / {num_steps}")
    # 9. Write schedule – keep old intervals, overwrite new ones
    for idx, slot in enumerate(all_intervals):
        conn.execute(
            "INSERT OR REPLACE INTO schedule(interval_start, decision, soc_forecast) VALUES (?, ?, ?)",
            (to_hel_naive(slot), plan[idx], soc_forecast[idx])
        )
    # Re-insert old rows we didn't touch
    for row in old_rows:
        conn.execute("INSERT OR IGNORE INTO schedule(interval_start, decision, soc_forecast) VALUES (?, ?, ?)",
                     row)
    # 10. Record plan run
    end_time = all_intervals[-1]
    conn.execute("INSERT INTO plan_runs(run_time, start_time, end_time) VALUES (?, ?, ?)",
                 (to_hel_naive(now_hel), to_hel_naive(start_time), to_hel_naive(end_time)))
    # 11. Store dynamic rates into rate_log
    # Store bucket rates for today
    for k in range(5):
        conn.execute("INSERT OR REPLACE INTO bucket_rate_log(timestamp, bucket, charge_rate, discharge_rate) VALUES (?, ?, ?, ?)",
                     (to_hel_naive(now_hel), k, ch_rates[k], dch_rates[k]))
    # 12. Read and store battery capacity (percentage of design)
    cap_pct = read_battery_capacity_pct()
    if cap_pct is not None:
        conn.execute("INSERT OR REPLACE INTO capacity_log(timestamp, capacity) VALUES (?, ?)",
                     (to_hel_naive(now_hel), cap_pct))
    # Final commit and close
    conn.commit()
    conn.close()
    logging.info("Plan, rates, and capacity saved.")

if __name__ == "__main__":
    create_charging_plan()
