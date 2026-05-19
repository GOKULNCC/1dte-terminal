"""
Playwright-powered scraper — supplements yfinance + feedparser
Scrapes JS-rendered sites that simple HTTP requests can't handle:
  1. TradingView technical signals (SPX/NDX buy/sell/neutral)
  2. Investing.com multi-timeframe technicals (5m, 15m, 1h, daily, weekly)
  3. CBOE Put/Call ratio
  4. Finviz market overview (futures, sectors) + ticker-specific news
  5. MarketWatch headlines
  6. Investing.com economic calendar
"""
import json, hashlib, re, time
from datetime import datetime
from playwright.sync_api import sync_playwright

from config import db_connect

# Finviz tickers for news scraping
FINVIZ_TICKERS = ["SPY", "QQQ", "NVDA", "AAPL", "TSLA", "MSFT", "META", "AMZN", "GOOGL"]

# TradingView symbols to scrape technicals
TV_SYMBOLS = [
    {"symbol": "SPX", "url": "https://www.tradingview.com/symbols/SPX/technicals/"},
    {"symbol": "NDX", "url": "https://www.tradingview.com/symbols/NASDAQ-NDX/technicals/"},
]

# Investing.com technical analysis URLs
INVESTING_TECHNICALS = [
    {"symbol": "SPX", "url": "https://www.investing.com/indices/us-spx-500-technical"},
    {"symbol": "NDX", "url": "https://www.investing.com/indices/nq-100-technical"},
]

INVESTING_TIMEFRAMES = ["5", "15", "60", "300", "week"]  # 5m, 15m, 1h, daily, weekly


def init_db():
    """Extend schema with all Playwright-specific tables"""
    conn = db_connect()
    conn.execute("""CREATE TABLE IF NOT EXISTS fear_greed (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        score INTEGER, label TEXT, previous_close INTEGER,
        one_week_ago INTEGER, one_month_ago INTEGER,
        fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sectors (
        name TEXT PRIMARY KEY, change_pct REAL,
        fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS futures (
        name TEXT PRIMARY KEY, last REAL, change_pct REAL,
        fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS econ_calendar (
        id TEXT PRIMARY KEY, date TEXT, time TEXT, currency TEXT,
        impact TEXT, event TEXT, actual TEXT, forecast TEXT,
        previous TEXT, fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS technicals (
        id TEXT PRIMARY KEY,
        source TEXT, symbol TEXT, timeframe TEXT,
        summary TEXT, buy_count INTEGER, sell_count INTEGER, neutral_count INTEGER,
        ma_summary TEXT, ma_buy INTEGER, ma_sell INTEGER, ma_neutral INTEGER,
        osc_summary TEXT, osc_buy INTEGER, osc_sell INTEGER, osc_neutral INTEGER,
        fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS putcall (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, equity_ratio REAL, index_ratio REAL, total_ratio REAL,
        exchange_volume TEXT, fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS finviz_news (
        id TEXT PRIMARY KEY, ticker TEXT, headline TEXT, source TEXT,
        url TEXT, time_str TEXT, fetched_at TEXT)""")
    conn.commit()
    return conn


