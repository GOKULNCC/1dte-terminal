# 1-DTE Terminal

A fully local desktop trading terminal for 0DTE long-premium options on SPX and NDX.
Uses a local LLM (Qwen 3-30B via Ollama) for crash probability predictions and news sentiment scoring — **no cloud APIs, no monthly fees**.

Runs as a native desktop window (`pywebview` wrapping a local React dashboard).

## Features

- **Crash probability** — Qwen 3-30B `/think` deep-reasoning prediction each morning
- **Position sizer** — Kelly criterion + live IBKR OPRA options chain
- **Live SPX/VIX strip** — real-time ticks via `ib_insync` (IBKR TWS/Gateway)
- **Multi-TF technicals** — TradingView + Investing.com signals (Playwright scraper)
- **News scoring** — RSS feeds scored by local LLM at zero cost
- **Economic calendar** — Investing.com via Playwright
- **Earnings window** — auto SKIP banner around high-IV events
- **Trade journal** — log entries, track P&L

## Stack

| Layer | Tech |
|---|---|
| Desktop shell | pywebview (native OS window) |
| Frontend | React 19 + Vite |
| Backend | Python — `server.py` (stdlib `ThreadingHTTPServer`) |
| LLM | Ollama — Qwen 3-30B Q4_K_M |
| Scrapers | yfinance, feedparser, Playwright |
| Live data | ib_insync (IBKR TWS/IB Gateway) |
| Storage | SQLite (`trading.db`) |

**Monthly cost: ~$5 electricity.** No API subscriptions required.

## Prerequisites

- **NVIDIA GPU** with 24 GB+ VRAM (tested on RTX 5090)
- **Ollama** — [ollama.com](https://ollama.com) — with `qwen3:30b` pulled
- **IBKR TWS or IB Gateway** running locally (for live options chain)
- Python 3.11+ and Node 20+
- Windows (pywebview + win32 used for single-instance mutex)

## Quick Start

### 1. Clone

```bash
git clone https://github.com/<your-user>/1dte-terminal.git
cd 1dte-terminal
```

### 2. Pull the LLM

```bash
ollama pull qwen3:30b
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 4. Install and build the dashboard

```bash
cd dashboard
npm install
npm run build   # produces dashboard/dist/ — served by pywebview
cd ..
```

### 5. Launch

Double-click `Start_Trading_Tool.bat`, or:

```bash
pythonw app.py
```

This opens a native desktop window, starts the API server on port 8088, and launches the scheduler + IBKR live tick streamer in the background.

### 6. Seed data (first run)

In a separate terminal while the app is running:

```bash
python scraper.py          # world indices + RSS news
python qwen_analyzer.py    # score headlines + crash prediction
```

After this, the auto-scheduler keeps data fresh every 15 minutes during market hours.

## Project Structure

```
├── app.py                  Desktop entry point — pywebview window + worker launcher
├── server.py               ThreadingHTTPServer on :8088, serves all /api/ routes
├── scraper.py              Free data: yfinance indices + RSS news headlines
├── playwright_scraper.py   Deep scrape: TradingView, Investing.com, CBOE, Finviz
├── qwen_analyzer.py        Local LLM: headline scoring + crash probability
├── scheduler.py            15-min auto-refresh loop during market hours
├── ibkr_client.py          ib_insync connection wrapper
├── ibkr_live.py            Live SPX/VIX tick streamer (runs as background process)
├── ibkr_options.py         OPRA options chain fetcher
├── event_window.py         Earnings/macro window detection → SKIP signal
├── config.py               Shared constants: DB path, Ollama URL, IBKR ports
├── backtest.py             Historical calibration of the crash predictor
├── db_audit.py             Database inspection + maintenance utilities
├── migrate_db.py           Schema migrations
├── score_all_socials.py    Batch social media sentiment scoring
├── requirements.txt
├── Start_Trading_Tool.bat  Windows launcher (kills old instances, runs app.py)
├── IMPROVEMENTS.md         Architecture notes and session log
├── dashboard/
│   ├── src/
│   │   ├── App.jsx          Sidebar layout + screen routing
│   │   ├── theme.js         Design tokens + API base URL (http://localhost:8088)
│   │   ├── components/      One .jsx file per panel (11 panels)
│   │   └── util/kelly.js    Kelly criterion math for position sizer
│   ├── package.json
│   └── vite.config.js
├── legacy/
│   └── trading_app.jsx      Original monolithic prototype (reference only)
├── .env.example
└── .gitignore
```

## Configuration

All settings live in `config.py`. Key values:

| Constant | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434/v1/chat/completions` | Local Ollama |
| `QWEN_MODEL` | `qwen3:30b` | Model name |
| `API_PORT` | `8088` | Backend HTTP port |
| `IB_HOST` | `127.0.0.1` | IBKR TWS/Gateway host |
| `IB_PORT` | `4001` | IB Gateway live (4002 = paper; TWS: 7496/7497) |

## Data Sources

| Source | Method | API key? |
|---|---|---|
| World indices | yfinance | No |
| Market news | RSS (CNBC, Reuters, Yahoo Finance) | No |
| Fear & Greed | CNN JSON endpoint | No |
| TradingView signals | Playwright | No |
| CBOE Put/Call ratio | Playwright | No |
| Futures, sectors, ticker news | Finviz via Playwright | No |
| Economic calendar | Investing.com via Playwright | No |
| Live SPX/VIX ticks | IBKR TWS via ib_insync | Requires IBKR account |
| Live options chain | IBKR OPRA via ib_insync | Requires IBKR account |
| LLM inference | Local Ollama | No |

## Notes

- `trading.db` is git-ignored and auto-created on first scraper run.
- IBKR is optional — without it the Sizer falls back to Black-Scholes premium estimates.
- Paper trading port: TWS 7497 / IB Gateway 4002. Start there.
- Playwright scrapers may break when target sites update their layouts.
- `app.py` uses a Windows Mutex to prevent duplicate instances; single-process server uses `ThreadingHTTPServer` to handle concurrent requests without SQLite write races.
