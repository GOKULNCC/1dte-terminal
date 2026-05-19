"""
Free market data scraper — replaces Claude Haiku web_search
Fetches: world indices, news headlines, political posts
Runs every 15 minutes via scheduler
"""
import json, time, hashlib, requests
from datetime import datetime
from pathlib import Path

# --- Yahoo Finance (free, no API key) ---
import yfinance as yf

# --- RSS feeds (free) ---
import feedparser

from config import db_connect

# World indices Yahoo tickers
INDICES = {
    "^N225": "Nikkei 225", "^HSI": "Hang Seng", "000001.SS": "Shanghai Composite",
    "^BSESN": "SENSEX", "^AXJO": "ASX 200", "^KS11": "KOSPI",
    "^GDAXI": "DAX", "^FTSE": "FTSE 100", "^FCHI": "CAC 40", "^STOXX50E": "EURO STOXX 50",
    "^GSPC": "S&P 500", "^IXIC": "NASDAQ", "^DJI": "Dow Jones", "^RUT": "Russell 2000",
    "^VIX": "VIX",
}

# Free RSS news feeds
NEWS_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # CNBC Top News
    "https://feeds.reuters.com/reuters/businessNews",  # Reuters Business
    "https://www.ft.com/?format=rss",  # FT (partial)
]


def init_db():
    conn = db_connect()
    conn.execute("""CREATE TABLE IF NOT EXISTS indices (
        symbol TEXT, name TEXT, price REAL, change_pct REAL, 
        fetched_at TEXT, PRIMARY KEY (symbol))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS news (
        id TEXT PRIMARY KEY, headline TEXT, source TEXT, url TEXT,
        published TEXT, fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS scored_news (
        id TEXT PRIMARY KEY, headline TEXT, source TEXT, sentiment REAL,
        category TEXT, crash_relevance REAL, political INTEGER, author TEXT,
        gemma_note TEXT, scored_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, crash_prob REAL, 
        primary_driver TEXT, top_risks TEXT, confidence TEXT,
        model TEXT, created_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fear_greed (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        score INTEGER, label TEXT, previous_close INTEGER,
        one_week_ago INTEGER, one_month_ago INTEGER,
        fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS earnings (
        ticker TEXT PRIMARY KEY,
        company TEXT,
        earnings_date TEXT,
        timing TEXT,
        eps_estimate REAL,
        eps_low REAL,
        eps_high REAL,
        revenue_estimate REAL,
        fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS earnings_history (
        id TEXT PRIMARY KEY,
        ticker TEXT,
        date TEXT,
        eps_estimate REAL,
        eps_actual REAL,
        surprise_pct REAL,
        fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS political_socials (
        id TEXT PRIMARY KEY,
        author TEXT,
        handle TEXT,
        text TEXT,
        likes INTEGER,
        retweets INTEGER,
        created_at TEXT,
        fetched_at TEXT,
        qwen_sentiment TEXT DEFAULT 'UNSCORED')""")
    conn.commit()
    return conn


def fetch_indices():
    """Fetch world index prices via Yahoo Finance (free, no API key)"""
    results = []
    for ticker, name in INDICES.items():
        try:
            t = yf.Ticker(ticker)
            info = t.fast_info
            price = info.last_price
            prev = info.previous_close
            pct = ((price - prev) / prev * 100) if prev else 0
            results.append({"symbol": ticker, "name": name, "price": round(price, 2), "change_pct": round(pct, 2)})
        except Exception as e:
            print(f"  Skip {name}: {e}")
    return results