# ============================================================
# 1. TradingView Technical Signals
# ============================================================
def scrape_tradingview_technicals(page, conn):
    """Scrape TradingView technicals gauge for SPX and NDX"""
    print("  [Playwright] Scraping TradingView technicals...")
    results = []
    now = datetime.now().isoformat()

    for tv in TV_SYMBOLS:
        try:
            page.goto(tv["url"], wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(4000)

            # The technicals page has a summary gauge
            # Try to find the recommendation text (STRONG BUY, BUY, NEUTRAL, SELL, STRONG SELL)
            summary = "Neutral"
            buy_count = sell_count = neutral_count = 0

            # Look for the speedometer/gauge text
            gauge_els = page.query_selector_all("[class*='speedometerSignal']")
            if not gauge_els:
                gauge_els = page.query_selector_all("[class*='speedometer'] span")
            if not gauge_els:
                # Broader search for recommendation text
                all_text = page.inner_text("body")
                for sig in ["Strong Buy", "Buy", "Strong Sell", "Sell", "Neutral"]:
                    if sig in all_text:
                        summary = sig
                        break

            for el in gauge_els:
                txt = el.inner_text().strip()
                if txt in ["Strong Buy", "Buy", "Neutral", "Sell", "Strong Sell"]:
                    summary = txt
                    break

            # Try to extract indicator counts from the counters
            counter_els = page.query_selector_all("[class*='counterNumber']")
            if not counter_els:
                counter_els = page.query_selector_all("[class*='counter']")

            counts = []
            for el in counter_els:
                txt = el.inner_text().strip()
                if txt.isdigit():
                    counts.append(int(txt))

            # TradingView shows: Sell count, Neutral count, Buy count
            if len(counts) >= 3:
                sell_count = counts[0]
                neutral_count = counts[1]
                buy_count = counts[2]

            uid = f"tv_{tv['symbol']}_daily"
            conn.execute(
                "INSERT OR REPLACE INTO technicals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, "TradingView", tv["symbol"], "daily",
                 summary, buy_count, sell_count, neutral_count,
                 "", 0, 0, 0, "", 0, 0, 0, now))

            results.append({
                "source": "TradingView", "symbol": tv["symbol"],
                "timeframe": "daily", "summary": summary,
                "buy": buy_count, "sell": sell_count, "neutral": neutral_count
            })
            print(f"    {tv['symbol']}: {summary} (Buy:{buy_count} Sell:{sell_count} Neut:{neutral_count})")

        except Exception as e:
            print(f"    TradingView {tv['symbol']} error: {e}")

    conn.commit()
    return results


# ============================================================
# 2. Investing.com Multi-Timeframe Technicals
# ============================================================
def scrape_investing_technicals(page, conn):
    """Scrape Investing.com technical analysis for all timeframes"""
    print("  [Playwright] Scraping Investing.com technicals (all timeframes)...")
    results = []
    now = datetime.now().isoformat()

    for inv in INVESTING_TECHNICALS:
        try:
            page.goto(inv["url"], wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(3000)

            # Close cookie consent
            try:
                btn = page.query_selector("#onetrust-accept-btn-handler")
                if btn and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(500)
            except Exception:
                pass

            # For each timeframe, click the timeframe button and read the summary
            tf_labels = {"5": "5m", "15": "15m", "60": "1h", "300": "daily", "week": "weekly"}

            for tf_val in INVESTING_TIMEFRAMES:
                try:
                    # Click the timeframe selector
                    tf_btn = page.query_selector(f"a[data-period='{tf_val}'], [data-value='{tf_val}']")
                    if tf_btn:
                        tf_btn.click()
                        page.wait_for_timeout(1500)

                    # Read the summary text (Strong Buy, Buy, Neutral, Sell, Strong Sell)
                    summary = "Neutral"
                    buy_count = sell_count = neutral_count = 0

                    # Look for summary text in the technicals summary area
                    summary_el = page.query_selector("[class*='techSummary'] span, [class*='summary'] .buy, [class*='summary'] .sell, .techStudiesWidget .summary")
                    if summary_el:
                        summary = summary_el.inner_text().strip()

                    if summary == "Neutral":
                        # Fallback: search page for recommendation
                        tech_area = page.query_selector("[class*='technicalSummary'], [class*='techStudies'], #TextAnalysis")
                        if tech_area:
                            text = tech_area.inner_text()
                            for sig in ["Strong Buy", "Strong Sell", "Buy", "Sell", "Neutral"]:
                                if sig in text:
                                    summary = sig
                                    break

                    # Extract MA and Oscillator summary tables
                    ma_summary = ""
                    osc_summary = ""
                    ma_buy = ma_sell = ma_neutral = 0
                    osc_buy = osc_sell = osc_neutral = 0

                    tables = page.query_selector_all("table")
                    for table in tables:
                        header = table.query_selector("th, thead")
                        if header:
                            h_text = header.inner_text().lower()
                            rows = table.query_selector_all("tr")
                            buy_c = sell_c = neut_c = 0
                            for row in rows:
                                cells = row.query_selector_all("td")
                                for cell in cells:
                                    ct = cell.inner_text().strip().lower()
                                    if ct == "buy":
                                        buy_c += 1
                                    elif ct == "sell":
                                        sell_c += 1
                                    elif ct == "neutral":
                                        neut_c += 1

                            if "moving" in h_text:
                                ma_buy, ma_sell, ma_neutral = buy_c, sell_c, neut_c
                                ma_summary = "Buy" if ma_buy > ma_sell else "Sell" if ma_sell > ma_buy else "Neutral"
                            elif "oscill" in h_text:
                                osc_buy, osc_sell, osc_neutral = buy_c, sell_c, neut_c
                                osc_summary = "Buy" if osc_buy > osc_sell else "Sell" if osc_sell > osc_buy else "Neutral"

                    tf_label = tf_labels.get(tf_val, tf_val)
                    uid = f"inv_{inv['symbol']}_{tf_label}"

                    # Total counts
                    total_buy = buy_count or (ma_buy + osc_buy)
                    total_sell = sell_count or (ma_sell + osc_sell)
                    total_neutral = neutral_count or (ma_neutral + osc_neutral)

                    conn.execute(
                        "INSERT OR REPLACE INTO technicals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (uid, "Investing.com", inv["symbol"], tf_label,
                         summary, total_buy, total_sell, total_neutral,
                         ma_summary, ma_buy, ma_sell, ma_neutral,
                         osc_summary, osc_buy, osc_sell, osc_neutral, now))

                    results.append({
                        "source": "Investing.com", "symbol": inv["symbol"],
                        "timeframe": tf_label, "summary": summary,
                        "buy": total_buy, "sell": total_sell, "neutral": total_neutral
                    })

                except Exception as e:
                    print(f"    Investing.com {inv['symbol']} {tf_val} error: {e}")

            print(f"    {inv['symbol']}: scraped {len(INVESTING_TIMEFRAMES)} timeframes")

        except Exception as e:
            print(f"    Investing.com {inv['symbol']} error: {e}")

    conn.commit()
    return results


