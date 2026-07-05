import pulp
import json
import os
import datetime
import requests
import shutil
import logging
from pathlib import Path
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "dashboard_data.json"
load_dotenv(dotenv_path=BASE_DIR / ".env")
HISTORY_PATH = BASE_DIR / "history.json"
ARCHIVE_DIR = BASE_DIR / "archive"
ARCHIVE_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

def atomic_write_json(filepath, obj):
    tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")
    with open(tmp_path, 'w') as f:
        json.dump(obj, f, indent=4 if isinstance(obj, dict) else 2)
    os.replace(tmp_path, filepath)

def fetch_nordpool_json(target_date):
    """Fetches 96-interval price data for a single date."""
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

def get_current_soc():
    """Reads the actual physical battery level of the laptop."""
    try:
        with open('/sys/class/power_supply/BAT0/capacity', 'r') as f:
            return int(f.read().strip())
    except Exception as e:
        logging.warning(f"Could not read physical battery. Defaulting to 50. Error: {e}")
        return 50

def extract_prices(raw_data):
    """Extracts price list from API response, adds surcharge."""
    SURCHARGE_EUR_PER_MWH = 4.9   # 0.49 c/kWh = 4.9 €/MWh
    prices = []
    for entry in raw_data.get('multiAreaEntries', []):
        val = entry.get('entryPerArea', {}).get('FI')
        if val is not None:
            prices.append(val + SURCHARGE_EUR_PER_MWH)
    return prices

# ... (all imports and functions above remain identical) ...

def create_charging_plan():
    hel_tz = ZoneInfo("Europe/Helsinki")
    now_hel = datetime.datetime.now(hel_tz)

    # 1. Determine the first 15-min interval that starts *after* now
    minutes_since_midnight = now_hel.hour * 60 + now_hel.minute
    next_block_index = (minutes_since_midnight // 15) + 1
    next_block_minutes = next_block_index * 15
    start_time = now_hel.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(minutes=next_block_minutes)
    logging.info(f"Plan will start at {start_time.isoformat()}")

    # 2. Fetch today's and tomorrow's prices
    today_date = start_time.date()
    tomorrow_date = today_date + datetime.timedelta(days=1)

    raw_today = fetch_nordpool_json(today_date)
    raw_tomorrow = fetch_nordpool_json(tomorrow_date)

    if not raw_today or not raw_tomorrow:
        logging.error("Failed to fetch required price data for both days.")
        return

    prices_today = extract_prices(raw_today)
    prices_tomorrow = extract_prices(raw_tomorrow)

    if len(prices_today) != 96 or len(prices_tomorrow) != 96:
        logging.error("Price lists do not have the expected 96 intervals.")
        return

    # 3. Build the price list for the horizon (start_time → end of tomorrow)
    midnight_today = datetime.datetime.combine(today_date, datetime.time(0, 0), tzinfo=hel_tz)
    today_intervals = []
    for i, price in enumerate(prices_today):
        interval_start = midnight_today + datetime.timedelta(minutes=i * 15)
        if interval_start >= start_time:
            today_intervals.append(price)

    all_prices = today_intervals + prices_tomorrow
    num_steps = len(all_prices)
    logging.info(f"Planning horizon: {num_steps} intervals ({num_steps*15/60:.1f} hours)")

    # 4. MILP setup (15-min intervals → 4 per hour)
    INTERVALS_PER_HOUR = 4
    DISCHARGE = 33.33 / INTERVALS_PER_HOUR   # % per 15 min
    CHARGE = 50.00 / INTERVALS_PER_HOUR      # % per 15 min

    start_soc = get_current_soc()
    prob = pulp.LpProblem("Battery_Optimization", pulp.LpMinimize)
    x = pulp.LpVariable.dicts("charge", range(num_steps), cat='Binary')
    soc = pulp.LpVariable.dicts("soc", range(num_steps + 1), lowBound=20, upBound=100)

    prob += soc[0] == start_soc
    for t in range(num_steps):
        prob += soc[t+1] == soc[t] + x[t] * CHARGE - (1 - x[t]) * DISCHARGE

    prob += pulp.lpSum([x[t] * all_prices[t] for t in range(num_steps)])

    logging.info("Solving MILP Model...")
    solver = pulp.HiGHS(msg=1, timeLimit=30)
    prob.solve(solver)

    if pulp.LpStatus[prob.status] == 'Optimal':
        new_plan = {
            "prices": all_prices,
            "plan": [int(x[t].varValue) for t in range(num_steps)],
            "soc": [soc[t].varValue for t in range(num_steps)],
            "start_time": start_time.isoformat(),
            "timestamp": now_hel.isoformat()
        }

        # Save a copy of the OLD plan before overwriting, if it exists
        old_plan_path = DATA_PATH
        if old_plan_path.exists():
            plans_archive = BASE_DIR / "archive" / "plans"
            plans_archive.mkdir(parents=True, exist_ok=True)
            try:
                with open(old_plan_path, 'r') as f:
                    old_data = json.load(f)
                old_start = old_data.get("start_time", "unknown")
                old_start_clean = old_start.replace(":", "").replace("+", "_")  # safe filename
                backup_path = plans_archive / f"plan_{old_start_clean}.json"
                shutil.copy2(old_plan_path, backup_path)
                logging.info(f"Old plan saved to {backup_path}")
            except Exception as e:
                logging.warning(f"Could not backup old plan: {e}")

        # Write the new plan
        atomic_write_json(DATA_PATH, new_plan)
        logging.info("Success! Optimal plan saved to dashboard_data.json")
    else:
        logging.error(f"Solver failed. Status: {pulp.LpStatus[prob.status]}")

if __name__ == "__main__":
    create_charging_plan()
