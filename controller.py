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
    """Write JSON atomically by using a temporary file and then renaming."""
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
    current_hour = str(now.hour)
    current_battery = get_real_battery_percentage()
    logging.info(str(now))
    logging.info(str(current_battery))
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
    try:
        with open(DATA_PATH, 'r') as f:
            data = json.load(f)
        plan_list = data["plan"]
        schedule = {str(i): bool(v) for i, v in enumerate(plan_list)}
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        logging.warning(f"Could not load plan: {e}. Defaulting to no charging.")
        schedule = {}
    
    mins_since_midnight = (now.hour * 60) + now.minute
    offset_minutes = (mins_since_midnight - 60) % 1440
    current_index = str(offset_minutes // 15)
    logging.info(f"Time: {now.strftime('%H:%M')}, Index: {current_index}")
    plan_says_charge = schedule.get(current_index, False)
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
        logging.info(f"Normal Operation. Plan for hour {current_hour}: {'ON' if plan_says_charge else 'OFF'}")
        trigger_plug(turn_on=plan_says_charge)

if __name__ == "__main__":
    run_controller()