# ============================================================
# 3. CBOE Put/Call Ratio
# ============================================================
def scrape_cboe_putcall(page, conn):
    """Scrape CBOE daily market statistics for put/call ratio"""
    print("  [Playwright] Scraping CBOE Put/Call ratio...")
    result = None
    now = datetime.now().isoformat()

    try:
        page.goto("https://www.cboe.com/us/options/market_statistics/daily/", wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(3000)

        # Look for the put/call ratio data in the page
        content = page.inner_text("body")

        # Try to extract ratios from the page text
        equity_ratio = None
        index_ratio = None
        total_ratio = None

        # CBOE shows ratios in table format
        tables = page.query_selector_all("table")
        for table in tables:
            text = table.inner_text().lower()
            if "put/call" in text or "put call" in text or "p/c ratio" in text:
                rows = table.query_selector_all("tr")
                for row in rows:
                    cells = row.query_selector_all("td, th")
                    cell_texts = [c.inner_text().strip() for c in cells]
                    for i, ct in enumerate(cell_texts):
                        ct_lower = ct.lower()
                        if "equity" in ct_lower and i + 1 < len(cell_texts):
                            try:
                                equity_ratio = float(cell_texts[i + 1])
                            except ValueError:
                                pass
                        elif "index" in ct_lower and "equity" not in ct_lower and i + 1 < len(cell_texts):
                            try:
                                index_ratio = float(cell_texts[i + 1])
                            except ValueError:
                                pass
                        elif "total" in ct_lower and i + 1 < len(cell_texts):
                            try:
                                total_ratio = float(cell_texts[i + 1])
                            except ValueError:
                                pass

        # Fallback: try regex on full page text
        if total_ratio is None:
            m = re.search(r'(?:total|overall).*?(\d+\.\d+)', content, re.IGNORECASE)
            if m:
                total_ratio = float(m.group(1))
        if equity_ratio is None:
            m = re.search(r'equity.*?put.*?call.*?(\d+\.\d+)', content, re.IGNORECASE)
            if m:
                equity_ratio = float(m.group(1))

        if equity_ratio or total_ratio:
            today = datetime.now().strftime("%Y-%m-%d")
            conn.execute(
                "INSERT INTO putcall (date, equity_ratio, index_ratio, total_ratio, exchange_volume, fetched_at) VALUES (?,?,?,?,?,?)",
                (today, equity_ratio, index_ratio, total_ratio, "", now))
            conn.commit()
            result = {"equity": equity_ratio, "index": index_ratio, "total": total_ratio}
            print(f"    Put/Call — Equity: {equity_ratio}, Index: {index_ratio}, Total: {total_ratio}")
        else:
            print("    Could not extract put/call ratio")

    except Exception as e:
        print(f"    CBOE error: {e}")

    return result


# ============================================================
# 4. Finviz — Futures, Sectors, and Ticker News
# ============================================================
def scrape_finviz(page, conn):
    """Scrape Finviz for futures and sector performance"""
    print("  [Playwright] Scraping Finviz futures & sectors...")
    results = {"futures": [], "sectors": []}
    now = datetime.now().isoformat()

    try:
        page.goto("https://finviz.com/", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)

        # --- Futures ---
        futures_rows = page.query_selector_all('tr:has(a[href*="futures"])')
        for row in futures_rows:
            cells = row.query_selector_all("td")
            if len(cells) >= 4:
                name = cells[0].inner_text().strip()
                last_text = cells[1].inner_text().strip().replace(",", "")
                change_text = cells[3].inner_text().strip().replace("%", "")
                if name and last_text and change_text:
                    try:
                        last_val = float(last_text)
                        change_val = float(change_text)
                        conn.execute(
                            "INSERT OR REPLACE INTO futures VALUES (?,?,?,?)",
                            (name, last_val, change_val, now))
                        results["futures"].append({"name": name, "last": last_val, "change_pct": change_val})
                    except ValueError:
                        pass

        # --- Sector Performance ---
        page.goto("https://finviz.com/groups.ashx?g=sector&v=110&o=-perf1w", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)

        try:
            close_btn = page.query_selector("button[aria-label='Close'], .modal-close, [class*='close']")
            if close_btn and close_btn.is_visible():
                close_btn.click()
                page.wait_for_timeout(500)
        except Exception:
            pass

        sector_rows = page.query_selector_all("tr.styled-row")
        for row in sector_rows:
            cells = row.query_selector_all("td")
            if len(cells) >= 13:
                name_el = cells[1].query_selector("a")
                sector_name = name_el.inner_text().strip() if name_el else cells[1].inner_text().strip()
                perf_text = cells[12].inner_text().strip().replace("%", "")
                if sector_name and perf_text:
                    try:
                        perf_val = float(perf_text)
                        conn.execute(
                            "INSERT OR REPLACE INTO sectors VALUES (?,?,?)",
                            (sector_name, perf_val, now))
                        results["sectors"].append({"name": sector_name, "change_pct": perf_val})
                    except ValueError:
                        pass

        conn.commit()
        print(f"    Futures: {len(results['futures'])} items")
        print(f"    Sectors: {len(results['sectors'])} items")
    except Exception as e:
        print(f"    Finviz error: {e}")

    return results


def scrape_finviz_news(page, conn):
    """Scrape Finviz ticker-specific news for key stocks"""
    print(f"  [Playwright] Scraping Finviz news for {len(FINVIZ_TICKERS)} tickers...")
    all_articles = []
    now = datetime.now().isoformat()

    for ticker in FINVIZ_TICKERS:
        try:
            page.goto(f"https://finviz.com/quote.ashx?t={ticker}", wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)

            # Find the news table (id="news-table")
            news_table = page.query_selector("#news-table")
            if not news_table:
                print(f"    {ticker}: no news table found")
                continue

            rows = news_table.query_selector_all("tr")
            count = 0
            for row in rows[:10]:  # Top 10 per ticker
                try:
                    link = row.query_selector("a")
                    time_el = row.query_selector("td:first-child")
                    if link:
                        headline = link.inner_text().strip()
                        href = link.get_attribute("href") or ""
                        time_str = time_el.inner_text().strip() if time_el else ""

                        if headline and len(headline) > 10:
                            uid = hashlib.md5(f"{ticker}{headline}".encode()).hexdigest()[:12]
                            conn.execute(
                                "INSERT OR REPLACE INTO finviz_news VALUES (?,?,?,?,?,?,?)",
                                (uid, ticker, headline, "Finviz", href, time_str, now))

                            # Also add to main news table for Qwen scoring
                            conn.execute(
                                "INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?)",
                                (uid, headline, f"Finviz/{ticker}", href, "", now))

                            all_articles.append({"ticker": ticker, "headline": headline})
                            count += 1
                except Exception:
                    continue

            if count > 0:
                print(f"    {ticker}: {count} headlines")

        except Exception as e:
            print(f"    {ticker} error: {e}")

    conn.commit()
    print(f"    Total: {len(all_articles)} headlines from {len(FINVIZ_TICKERS)} tickers")
    return all_articles


# ============================================================
# 5. MarketWatch Headlines (existing)
# ============================================================
def scrape_marketwatch_headlines(page, conn):
    """Scrape MarketWatch for additional headlines"""
    print("  [Playwright] Scraping MarketWatch headlines...")
    articles = []

    try:
        page.goto("https://www.marketwatch.com/latest-news", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)

        headline_els = page.query_selector_all("a.link[href*='story']")
        if not headline_els:
            headline_els = page.query_selector_all("h3 a, h2 a")
        if not headline_els:
            headline_els = page.query_selector_all("div.article__content a.link")
        if not headline_els:
            page.goto("https://www.marketwatch.com/", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
            headline_els = page.query_selector_all("h3 a[href*='story'], a.link[href*='story']")

        seen_titles = set()
        for el in headline_els[:25]:
            title = el.inner_text().strip()
            href = el.get_attribute("href") or ""
            if title and len(title) > 15 and title not in seen_titles:
                seen_titles.add(title)
                uid = hashlib.md5(title.encode()).hexdigest()[:12]
                articles.append({
                    "id": uid,
                    "headline": title,
                    "source": "MarketWatch",
                    "url": href if href.startswith("http") else f"https://www.marketwatch.com{href}",
                })

        now = datetime.now().isoformat()
        saved = 0
        for a in articles[:15]:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?)",
                    (a["id"], a["headline"], a["source"], a["url"], "", now))
                saved += 1
            except Exception:
                pass
        conn.commit()
        print(f"    MarketWatch: {saved} new headlines")
    except Exception as e:
        print(f"    MarketWatch error: {e}")

    return articles


