"""Auto-refresh every 15 minutes during market hours.

Also runs the backtest job once per day after the US close so prior
predictions get scored against realized SPX/NDX moves.
"""
import time, subprocess, sys
from datetime import datetime

cycle = 0
last_backtest_date = None

while True:
    now = datetime.now()
    hour = now.hour
    # Only during US market hours (9:30 AM - 4:00 PM ET, adjust for your timezone)
    if 6 <= hour <= 20:  # broad window
        print(f"\n[{now.strftime('%H:%M:%S')}] Auto-refresh cycle {cycle + 1} starting...")

        # Fast scrape every cycle (yfinance + RSS)
        subprocess.run([sys.executable, "scraper.py"])

        # Deep scrape every other cycle (Playwright — heavier)
        if cycle % 2 == 0:
            print(f"  [Deep scrape] Running Playwright scrapers...")
            subprocess.run([sys.executable, "playwright_scraper.py"])

        # Score + predict
        subprocess.run([sys.executable, "qwen_analyzer.py"])

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Cycle {cycle + 1} complete. Next in 15 min.")
        cycle += 1
    else:
        print(f"[{now.strftime('%H:%M:%S')}] Outside market hours. Sleeping.")

    # Once-per-day backtest after US close (>= 21:00 local). Runs whether or
    # not we were inside the market window — outcomes only need a closed session.
    today_str = now.strftime("%Y-%m-%d")
    if hour >= 21 and last_backtest_date != today_str:
        print(f"[{now.strftime('%H:%M:%S')}] Running daily backtest...")
        subprocess.run([sys.executable, "backtest.py"])
        last_backtest_date = today_str

    time.sleep(900)  # 15 minutes
