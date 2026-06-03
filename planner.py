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
    """Write JSON atomically by using a temporary file and then renaming."""
    tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")
    with open(tmp_path, 'w') as f:
        json.dump(obj, f, indent=4 if isinstance(obj, dict) else 2)
    os.replace(tmp_path, filepath)

def fetch_nordpool_json(target_date):
    """Fetches 96-interval price data from the modern Nord Pool API."""
    date_str = target_date.strftime("%Y-%m-%d")
    url = f"https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices"
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

def archive_day():
    """Combines current data into a single file for the past day."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    hel_tz = ZoneInfo("Europe/Helsinki")
    now_hel = datetime.datetime.now(hel_tz)
    archive_date = (now_hel - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    archive_path = ARCHIVE_DIR / f"{archive_date}.json"
    logging.info("--- Archive Check ---")
    if not DATA_PATH.exists():
        logging.warning(f"Archive Failed: {DATA_PATH} not found.")
        return
    if not HISTORY_PATH.exists():
        logging.warning(f"Archive Failed: {HISTORY_PATH} not found.")
        return
    try:
        with open(DATA_PATH, 'r') as f:
            plan_data = json.load(f)
        with open(HISTORY_PATH, 'r') as f:
            soc_data = json.load(f)
        archive_record = {
            "prices": plan_data.get("prices"),
            "plan": plan_data.get("plan"),
            "actual_soc": soc_data
        }
        atomic_write_json(archive_path, archive_record)
        logging.info(f"Successfully archived: {archive_path}")
    except Exception as e:
        logging.error(f"Archiving error: {e}")

def get_current_soc():
    """Reads the actual physical battery level of the laptop."""
    try:
        with open('/sys/class/power_supply/BAT0/capacity', 'r') as f:
            return int(f.read().strip())
    except Exception as e:
        logging.warning(f"Could not read physical battery. Defaulting to 50. Error: {e}")
        return 50

def create_charging_plan():
    archive_day() 
    target_date = datetime.date.today()+ datetime.timedelta(days=1)
    logging.info(f"Targeting prices for: {target_date}")
    raw_data = fetch_nordpool_json(target_date)
    if not raw_data or 'multiAreaEntries' not in raw_data:
        logging.warning("!!! Data not available yet.")
        return
    prices = []
    for entry in raw_data.get('multiAreaEntries', []):
        val = entry.get('entryPerArea', {}).get('FI')
        if val is not None:
            prices.append(val)
    num_steps = len(prices)
    if num_steps == 0:
        logging.error("Extracted price list is empty.")
        return
    logging.info(f"Successfully loaded {num_steps} intervals.")
    start_soc = get_current_soc()
    prob = pulp.LpProblem("Battery_Optimization", pulp.LpMinimize)
    x = pulp.LpVariable.dicts("charge", range(num_steps), cat='Binary')
    soc = pulp.LpVariable.dicts("soc", range(num_steps + 1), lowBound=20, upBound=100)
    intervals_per_hour = num_steps / 24
    DISCHARGE = 33.33 / intervals_per_hour
    CHARGE = 50.00 / intervals_per_hour
    prob += soc[0] == start_soc
    for t in range(num_steps):
        prob += soc[t+1] <= soc[t] + x[t]*CHARGE - (1-x[t])*DISCHARGE
        prob += soc[t+1] >= soc[t] - DISCHARGE
    prob += pulp.lpSum([x[t] * prices[t] for t in range(num_steps)])
    logging.info("Solving MILP Model...")
    solver = None
    try:
        solver = pulp.HiGHS(msg=1, timeLimit=30)
        prob.solve(solver)
    except Exception as e:
        logging.warning(f"HiGHS failed ({e}), falling back to CBC.")
        solver = pulp.PULP_CBC_CMD(msg=1, timeLimit=30)
        prob.solve(solver)
    if pulp.LpStatus[prob.status] == 'Optimal':
        dashboard_data = {
            "prices": prices,
            "plan": [int(x[t].varValue) for t in range(num_steps)],
            "soc": [soc[t].varValue for t in range(num_steps)],
            "timestamp": datetime.datetime.now().isoformat()
        }
        atomic_write_json(DATA_PATH, dashboard_data)
        logging.info("Success! Optimal plan saved to dashboard_data.json")
        atomic_write_json(HISTORY_PATH, [])
        logging.info("History reset for the new day.")
    else:
        logging.error(f"Solver failed. Status: {pulp.LpStatus[prob.status]}")

if __name__ == "__main__":
    create_charging_plan()