import requests
from bs4 import BeautifulSoup

def scrape_economic_calendar(page, conn):
    """Scrape ForexFactory economic calendar for upcoming events using requests (bypasses Playwright block)"""
    print("  [Playwright] Scraping ForexFactory economic calendar via Requests...")
    events = []

    try:
        r = requests.get("https://www.forexfactory.com/calendar?month=this", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        soup = BeautifulSoup(r.text, "html.parser")
        
        rows = soup.select("table.calendar__table tr.calendar__row")
        current_date_str = ""
        current_year = datetime.now().year

        for row in rows:
            try:
                date_el = row.select_one("td.calendar__date")
                if date_el:
                    d_text = date_el.text.strip()
                    if d_text: 
                        raw_date = d_text.split("\n")[0].strip() # e.g. "Mon May 12" -> "May 12"
                        if len(raw_date.split(" ")) >= 2:
                            month_day = " ".join(raw_date.split(" ")[1:]) # "May 12"
                            try:
                                parsed = datetime.strptime(f"{current_year} {month_day}", "%Y %b %d")
                                current_date_str = parsed.strftime("%Y-%m-%d")
                            except Exception:
                                current_date_str = raw_date
                
                if not current_date_str:
                    continue
                
                time_el = row.select_one("td.calendar__time")
                currency_el = row.select_one("td.calendar__currency")
                impact_el = row.select_one("td.calendar__impact span")
                event_el = row.select_one("td.calendar__event")
                actual_el = row.select_one("td.calendar__actual")
                forecast_el = row.select_one("td.calendar__forecast")
                previous_el = row.select_one("td.calendar__previous")

                event_name = event_el.text.strip() if event_el else ""
                if not event_name:
                    continue

                event_time = time_el.text.strip() if time_el else ""
                currency = currency_el.text.strip() if currency_el else ""
                actual = actual_el.text.strip() if actual_el else ""
                forecast = forecast_el.text.strip() if forecast_el else ""
                previous = previous_el.text.strip() if previous_el else ""

                impact_label = "low"
                if impact_el:
                    cls = impact_el.get("class", [])
                    if "icon--ff-impact-red" in cls: impact_label = "high"
                    elif "icon--ff-impact-ora" in cls: impact_label = "medium"

                # Save all events
                uid = hashlib.md5(f"{current_date_str}{event_time}{event_name}".encode()).hexdigest()[:12]
                now = datetime.now().isoformat()
                
                events.append({
                    "id": uid, "date": current_date_str, "time": event_time,
                    "currency": currency, "impact": impact_label,
                    "event": event_name, "actual": actual,
                    "forecast": forecast, "previous": previous
                })
                conn.execute(
                    "INSERT OR REPLACE INTO econ_calendar VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uid, current_date_str, event_time, currency, impact_label,
                     event_name, actual, forecast, previous, now))
            except Exception:
                continue

        conn.commit()
        high_impact = [e for e in events if e["impact"] == "high"]
        print(f"    Calendar: {len(events)} events ({len(high_impact)} high-impact)")
    except Exception as e:
        print(f"    Economic calendar error: {e}")

    return events


