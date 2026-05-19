# 1-DTE Credit Spread Trading Assistant — Local Setup
## Qwen 3.6-27B + Python Scraper + React Dashboard
## Zero API Costs. Runs entirely on your laptop.

---

## Your Hardware
- **RAM:** 64GB
- **GPU:** NVIDIA RTX 5090 (32GB VRAM)
- **Model:** Qwen 3.6-27B Dense (17GB at Q4, fits easily in your 32GB VRAM)

This is a beast setup. You can run the best open-source model available (GPQA 87.8%, SWE-bench 77.2%) with room to spare. No compromises needed.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 YOUR LAPTOP                      │
│                                                  │
│  ┌──────────────┐    ┌───────────────────────┐  │
│  │ Python       │───>│ Ollama / vLLM          │  │
│  │ Scraper      │    │ Qwen 3.6-27B           │  │
│  │ (Yahoo/RSS)  │    │ on RTX 5090            │  │
│  │ FREE data    │    │ GPQA 87.8%             │  │
│  └──────┬───────┘    │ 262K context           │  │
│         │            │ /think mode            │  │
│         v            └───────────┬────────────┘  │
│  ┌──────────────┐               │               │
│  │ SQLite DB    │<──────────────┘               │
│  │ All data     │                               │
│  └──────┬───────┘                               │
│         │                                        │
│         v                                        │
│  ┌──────────────┐                               │
│  │ React Web    │  http://localhost:3000         │
│  │ Dashboard    │  Same mobile-style UI          │
│  └──────────────┘                               │
│                                                  │
│  Monthly cost: $0.00                             │
└─────────────────────────────────────────────────┘
```

---

## Step 1: Install Ollama + Qwen 3.6-27B

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull Qwen 3.6-27B (will use your RTX 5090 automatically)
ollama pull qwen3.6:27b

# Test it
ollama run qwen3.6:27b "What is a credit spread?"
```

The model is ~17GB at Q4 quantization. With your 32GB VRAM, it loads entirely into GPU memory. Expected speed: ~40-60 tokens/second.

**Alternatively, for maximum performance with vLLM:**
```bash
pip install vllm
vllm serve Qwen/Qwen3.6-27B --port 11434 --max-model-len 65536 --reasoning-parser qwen3
```

---

## Step 2: Python Scraper (replaces Claude Haiku)

Create `scraper.py`:

```python
"""
Free market data scraper — replaces Claude Haiku web_search
Fetches: world indices, news headlines, political posts
Runs every 15 minutes via scheduler
"""
import json, time, sqlite3, hashlib
from datetime import datetime
from pathlib import Path

# --- Yahoo Finance (free, no API key) ---
import yfinance as yf

# --- RSS feeds (free) ---
import feedparser

DB_PATH = "trading.db"

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
    conn = sqlite3.connect(DB_PATH)
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


if __name__ == "__main__":
    conn = init_db()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching indices...")
    indices = fetch_indices()
    save_indices(conn, indices)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching news...")
    news = fetch_news()
    save_news(conn, news)
    conn.close()
    print("Done. Run qwen_analyzer.py next to score with Qwen 3.6.")
```

Install dependencies:
```bash
pip install yfinance feedparser
```

---

## Step 3: Qwen 3.6 Analyzer (replaces Gemma + Opus)

Create `qwen_analyzer.py`:

```python
"""
Qwen 3.6-27B local analyzer — replaces ALL Claude API calls
Runs on your RTX 5090 via Ollama (OpenAI-compatible API)
Handles: sentiment, crash probability, pattern matching, predictions
"""
import json, sqlite3, requests
from datetime import datetime

OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
MODEL = "qwen3.6:27b"
DB_PATH = "trading.db"


def ask_qwen(prompt, think=True):
    """Call Qwen 3.6 via Ollama OpenAI-compatible API"""
    messages = [{"role": "user", "content": prompt}]
    # Enable thinking mode for deep reasoning
    if think:
        messages[0]["content"] = "/think\n" + messages[0]["content"]
    
    resp = requests.post(OLLAMA_URL, json={
        "model": MODEL,
        "messages": messages,
        "temperature": 0.6,
        "top_p": 0.95,
        "max_tokens": 2000,
    })
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    
    # Extract thinking and answer
    thinking = ""
    answer = text
    if "<think>" in text and "</think>" in text:
        thinking = text.split("<think>")[1].split("</think>")[0].strip()
        answer = text.split("</think>")[1].strip()
    
    return {"thinking": thinking, "answer": answer, "full": text}


def extract_json(text):
    """Extract JSON from model output"""
    import re
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    m = re.search(r'[\[{].*[}\]]', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except:
            pass
    return None


def score_headlines():
    """Score all unscored headlines with Qwen 3.6"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    # Get unscored headlines
    rows = conn.execute("""
        SELECT n.id, n.headline, n.source FROM news n
        LEFT JOIN scored_news s ON n.id = s.id
        WHERE s.id IS NULL
        ORDER BY n.fetched_at DESC LIMIT 20
    """).fetchall()
    
    if not rows:
        print("  No new headlines to score")
        return []
    
    # Batch score all headlines in one call (efficient)
    headlines_text = "\n".join([f"{i+1}. [{r['source']}] {r['headline']}" for i, r in enumerate(rows)])
    
    result = ask_qwen(f"""Score these market news headlines for a 1-DTE credit spread trader (sells 2-3% OTM puts on SPX/NDX).

HEADLINES:
{headlines_text}

For each headline, return a JSON array:
[{{"id": 1, "sentiment": -1.0 to 1.0, "category": "TARIFF|GEOPOLITICAL|FED_HAWK|OIL_SHOCK|EARNINGS|INFLATION|TECH|POLITICAL|MARKET", "crash_relevance": 0.0 to 1.0, "political": true/false, "author": "name or null", "note": "one-line trading insight"}}]

Important: sentiment -1 = very bearish, +1 = very bullish. crash_relevance = how likely this could cause SPX >-2% move.
Return ONLY the JSON array.""", think=True)
    
    scored = extract_json(result["answer"])
    if not scored or not isinstance(scored, list):
        print("  Failed to parse Qwen output")
        return []
    
    now = datetime.now().isoformat()
    saved = []
    for i, s in enumerate(scored):
        if i >= len(rows):
            break
        row = rows[i]
        conn.execute("INSERT OR REPLACE INTO scored_news VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (row["id"], row["headline"], row["source"],
                      s.get("sentiment", 0), s.get("category", "MARKET"),
                      s.get("crash_relevance", 0), 1 if s.get("political") else 0,
                      s.get("author"), s.get("note", ""), now))
        saved.append({**dict(row), **s})
    
    conn.commit()
    print(f"  Scored {len(saved)} headlines with Qwen 3.6")
    print(f"  Thinking depth: {len(result['thinking'])} chars")
    
    conn.close()
    return saved


def predict_crash():
    """Run full crash prediction with deep thinking"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    # Get latest scored news
    news = conn.execute("""
        SELECT * FROM scored_news ORDER BY scored_at DESC LIMIT 15
    """).fetchall()
    
    # Get latest indices
    indices = conn.execute("SELECT * FROM indices ORDER BY fetched_at DESC").fetchall()
    
    news_text = "\n".join([
        f"[{r['sentiment']:+.2f} {r['category']}] {r['headline']}"
        + (f" (POLITICAL: {r['author']})" if r['political'] else "")
        for r in news
    ])
    
    idx_text = "\n".join([
        f"{r['name']}: {r['price']} ({r['change_pct']:+.2f}%)" for r in indices
    ])
    
    result = ask_qwen(f"""You are the world's most careful quantitative strategist. Predict the probability that SPX drops >2% or NDX drops >2.5% in the NEXT trading session.

HISTORICAL CONTEXT:
- SPX drops >2% on 3.4% of trading days (96 out of 2,812 days in 11 years)
- NDX drops >2.5% on 4.7% of days (84 out of 1,804 days)
- Known crash catalysts: COVID Mar 2020, Trump tariffs Apr/Oct 2025, hawkish Fed Dec 2024, Japan carry trade Aug 2024
- Worst crash: -11.98% SPX (Mar 16, 2020)

CURRENT WORLD INDICES:
{idx_text or "Not yet fetched"}

SCORED NEWS (Qwen sentiment):
{news_text or "No news yet"}

Think VERY deeply. Consider:
1. Is there genuine catalyst evidence, or is this a normal day?
2. What are the TOP 3 risk factors right now?
3. Base rate is only 3.4% — don't inflate without evidence
4. For political posts: is the language "considering" (delayed) or "effective immediately" (urgent)?
5. Build scenario tree: crash / selloff / dip / rally

Return JSON:
{{"crash_probability": 0-100, "confidence": "HIGH|MEDIUM|LOW", "primary_driver": "description", "top_risks": ["r1", "r2", "r3"], "scenarios": [{{"name": "scenario", "probability": 0-100, "spx_range": "X% to Y%"}}], "recommended_otm_spx": "X.XX%", "recommended_otm_ndx": "X.XX%", "action": "TRADE|WIDEN|SKIP", "verdict": "2-3 sentence summary"}}""", think=True)
    
    parsed = extract_json(result["answer"])
    if parsed:
        now = datetime.now().isoformat()
        conn.execute("INSERT INTO predictions (crash_prob, primary_driver, top_risks, confidence, model, created_at) VALUES (?,?,?,?,?,?)",
                     (parsed.get("crash_probability", 5),
                      parsed.get("primary_driver", ""),
                      json.dumps(parsed.get("top_risks", [])),
                      parsed.get("confidence", "LOW"),
                      "Qwen3.6-27B", now))
        conn.commit()
        print(f"\n{'='*50}")
        print(f"CRASH PROBABILITY: {parsed.get('crash_probability', '?')}%")
        print(f"CONFIDENCE: {parsed.get('confidence', '?')}")
        print(f"ACTION: {parsed.get('action', '?')}")
        print(f"SPX OTM: {parsed.get('recommended_otm_spx', '?')}")
        print(f"NDX OTM: {parsed.get('recommended_otm_ndx', '?')}")
        print(f"DRIVER: {parsed.get('primary_driver', '?')}")
        print(f"VERDICT: {parsed.get('verdict', '?')}")
        print(f"{'='*50}")
        print(f"\nThinking depth: {len(result['thinking'])} chars")
    
    conn.close()
    return parsed


if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scoring headlines with Qwen 3.6-27B...")
    score_headlines()
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Running crash prediction with deep thinking...")
    predict_crash()
```

