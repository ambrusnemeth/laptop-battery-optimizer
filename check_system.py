#!/usr/bin/env python3
"""
Check the health of the battery optimization system.
Run manually: python3 check_system.py
"""

import sqlite3
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "battery.db"
HEL_TZ = ZoneInfo("Europe/Helsinki")


def check_all():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    print("=" * 50)
    print("  SYSTEM HEALTH CHECK")
    print("=" * 50)
    now = datetime.datetime.now(HEL_TZ)
    print(f"Current Helsinki time: {now}")
    print()

    # 1. Prices
    today = now.date().isoformat()
    tomorrow = (now.date() + datetime.timedelta(days=1)).isoformat()

    prices_today = cur.execute(
        "SELECT COUNT(*) FROM prices WHERE date(interval_start) = ?", (today,)
    ).fetchone()[0]
    prices_tomorrow = cur.execute(
        "SELECT COUNT(*) FROM prices WHERE date(interval_start) = ?", (tomorrow,)
    ).fetchone()[0]
    print(f"📊 Prices for today ({today}): {prices_today}/288 intervals")
    print(f"📊 Prices for tomorrow ({tomorrow}): {prices_tomorrow}/288 intervals")
    # 2. Schedule
    sched_today = cur.execute(
        "SELECT COUNT(*) FROM schedule WHERE date(interval_start) = ?", (today,)
    ).fetchone()[0]
    sched_tomorrow = cur.execute(
        "SELECT COUNT(*) FROM schedule WHERE date(interval_start) = ?", (tomorrow,)
    ).fetchone()[0]
    print(f"📋 Schedule for today: {sched_today}/288 intervals")
    print(f"📋 Schedule for tomorrow: {sched_tomorrow}/288 intervals")
    # 3. SOC log
    soc_count = cur.execute(
        "SELECT COUNT(*) FROM soc_log WHERE date(timestamp) = ?", (today,)
    ).fetchone()[0]
    latest_soc = cur.execute(
        "SELECT timestamp, soc FROM soc_log ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    print(f"🔋 SoC logs today: {soc_count}")
    if latest_soc:
        print(f"   Latest: {latest_soc[0]} -> {latest_soc[1]}%")

    # 4. Plan runs
    plan_runs = cur.execute(
        "SELECT run_time, start_time, end_time FROM plan_runs ORDER BY run_time DESC LIMIT 5"
    ).fetchall()
    print(f"\n📝 Recent plan runs:")
    for run in plan_runs:
        print(f"   Run: {run[0]}, active from {run[1]} to {run[2]}")

    # 5. Upcoming charging blocks
    current_interval_min = (now.hour * 60 + now.minute) // 5 * 5
    current_interval_str = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(minutes=current_interval_min)
    naive_now = current_interval_str.replace(tzinfo=None).isoformat()
    upcoming = cur.execute(
        "SELECT interval_start, decision FROM schedule "
        "WHERE date(interval_start) = ? AND interval_start >= ? AND decision = 1 "
        "ORDER BY interval_start LIMIT 5",
        (today, naive_now)
    ).fetchall()
    print(f"\n⚡ Next charging blocks:")
    if upcoming:
        for block in upcoming:
            print(f"   {block[0]} -> Charging")
    else:
        print("   No charging planned for the rest of today")

    conn.close()
    print("\n" + "=" * 50)


if __name__ == "__main__":
    check_all()