# ============================================================
# 8. Truth Social
# ============================================================
def scrape_truth_social(page, conn):
    """Scrape Truth Social posts using Playwright API interception"""
    print("  [Playwright] Scraping Truth Social...")
    handles = ["realDonaldTrump"]
    all_truths = []
    
    for handle in handles:
        try:
            print(f"    Navigating to @{handle}...")
            # We must use a new page to easily attach a one-time response handler
            new_page = page.context.new_page()
            
            intercepted_data = []
            def handle_response(response):
                if "statuses" in response.url and response.status == 200:
                    try:
                        data = response.json()
                        if isinstance(data, list) and len(data) > 0:
                            intercepted_data.extend(data)
                    except:
                        pass
            
            new_page.on("response", handle_response)
            new_page.goto(f"https://truthsocial.com/@{handle}", wait_until="networkidle", timeout=20000)
            
            # Wait a bit for the API call to finish
            new_page.wait_for_timeout(3000)
            new_page.close()
            
            now = datetime.now().isoformat()
            
            for post in intercepted_data:
                tid = post.get("id")
                if not tid: continue
                
                # HTML content is provided, so we strip it to get raw text
                raw_content = post.get("content", "")
                from bs4 import BeautifulSoup
                text = BeautifulSoup(raw_content, "html.parser").get_text() if raw_content else ""
                
                likes = post.get("favourites_count", 0)
                rts = post.get("reblogs_count", 0)
                created_at = post.get("created_at", "")
                
                # Save to political_socials table (which is used by our Socials tab)
                # Note: This shares the table with Twitter syndication
                conn.execute(
                    "INSERT OR IGNORE INTO political_socials (id, author, handle, text, likes, retweets, created_at, fetched_at, qwen_sentiment) VALUES (?,?,?,?,?,?,?,?,?)",
                    (tid, f"Truth Social: {handle}", handle, text, likes, rts, created_at, now, "UNSCORED")
                )
                all_truths.append(tid)
            
            conn.commit()
            print(f"    Saved {len(intercepted_data)} Truths from @{handle}")
        except Exception as e:
            print(f"    Error scraping @{handle}: {e}")
            
    return all_truths


