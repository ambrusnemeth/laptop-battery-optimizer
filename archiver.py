#!/usr/bin/env python3
"""
Archive the previous calendar day.

Runs at 00:01 daily.
- Looks at all saved plans in archive/plans/ that cover any part of yesterday.
- Extracts the plan decisions and prices for each 15-min block of yesterday.
- Extracts SoC readings from history.json that fall on yesterday.
- Writes a clean archive/YYYY-MM-DD.json.
- Rewrites history.json keeping only today's entries.
"""

import json
import os
import datetime
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
ARCHIVE_DIR = BASE_DIR / "archive"
PLANS_DIR = ARCHIVE_DIR / "plans"
HISTORY_PATH = BASE_DIR / "history.json"

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

HEL_TZ = ZoneInfo("Europe/Helsinki")

def atomic_write_json(filepath, obj):
    tmp = filepath.with_suffix(filepath.suffix + ".tmp")
    with open(tmp, 'w') as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, filepath)

def load_json(path):
    if path.exists():
        with open(path, 'r') as f:
            return json.load(f)
    return None

def get_plan_for_interval(interval_start, plans):
    """
    Given a list of loaded plan dicts (each with 'start_time' and data),
    return the plan that was active at interval_start.
    We take the one with the latest start_time <= interval_start.
    """
    active = None
    latest_start = None
    for plan in plans:
        try:
            plan_start = datetime.datetime.fromisoformat(plan["start_time"])
        except:
            continue
        if plan_start <= interval_start:
            if (latest_start is None) or (plan_start > latest_start):
                latest_start = plan_start
                active = plan
    return active

def archive_yesterday():
    now_hel = datetime.datetime.now(HEL_TZ)
    yesterday = (now_hel - datetime.timedelta(days=1)).date()
    today = now_hel.date()

    logging.info(f"Archiving {yesterday}")

    # 1. Load all saved plan files that intersect yesterday
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    plan_files = sorted(PLANS_DIR.glob("plan_*.json"))
    plans = []
    for pf in plan_files:
        try:
            plan = load_json(pf)
            if plan and "start_time" in plan and "plan" in plan:
                # Filter only plans that could cover yesterday (rough pre‑filter)
                plan_start = datetime.datetime.fromisoformat(plan["start_time"])
                # Plan ends at start_time + len(plan)*15 min
                plan_end = plan_start + datetime.timedelta(minutes=len(plan["plan"]) * 15)
                # Intersection with yesterday?
                yesterday_start = datetime.datetime.combine(yesterday, datetime.time(0, 0), tzinfo=HEL_TZ)
                yesterday_end = yesterday_start + datetime.timedelta(days=1)
                if plan_start < yesterday_end and plan_end > yesterday_start:
                    plans.append(plan)
        except Exception as e:
            logging.warning(f"Skipping {pf.name}: {e}")

    if not plans:
        logging.error("No plans found covering yesterday. Archive aborted.")
        return

    # 2. Build 96 intervals for yesterday
    day_start = datetime.datetime.combine(yesterday, datetime.time(0, 0), tzinfo=HEL_TZ)
    intervals = [day_start + datetime.timedelta(minutes=i * 15) for i in range(96)]

    prices = []
    plan_decisions = []
    for t in intervals:
        active_plan = get_plan_for_interval(t, plans)
        if active_plan is None:
            logging.warning(f"No plan found for {t}. Filling with 0 and NaN price.")
            prices.append(None)
            plan_decisions.append(0)
            continue

        # Find the interval index in that plan
        plan_start = datetime.datetime.fromisoformat(active_plan["start_time"])
        delta = t - plan_start
        idx = int(delta.total_seconds() // 900)
        if 0 <= idx < len(active_plan["prices"]):
            prices.append(active_plan["prices"][idx])
            plan_decisions.append(active_plan["plan"][idx])
        else:
            logging.warning(f"Index {idx} out of bounds for plan starting {active_plan['start_time']}")
            prices.append(None)
            plan_decisions.append(0)

    # 3. Extract SoC history for yesterday
    hist_data = load_json(HISTORY_PATH)
    if hist_data is None:
        hist_data = []
    actual_soc = []
    for entry in hist_data:
        try:
            ts = datetime.datetime.fromisoformat(entry["iso_time"])
            if ts.date() == yesterday:
                actual_soc.append(entry)
        except:
            continue

    # 4. Write the daily archive
    archive_file = ARCHIVE_DIR / f"{yesterday}.json"
    archive_record = {
        "date": yesterday.isoformat(),
        "prices": prices,
        "plan": plan_decisions,
        "actual_soc": actual_soc
    }
    atomic_write_json(archive_file, archive_record)
    logging.info(f"Archived {yesterday} to {archive_file} ({len(actual_soc)} SoC points)")

    # 5. Trim history.json to keep only today's entries
    new_history = []
    for entry in hist_data:
        try:
            ts = datetime.datetime.fromisoformat(entry["iso_time"])
            if ts.date() >= today:
                new_history.append(entry)
        except:
            pass
    atomic_write_json(HISTORY_PATH, new_history)
    logging.info(f"Trimmed history.json, kept {len(new_history)} entries for today.")

if __name__ == "__main__":
    archive_yesterday()