---

## Step 4: API Server (feeds the React dashboard)

Create `server.py`:

```python
"""
Local API server — serves data to the React dashboard
Runs at http://localhost:8080
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, sqlite3, subprocess
from datetime import datetime

DB_PATH = "trading.db"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        if self.path == "/api/indices":
            rows = conn.execute("SELECT * FROM indices ORDER BY name").fetchall()
            self.wfile.write(json.dumps([dict(r) for r in rows]).encode())
        
        elif self.path == "/api/news":
            rows = conn.execute("""
                SELECT s.*, n.url FROM scored_news s
                JOIN news n ON s.id = n.id
                ORDER BY s.scored_at DESC LIMIT 20
            """).fetchall()
            self.wfile.write(json.dumps([dict(r) for r in rows]).encode())
        
        elif self.path == "/api/prediction":
            row = conn.execute("SELECT * FROM predictions ORDER BY created_at DESC LIMIT 1").fetchone()
            self.wfile.write(json.dumps(dict(row) if row else {}).encode())
        
        elif self.path == "/api/refresh":
            # Trigger scraper + analyzer
            subprocess.Popen(["python", "scraper.py"])
            subprocess.Popen(["python", "qwen_analyzer.py"])
            self.wfile.write(json.dumps({"status": "refreshing"}).encode())
        
        elif self.path == "/api/predict":
            subprocess.Popen(["python", "qwen_analyzer.py"])
            self.wfile.write(json.dumps({"status": "predicting"}).encode())
        
        else:
            self.wfile.write(json.dumps({"error": "unknown endpoint"}).encode())
        
        conn.close()
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    
    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


if __name__ == "__main__":
    print("Starting API server at http://localhost:8080")
    HTTPServer(("", 8080), Handler).serve_forever()
```

---

## Step 5: Run Everything

Open 3 terminals:

```bash
# Terminal 1: Ollama (Qwen model server)
ollama serve

# Terminal 2: API server
python server.py

# Terminal 3: Initial data fetch + analysis
python scraper.py && python qwen_analyzer.py
```

Then open `http://localhost:8080/api/indices` in your browser to verify data is flowing.

---

## Step 6: Scheduler (auto-refresh every 15 min)

Create `scheduler.py`:

```python
"""Auto-refresh every 15 minutes during market hours"""
import time, subprocess
from datetime import datetime

while True:
    now = datetime.now()
    hour = now.hour
    # Only during US market hours (9:30 AM - 4:00 PM ET, adjust for your timezone)
    if 6 <= hour <= 20:  # broad window
        print(f"\n[{now.strftime('%H:%M:%S')}] Auto-refresh cycle starting...")
        subprocess.run(["python", "scraper.py"])
        subprocess.run(["python", "qwen_analyzer.py"])
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Cycle complete. Next in 15 min.")
    else:
        print(f"[{now.strftime('%H:%M:%S')}] Outside market hours. Sleeping.")
    time.sleep(900)  # 15 minutes
```

---

## Model Comparison: Why Qwen 3.6-27B

| Benchmark | Qwen 3.6-27B | Claude Opus 4.6 | Gemma 4 E4B | Your gain |
|-----------|-------------|-----------------|-------------|-----------|
| GPQA Diamond | 87.8% | 91.3% | ~55% | Near-Opus quality! |
| SWE-bench | 77.2% | 80.8% | N/A | -3.6% vs Opus |
| Context | 262K tokens | 1M tokens | 128K | Plenty for your use |
| Thinking mode | Native /think | Extended thinking | Basic CoT | Deep reasoning |
| Speed on your GPU | ~50 tok/s | N/A (API) | ~20 tok/s (phone) | Fast |
| VRAM needed | 17GB Q4 | N/A | 5GB | Fits easily in 32GB |
| Monthly cost | **$0.00** | $28-46/mo | $0 (phone only) | **Saves $28-46/mo** |

---

## Monthly Cost: $0.00

| Component | Old (Claude) | New (Local) |
|-----------|-------------|-------------|
| Data fetching | $8/mo (Haiku) | $0 (Yahoo + RSS) |
| Sentiment scoring | $0 (Gemma on-device) | $0 (Qwen local) |
| Crash prediction | $23/mo (Opus) | $0 (Qwen local) |
| Event/news analysis | $0-22/mo | $0 (Qwen local) |
| Electricity | $0 | ~$5/mo (GPU power) |
| **TOTAL** | **$28-46/mo** | **~$5/mo electricity** |