# ============================================================
# Main runner
# ============================================================
def run_all():
    """Run all Playwright scrapers in a single browser session"""
    conn = init_db()
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Starting Playwright scrapers...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # 1. TradingView technicals
        tv = scrape_tradingview_technicals(page, conn)

        # 2. Investing.com technicals (all timeframes)
        inv = scrape_investing_technicals(page, conn)

        # 3. CBOE Put/Call
        pc = scrape_cboe_putcall(page, conn)

        # 4. Finviz futures + sectors
        fv = scrape_finviz(page, conn)

        # 5. Finviz ticker news
        fn = scrape_finviz_news(page, conn)

        # 6. MarketWatch headlines
        mw = scrape_marketwatch_headlines(page, conn)

        # 7. Economic calendar
        cal = scrape_economic_calendar(page, conn)

        # 8. Truth Social
        ts = scrape_truth_social(page, conn)

        browser.close()

    conn.close()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Playwright scrapers complete.\n")

    return {
        "tradingview_technicals": len(tv),
        "investing_technicals": len(inv),
        "cboe_putcall": pc,
        "finviz_futures": len(fv.get("futures", [])),
        "finviz_sectors": len(fv.get("sectors", [])),
        "finviz_news": len(fn),
        "marketwatch": len(mw),
        "economic_calendar": len(cal),
    }


if __name__ == "__main__":
    results = run_all()
    print(json.dumps(results, indent=2, default=str))