def fetch_news():
    """Fetch headlines from RSS feeds (free, no API key)"""
    articles = []
    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                uid = hashlib.md5(entry.get("title", "").encode()).hexdigest()[:12]
                articles.append({
                    "id": uid,
                    "headline": entry.get("title", ""),
                    "source": feed.feed.get("title", "Unknown"),
                    "url": entry.get("link", ""),
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            print(f"  Feed error: {e}")
    # Deduplicate
    seen = set()
    unique = []
    for a in articles:
        if a["headline"] not in seen:
            seen.add(a["headline"])
            unique.append(a)
    return unique[:20]  # Top 20 headlines


def save_indices(conn, indices):
    now = datetime.now().isoformat()
    for idx in indices:
        conn.execute("INSERT OR REPLACE INTO indices VALUES (?,?,?,?,?)",
                     (idx["symbol"], idx["name"], idx["price"], idx["change_pct"], now))
    conn.commit()
    print(f"  Saved {len(indices)} indices")


def save_news(conn, articles):
    now = datetime.now().isoformat()
    for a in articles:
        conn.execute("INSERT OR REPLACE INTO news VALUES (?,?,?,?,?,?)",
                     (a["id"], a["headline"], a["source"], a["url"], a["published"], now))
    conn.commit()
    print(f"  Saved {len(articles)} headlines")


def fetch_fear_greed_api():
    """Fetch CNN Fear & Greed via direct JSON API (faster than Playwright)"""
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        fg = data.get("fear_and_greed", {})
        score = int(fg.get("score", 0))
        label = fg.get("rating", "")
        prev = int(fg.get("previous_close", 0)) if fg.get("previous_close") else None
        week = int(fg.get("previous_1_week", 0)) if fg.get("previous_1_week") else None
        month = int(fg.get("previous_1_month", 0)) if fg.get("previous_1_month") else None
        return {"score": score, "label": label, "previous_close": prev,
                "one_week_ago": week, "one_month_ago": month}
    except Exception as e:
        print(f"  Fear & Greed API error: {e}")
        return None


def save_fear_greed(conn, fg):
    if not fg:
        return
    now = datetime.now().isoformat()
    conn.execute("INSERT INTO fear_greed (score, label, previous_close, one_week_ago, one_month_ago, fetched_at) VALUES (?,?,?,?,?,?)",
                 (fg["score"], fg["label"], fg.get("previous_close"), fg.get("one_week_ago"), fg.get("one_month_ago"), now))
    conn.commit()
    print(f"  Fear & Greed: {fg['score']} ({fg['label']})")


# ============================================================
# Earnings Data (via yfinance — no API key needed)
# ============================================================
EARNINGS_TICKERS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRK-B",
    "AVGO", "LLY", "JPM", "V", "UNH", "MA", "XOM", "COST",
    "HD", "PG", "JNJ", "NFLX"
]

# Map tickers to friendly company names
TICKER_NAMES = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "GOOGL": "Alphabet",
    "AMZN": "Amazon", "META": "Meta", "TSLA": "Tesla", "BRK-B": "Berkshire",
    "AVGO": "Broadcom", "LLY": "Eli Lilly", "JPM": "JPMorgan", "V": "Visa",
    "UNH": "UnitedHealth", "MA": "Mastercard", "XOM": "ExxonMobil", "COST": "Costco",
    "HD": "Home Depot", "PG": "P&G", "JNJ": "J&J", "NFLX": "Netflix"
}


def fetch_earnings():
    """Fetch upcoming earnings dates and historical EPS via yfinance"""
    results = []
    history = []
    now = datetime.now().isoformat()

    for ticker in EARNINGS_TICKERS:
        try:
            t = yf.Ticker(ticker)

            # --- Upcoming earnings date + estimates ---
            cal = t.calendar
            if cal and "Earnings Date" in cal:
                dates = cal["Earnings Date"]
                ed = dates[0] if isinstance(dates, list) else dates
                earnings_date = ed.isoformat() if hasattr(ed, 'isoformat') else str(ed)

                # Determine timing from the hour (16:00 = after close, <12:00 = before open)
                timing = "TBD"
                try:
                    ed_df = t.get_earnings_dates()
                    if ed_df is not None and len(ed_df) > 0:
                        first_date = ed_df.index[0]
                        hour = first_date.hour
                        if hour >= 16:
                            timing = "AMC"  # After Market Close
                        elif hour <= 12:
                            timing = "BMO"  # Before Market Open
                except Exception:
                    pass

                results.append({
                    "ticker": ticker,
                    "company": TICKER_NAMES.get(ticker, ticker),
                    "earnings_date": earnings_date,
                    "timing": timing,
                    "eps_estimate": cal.get("Earnings Average"),
                    "eps_low": cal.get("Earnings Low"),
                    "eps_high": cal.get("Earnings High"),
                    "revenue_estimate": cal.get("Revenue Average"),
                })

            # --- Historical EPS surprises ---
            try:
                ed_df = t.get_earnings_dates()
                if ed_df is not None:
                    for idx, row in ed_df.head(8).iterrows():
                        date_str = idx.strftime("%Y-%m-%d")
                        eps_est = row.get("EPS Estimate")
                        eps_act = row.get("Reported EPS")
                        surprise = row.get("Surprise(%)")

                        # Skip future dates with no actual EPS
                        if eps_act is not None and str(eps_act) != 'nan':
                            uid = f"{ticker}_{date_str}"
                            history.append({
                                "id": uid,
                                "ticker": ticker,
                                "date": date_str,
                                "eps_estimate": float(eps_est) if eps_est is not None and str(eps_est) != 'nan' else None,
                                "eps_actual": float(eps_act),
                                "surprise_pct": float(surprise) if surprise is not None and str(surprise) != 'nan' else None,
                            })
            except Exception:
                pass

            print(f"  {ticker}: {results[-1]['earnings_date'] if results else '?'}")

        except Exception as e:
            print(f"  {ticker}: error - {e}")

    return results, history


