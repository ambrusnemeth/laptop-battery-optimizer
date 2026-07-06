import os
import json
import sqlite3
import datetime
import logging
from zoneinfo import ZoneInfo
import requests
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "battery.db"
load_dotenv(dotenv_path=BASE_DIR / ".env")

HA_TOKEN = os.getenv("HA_TOKEN")
HA_ENTITY = os.getenv("HA_ENTITY")
HA_BASE_URL = os.getenv("HA_BASE_URL")
HA_URL_ON = f"{HA_BASE_URL}/api/services/switch/turn_on"
HA_URL_OFF = f"{HA_BASE_URL}/api/services/switch/turn_off"

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


def get_real_battery_percentage():
    try:
        with open('/sys/class/power_supply/BAT0/capacity', 'r') as f:
            return int(f.read().strip())
    except:
        return 50


def trigger_plug(turn_on):
    url = HA_URL_ON if turn_on else HA_URL_OFF
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json"
    }
    requests.post(url, headers=headers, json={"entity_id": HA_ENTITY})


def get_current_interval(now_hel):
    """Return the start of the 15-min block containing now_hel."""
    minute_block = (now_hel.hour * 60 + now_hel.minute) // 15
    return now_hel.replace(hour=0, minute=0, second=0, microsecond=0) \
           + datetime.timedelta(minutes=minute_block * 15)


def run_controller():
    now_hel = datetime.datetime.now(HEL_TZ)
    current_battery = get_real_battery_percentage()

    logging.info(f"Time: {now_hel.isoformat()}, Battery: {current_battery}%")

    # Log SoC to database (naive Helsinki)
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO soc_log(timestamp, soc) VALUES (?, ?)",
        (now_hel.replace(tzinfo=None).isoformat(), current_battery)
    )
    conn.commit()

    # Get plan decision for current interval
    interval = get_current_interval(now_hel)
    naive_interval = interval.replace(tzinfo=None).isoformat()

    row = conn.execute(
        "SELECT decision FROM schedule WHERE interval_start = ?",
        (naive_interval,)
    ).fetchone()

    plan_says_charge = bool(row[0]) if row else False
    conn.close()

    logging.info(f"Interval: {naive_interval}, Plan says: {'ON' if plan_says_charge else 'OFF'}")

    # Emergency logic
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
        if not plan_says_charge:
            logging.info("Emergency over. Plan says OFF. Returning to normal schedule.")
            trigger_plug(turn_on=False)
            with open('/tmp/emergency_charge.flag', 'w') as f:
                f.write('False')
        else:
            logging.info("Emergency charging active, plan is ON anyway. Staying ON.")
            trigger_plug(turn_on=True)
    else:
        logging.info(f"Normal operation. Plug: {'ON' if plan_says_charge else 'OFF'}")
        trigger_plug(turn_on=plan_says_charge)


if __name__ == "__main__":
    run_controller()
