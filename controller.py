import os
import json
import datetime
import logging
from zoneinfo import ZoneInfo
import requests
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")
DATA_PATH = BASE_DIR / "dashboard_data.json"
HISTORY_PATH = BASE_DIR / "history.json"
HA_TOKEN = os.getenv("HA_TOKEN")
HA_ENTITY = os.getenv("HA_ENTITY")
HA_BASE_URL = os.getenv("HA_BASE_URL")
HA_URL_ON = f"{HA_BASE_URL}/api/services/switch/turn_on"
HA_URL_OFF = f"{HA_BASE_URL}/api/services/switch/turn_off"

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

def atomic_write_json(filepath, obj):
    tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")
    with open(tmp_path, 'w') as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp_path, filepath)

def get_real_battery_percentage():
    with open('/sys/class/power_supply/BAT0/capacity', 'r') as f:
        return int(f.read().strip())

def trigger_plug(turn_on):
    url = HA_URL_ON if turn_on else HA_URL_OFF
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json"
    }
    requests.post(url, headers=headers, json={"entity_id": HA_ENTITY})

def run_controller():
    helsinki_tz = ZoneInfo("Europe/Helsinki")
    now = datetime.datetime.now(helsinki_tz)
    current_battery = get_real_battery_percentage()
    logging.info(str(now))
    logging.info(f"Battery: {current_battery}%")

    # Log current SoC to history
    try:
        if HISTORY_PATH.exists():
            with open(HISTORY_PATH, 'r') as f:
                history = json.load(f)
        else:
            history = []
        history.append({
            "iso_time": now.isoformat(),
            "soc": current_battery
        })
        atomic_write_json(HISTORY_PATH, history)
    except Exception as e:
        logging.error(f"Logging error: {e}")

    # Load the plan and its start time
    try:
        with open(DATA_PATH, 'r') as f:
            data = json.load(f)
        plan_list = data["plan"]
        start_time_str = data["start_time"]
        start_time = datetime.datetime.fromisoformat(start_time_str)
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        logging.warning(f"Could not load plan: {e}. Defaulting to no charging.")
        trigger_plug(turn_on=False)
        return

    # Compute the 15-min interval index from the plan start
    seconds_elapsed = (now - start_time).total_seconds()
    current_index = int(seconds_elapsed // 900)

    # Determine charging decision
    if current_index < 0 or current_index >= len(plan_list):
        plan_says_charge = False
        logging.info(f"Time is outside plan horizon (index {current_index}). Switching off.")
    else:
        plan_says_charge = bool(plan_list[current_index])
        logging.info(f"Time: {now.strftime('%H:%M')}, Plan index: {current_index}, Charge: {plan_says_charge}")

    # Emergency charge logic (unchanged)
    try:
        with open('/tmp/emergency_charge.flag', 'r') as f:
            emergency_mode = f.read().strip() == 'True'
    except FileNotFoundError:
        emergency_mode = False

    if current_battery <= 20:
        logging.info("Battery critical (<20%). Engaging emergency charge.")
        trigger_plug(turn_on=True)
        with open('/tmp/emergency_charge.flag', 'w') as f:
            f.write('True')
    elif emergency_mode:
        if plan_says_charge == False:
            logging.info("Emergency over. Plan says OFF. Returning to normal schedule.")
            trigger_plug(turn_on=False)
            with open('/tmp/emergency_charge.flag', 'w') as f:
                f.write('False')
        else:
            logging.info("Emergency charging active, but plan is ON anyway. Staying ON.")
            trigger_plug(turn_on=True)
    else:
        logging.info(f"Normal Operation. Plan says: {'ON' if plan_says_charge else 'OFF'}")
        trigger_plug(turn_on=plan_says_charge)

if __name__ == "__main__":
    run_controller()