def fetch_political_socials():
    """Fetch tweets from key political accounts via Twitter Syndication API"""
    print("[Python] Fetching political social catalysts...")
    handles = ["realDonaldTrump", "elonmusk", "DanScavino"]
    results = []
    
    for handle in handles:
        try:
            r = requests.get(f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}", headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200: continue
            
            # Extract Next.js data
            if 'id="__NEXT_DATA__"' in r.text:
                data_json = r.text.split('id="__NEXT_DATA__" type="application/json">')[1].split('</script>')[0]
                j = json.loads(data_json)
                tweets = j.get("props", {}).get("pageProps", {}).get("timeline", {}).get("entries", [])
                
                for t in tweets:
                    tweet = t.get("content", {}).get("tweet", {})
                    if not tweet: continue
                    
                    text = tweet.get("text", "")
                    created_at = tweet.get("created_at", "")
                    user = tweet.get("user", {})
                    author = user.get("name", handle)
                    likes = tweet.get("favorite_count", 0)
                    rts = tweet.get("retweet_count", 0)
                    tid = tweet.get("id_str", "")
                    
                    if not tid: continue
                    
                    results.append({
                        "id": tid,
                        "author": author,
                        "handle": handle,
                        "text": text,
                        "likes": likes,
                        "retweets": rts,
                        "created_at": created_at
                    })
        except Exception as e:
            print(f"  Error fetching {handle}: {e}")
            
    print(f"  Fetched {len(results)} posts.")
    return results


def save_earnings(conn, results, history):
    """Save earnings data to DB"""
    now = datetime.now().isoformat()

    for r in results:
        conn.execute(
            "INSERT OR REPLACE INTO earnings VALUES (?,?,?,?,?,?,?,?,?)",
            (r["ticker"], r["company"], r["earnings_date"], r["timing"],
             r["eps_estimate"], r["eps_low"], r["eps_high"],
             r["revenue_estimate"], now))

    for h in history:
        conn.execute(
            "INSERT OR REPLACE INTO earnings_history VALUES (?,?,?,?,?,?,?)",
            (h["id"], h["ticker"], h["date"],
             h["eps_estimate"], h["eps_actual"], h["surprise_pct"], now))

    conn.commit()
    print(f"  Saved {len(results)} upcoming earnings, {len(history)} historical quarters")


if __name__ == "__main__":
    conn = init_db()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching indices...")
    indices = fetch_indices()
    save_indices(conn, indices)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching news...")
    news = fetch_news()
    save_news(conn, news)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching Fear & Greed (JSON API)...")
    fg = fetch_fear_greed_api()
    save_fear_greed(conn, fg)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching earnings (yfinance, {len(EARNINGS_TICKERS)} tickers)...")
    earnings, earnings_hist = fetch_earnings()
    save_earnings(conn, earnings, earnings_hist)
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching political socials...")
    socials = fetch_political_socials()
    now = datetime.now().isoformat()
    for s in socials:
        conn.execute(
            "INSERT OR REPLACE INTO political_socials (id, author, handle, text, likes, retweets, created_at, fetched_at, qwen_sentiment) VALUES (?,?,?,?,?,?,?,?,?)",
            (s["id"], s["author"], s["handle"], s["text"], s["likes"], s["retweets"], s["created_at"], now, "UNSCORED")
        )
    conn.commit()
    
    conn.close()
    print("Done. Run qwen_analyzer.py next to score with Qwen 3.")
