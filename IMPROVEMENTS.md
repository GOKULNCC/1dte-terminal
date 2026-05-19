# Trading Tool — Improvements & Architecture

This document captures every change made in our session, why each change was
made, and what the system looks like now. Sections roughly map to the order
the work was done.

---

## 0. Starting point (what we had)

A local 0DTE / 1DTE SPX/NDX decision-support terminal:

- `scraper.py` — yfinance prices, RSS news, Twitter syndication, CNN Fear &
  Greed
- `playwright_scraper.py` — TradingView / Investing.com / CBOE / Finviz /
  ForexFactory (deep scrape)
- `qwen_analyzer.py` — local Qwen3-30B via Ollama for sentiment + crash
  probability
- `server.py` — stdlib HTTP on :8088 reading SQLite
- `dashboard/` — React 19 + Vite dashboard
- `scheduler.py` — 15-min market-hours refresh loop

**Issues identified up front:**

1. No real IBKR live data — yfinance 15-min delays are useless for 0DTE entry.
2. No option chain awareness — sizing was blind to IV / skew / actual strikes.
3. No backtest / calibration — no measurement of whether the predictor adds
   edge.
4. Server spawned subprocesses on GET — duplicate clicks raced two writers
   against SQLite.
5. Stdlib HTTPServer single-threaded.
6. Twitter syndication scraping fragile.
7. Qwen prompt parsed with regex instead of structured output.
8. Scheduler had no recovery / no holiday or event awareness.

All of the above have been at least partially addressed below.

---

## 1. Foundation: config, WAL, job queue, sequential refresh

**Why:** The most-correct-but-cheap fixes. Eliminates the duplicate-writer
race and centralises the dozen places that hard-coded `"trading.db"`.

### Files

#### `config.py` (new)

Single source of truth:

- `DB_PATH`, `PROJECT_ROOT`
- `OLLAMA_URL`, `QWEN_MODEL`
- `API_HOST = "127.0.0.1"`, `API_PORT = 8088`
- `IB_HOST = "127.0.0.1"`, `IB_PORT = 4001`, `IB_CLIENT_ID = 17`
- `db_connect(row_factory=False)` helper. Opens SQLite with
  `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL`. WAL is the
  thing that lets the scheduler, the server, and an on-demand fetch all
  read/write the file without blocking each other.

#### `requirements.txt` (new)

```
requests
yfinance
feedparser
playwright
beautifulsoup4
ib_insync
```

#### `server.py` (rewritten)

- `ThreadingHTTPServer` instead of `HTTPServer` (was single-threaded).
- Bound to `127.0.0.1` instead of `""` (was exposed on all interfaces).
- In-process **job lock** (`threading.Lock` + a `set` of running job kinds).
  Duplicate clicks during an in-flight refresh now coalesce — return
  `{"status": "already_running"}` instead of spawning a competing writer.
- `JOB_SCRIPTS` dict registering: `refresh`, `deep`, `predict`, `backtest`,
  `options`.
- `/api/refresh` now runs `scraper.py` then `qwen_analyzer.py`
  **sequentially** via `subprocess.run` (was Popen'd in parallel — analyzer
  scored stale rows on every cycle, a real bug).
- New `POST /api/jobs {kind: ...}` and `GET /api/jobs`.
- Cleaner per-route response code via a `_send_json` helper.

#### scripts updated to use config

`scraper.py`, `playwright_scraper.py`, `qwen_analyzer.py`,
`migrate_db.py` — import `db_connect` (and `OLLAMA_URL` / `QWEN_MODEL` where
relevant) from `config`. Local `DB_PATH` constants removed.

### Behaviour after this phase

```
GET /api/refresh        → kicks off scraper + scorer sequentially
GET /api/refresh-deep   → kicks off Playwright scrape
GET /api/predict        → kicks off Qwen analyzer only
GET /api/jobs           → { running: [...] }
POST /api/jobs          → trigger by kind, gated by the same lock
```

---

## 2. Backtest / calibration job

**Why:** The predictor was writing `predictions.crash_prob` but nothing was
recording what the market *actually did*. Without that, no measurement.

### Files

#### `backtest.py` (new)

- Creates `prediction_outcomes` table.
- For every `predictions` row whose target session has closed:
  - "Target session" = first trading day strictly after `created_at`'s date.
    Conservative — intraday predictions don't get scored against the same
    day's move.
  - Downloads SPX (`^GSPC`) and NDX (`^NDX`) close-to-close returns from
    yfinance, padded back 10 calendar days so `pct_change()` always has a
    prior bar.
  - Marks `crashed = 1` if SPX ≤ -2% or NDX ≤ -2.5% (the original predictor
    definition).
  - Brier per row = `(crash_prob/100 − crashed)²`.
- Idempotent — re-running skips already-scored rows.

#### `server.py` additions

- `_calibration_payload(conn)` — rolling Brier (30 / 90 / all), per-10%
  crash-prob calibration bins, recent outcomes.
- `GET /api/calibration` exposes it.
- `backtest` registered as a job kind, so `/api/backtest` triggers it
  through the lock.

#### `scheduler.py` update

Once-per-day after 21:00 local (= after US close), runs `backtest.py`
automatically. Tracked by `last_backtest_date` so it doesn't re-run within
the same day.

### Reference values

- Brier — perfect = 0.0, always-base-rate (~3.4%) = 0.033, random = 0.25.
  Below 0.033 means Qwen is adding signal over base rate.

---

## 3. Distribution predictor (signed 9-bucket moves)

**Why:** Binary crash/no-crash is too lossy. A 0DTE trader needs direction
AND magnitude AND probability distribution.

### Buckets

Nine buckets covering the full real line of close-to-close % moves:

```
down_2plus   ≤ -2.0%
down_1_5     -2.0% .. -1.5%
down_1       -1.5% .. -1.0%
down_0_5     -1.0% .. -0.5%
flat         -0.5% .. +0.5%
up_0_5       +0.5% .. +1.0%
up_1         +1.0% .. +1.5%
up_1_5       +1.5% .. +2.0%
up_2plus     ≥ +2.0%
```

### Files

#### `qwen_analyzer.py`

- `BUCKETS`, `BUCKET_LABELS`, `classify_bucket(pct)` helpers.
- `_ensure_predictions_schema(conn)` — idempotent ALTER adds:
  - `direction`, `expected_move_pct`, `max_upside_pct`, `max_downside_pct`,
    `ndx_upside_pct`, `ndx_downside_pct`,
  - `distribution_json`, `drivers_json`, `verdict`,
  - (later) `action`, `event_block_reason`.
- `_normalize_distribution(raw)` — clamps to [0,1], renormalises to 1.
  Empty fallback = `{flat: 1.0}`.
- `predict_move()` replaces `predict_crash()`:
  - Reads scored news + BULLISH/BEARISH political socials + indices.
  - Prompt: attribute every catalyst to a specific source, distinguish
    "considering" vs "effective immediately" language on political posts,
    anchor on real base rates (~55% of days move < 0.5%, only ~7% > 2%).
  - Returns `direction`, signed `expected_move_pct`, SPX/NDX max-up/down,
    full distribution summing to 1, `top_drivers`, `verdict`.
- `crash_prob` kept populated as `P(down_2plus) × 100` for back-compat with
  the legacy bin calibration.
- `predict_crash()` is now a thin alias so `scheduler.py` keeps working.

#### `backtest.py` extension

`prediction_outcomes` gains 7 new columns:

- `realized_bucket` — classified from realized SPX move.
- `predicted_bucket` — argmax of the predicted distribution.
- `bucket_prob` — P(realized bucket) from the predicted distribution.
- `log_score` — `-ln(bucket_prob + 1e-6)`.
- `multi_brier` — `Σ (p_k − 1[realized=k])²` across all 9 buckets.
- `directional_hit` — `sign(expected_move) == sign(realized)`.
- `expected_move_pct` — snapshot of the predictor's E[move] at scoring time.

#### `server.py` updates

- `/api/prediction` inflates `distribution_json`, `drivers_json`, and
  `top_risks` into proper JSON objects/arrays before responding.
- `/api/calibration` adds: `multi_brier_30/90/all`, `log_score_30/90/all`,
  `directional_accuracy_pct`, `directional_n`, `bucket_calibration`
  (per-bucket predicted-avg vs actual-frequency).

#### `dashboard/src/components/PredictPanel.jsx` rewritten

- Headline cards: direction + signed expected move; SPX & NDX max-up/down
  ranges; "Quick read" panel with P(any down) / P(flat) / P(any up) + tail
  probabilities.
- Full 9-bucket distribution as a horizontal bar chart, normalised to the
  largest bucket for visual contrast.
- Primary driver + verdict + top catalysts.
- Base rates panel (≤0.5% on 55%, 0.5–1% on 25%, 1–2% on 13%, >2% on 7%).
- Backwards-compatible — old prediction rows without `distribution_json`
  still render direction-less.

### Reference values (multi-bucket)

- Multi-Brier — uniform-1/9-baseline ≈ 0.889; always-predict-flat
  on a typical day ~0.45.
- Log-score — uniform → ln(9) ≈ 2.20. Lower better.
- Directional accuracy — coin-flip 50%; lazy-long ~53%.

---

## 4. Event-window auto-SKIP

**Why:** Within ±30 min of CPI / FOMC / NFP, IV crush + headline whipsaw
make any directional read close to useless for 0DTE long premium. The model
shouldn't be allowed to give a TRADE recommendation in that window.

### Files

#### `event_window.py` (new)

- `_event_dt_utc(date_str, time_str)` — parses ForexFactory time strings
  ("8:30am", "2:00pm", "12:00pm", "12:00am") as ET wall-clock and converts
  to UTC. Returns None for "All Day" / "Tentative" / empty.
- `active_window_events(now_utc, window_min, look_ahead_min)` — queries
  `econ_calendar` for high-impact USD events across a 3-ET-day window
  (handles TZ rollover) and filters to ±window. Sorted by absolute
  proximity.
- `should_block(now_utc)` — strict ±30-min gate used by the predictor.

#### `qwen_analyzer.py` integration

- New columns `action` (TRADE/WIDEN/SKIP) and `event_block_reason`.
- After parsing Qwen output, `should_block()` runs. If blocking:
  - `action = "SKIP"`, `event_block_reason = "Within +/-30 min of: <event @ time ET>, ..."`,
  - Console prints the override prominently.
- Otherwise `action = "TRADE"` (HIGH/MEDIUM confidence) or `"WIDEN"` (LOW).

#### `server.py` endpoint

`GET /api/event-window` returns:

```json
{
  "block_window_min": 30,
  "blocking":    [ ... events within ±30 min ... ],
  "approaching": [ ... events out to +60 min ... ],
  "should_skip": true|false
}
```

#### Dashboard

- `App.jsx` fetches `/api/event-window` on mount and re-polls every 60s.
- `PredictPanel` renders an `EventWindowBanner`:
  - **Red ⛔ AUTO SKIP** when blocking events exist OR last prediction
    was auto-skipped.
  - **Orange ⚠ APPROACHING** when events are 30–60 min out.

### Verified at runtime

Synthetic event 10 min in the future correctly detected as blocking
(`minutes_until: 9.2`), `should_block()` returned `True`.

---

## 5. Position Sizer (BSM heuristic, pre-OPRA)

**Why:** End-to-end "what the market will do → what to actually buy".

### `dashboard/src/components/SizerPanel.jsx` (new)

State persisted to `localStorage` under `sizer-prefs-v1`:
`accountSize, riskPct, indexKey (SPX/NDX), premiumOverride, kellyCapPct`.

**Side selection** (priority order):

1. `eventWindow.should_skip` or `prediction.action === "SKIP"` →
   big red SKIP banner.
2. `direction === "UP"` and |P(any up) − P(any down)| ≥ 10% → **CALL**.
3. `direction === "DOWN"` + same edge → **PUT**.
4. Direction unclear but P(any move ≥ 1%) ≥ 30% and ≥ MEDIUM
   confidence → **STRADDLE** (both legs ATM).
5. Otherwise → **NONE** ("premium decay will beat you here").

**Strike** — driven by `expected_move_pct` + confidence:

- HIGH confidence + small move → ATM (best gamma).
- < 0.4% expected → ATM.
- 0.4–1.0% → 0.20% OTM.
- > 1.0% → 0.35% OTM (leverage trade).
- Rounded to grid (SPX = 5pt, NDX = 25pt).

**Premium estimate (BSM heuristic)** — pre-OPRA fallback:

- ATM 0DTE ≈ `0.4 × spot × (VIX/100) × √(hoursLeft / (252 × 6.5))`.
- OTM ≈ ATM × `exp(−3 × |OTM%|)` (crude delta decay).

**Sizing** — `maxLoss = accountSize × riskPct/100`,
`contracts = floor(maxLoss / (premium × 100))`. Shows actual cost and
actual risk %.

Registered in `App.jsx` nav as the **📐 Sizer** entry.

---

## 6. IBKR / OPRA live option chain

**Why:** Replace the BSM heuristic with real bid/ask/IV/Δ/Γ from the user's
OPRA subscription on a live IB Gateway (port 4001, clientId 17).

### Files

#### `config.py` additions

`IB_HOST`, `IB_PORT = 4001`, `IB_CLIENT_ID = 17`.

#### `ibkr_client.py` (new)

- `ib_session(host, port, client_id, timeout)` — context manager. Connects
  with a 10s timeout, yields the `IB` object, disconnects in `finally`.
- `safe_float(v)` — normalises ib_insync's NaN sentinels to Python `None`.

#### `ibkr_options.py` (new)

- Per-symbol specs:
  - SPX: `exchange=CBOE`, `tradingClass=SPXW`, `strike_step=5`.
  - NDX: `exchange=NASDAQ`, `tradingClass=NDX`, `strike_step=25`.
- `fetch_chain(symbol)`:
  1. `ib.reqMarketDataType(4)` — delayed-frozen mode. Subscribed feeds
     return real-time; unsubscribed (SPX index spot without CBOE
     Streaming Market Indexes) return 15-min-delayed or last frozen.
  2. Fetch spot via `Index(SPX/NDX)`. **Fallback**: if IB still returns
     nothing, read latest from the yfinance-populated `indices` table.
  3. `reqSecDefOptParams` → pick the soonest expiry ≥ today (ET).
  4. Fetch ATM ± 20 strikes × {C, P}.
  5. Streaming `reqMktData` for ~4s, harvest
     bid/ask/last/mid/IV/Δ/Γ/Θ/vega, then cancelMktData.
  6. Upsert into `option_chain` keyed on `(symbol, expiry, strike, right)`.
- CLI: `python ibkr_options.py --symbols SPX` (defaults SPX + NDX).

#### `option_chain` table

```sql
symbol, expiry, strike, right,
bid, ask, last, mid,
iv, delta, gamma, theta, vega,
underlying_price, fetched_at
PRIMARY KEY (symbol, expiry, strike, right)
```

#### `server.py` additions

- `options` registered in `JOB_SCRIPTS` (lockable just like the others).
- `GET /api/options?symbol=SPX[&right=C]` returns the latest consistent
  snapshot (single `fetched_at`) with all legs. Resilient — empty payload
  if table missing or empty.
- `GET /api/refresh-options` triggers the fetch through the job lock.

#### `SizerPanel.jsx` integration

- Auto-fetches `/api/options` on mount and on index-key change.
- Chain status pill in the header:
  `● SPX 20260516 • 82 legs • 12s ago` (green when fresh, amber when > 5 min,
  "no chain — using BSM" when empty).
- **↻ Refresh chain (OPRA)** button triggers the job and re-polls after
  12s.
- `legPremiums` now prefers live OPRA mids. Each strike-card row shows
  actual `bid / ask / IV / Δ / Γ` when live data is present.
- Premium label distinguishes `live OPRA mid` / `BSM heuristic` /
  `user override` / `mixed`.

### Notes on subscriptions

- **OPRA** covers option quotes — real-time bid/ask/IV/Δ/Γ on the chain.
- **CBOE Streaming Market Indexes** ($3.50/mo) covers SPX/VIX index
  values — real-time spot. Not strictly required; the fallback path
  reads spot from the yfinance `indices` table. Recommended for accurate
  intraday Kelly math.
- NDX is NASDAQ-published, so the CBOE sub does **not** cover it.
  NDX chains still work via the spot fallback.

### Read-Only API setting

`Read-Only API` in IB Gateway must be **unchecked** if/when we add order
submission or `ib.positions()` reconciliation. For chain-only reads it
doesn't matter.

---

## 7. Kelly-fraction sizer

**Why:** With a real probability distribution AND real chain prices, we
can compute expected P&L per contract for every strike and use Kelly to
pick the strike + size that maximises long-term growth.

### Files

#### `dashboard/src/util/kelly.js` (new)

- `BUCKET_MIDPOINTS` — representative % move per bucket (e.g. `down_1_5`
  = -1.75%, `up_2plus` = +2.75%).
- `bucketReturns({side, strike, premium, spot, distribution})` — for each
  bucket, compute `close = spot × (1 + mid/100)`, then
  `payoff = max(close − K, 0)` for calls, `max(K − close, 0)` for puts.
  Returns per-bucket `{ prob, closePrice, payoff, r, pnlPerShare }` where
  `r = (payoff − premium) / premium`.
- `expectedPnLForContract(args)` — returns `{ expReturn, expPnLDollars,
  winProb, lossProb, rows }`.
- `kellyFraction(args)` — numerical search for `f` in `[0, 1]` that
  maximises `E[log(1 + f × R)]`. Coarse pass step 0.005 + fine pass step
  0.0005 around the best. Returns 0 when expected return ≤ 0.
- `analyzeChain({chainRows, side, spot, distribution})` — scores every
  chain row of the requested side, sorted by Kelly fraction descending.

#### `SizerPanel.jsx` extension

- New `kellyCapPct` preference (default 25% = ¼-Kelly).
- `kellyAnalysis` memo: when chain fresh + distribution present + sideRec
  is CALL/PUT/STRADDLE, runs `analyzeChain`. For STRADDLE, scans both
  sides and merges by Kelly desc.
- `usingKelly = !!kellyTop && !eventBlock` drives strike selection. When
  active:
  - The "STRIKE" card label flips to "STRIKE (Kelly-optimal)".
  - `effectiveLeg` becomes Kelly's pick, with real greeks shown.
  - Sizing switches from fixed-risk-% to Kelly-bounded:
    `position size = kellyCap × kellyFrac × account`.
  - Sizing card label changes to "Kelly-capped budget"
    showing `25% × 23.5% Kelly × $25,000`.
- New **Kelly Analysis** card lists the top 6 positive-edge strikes:
  `Side | Strike | Premium | Δ | E[$/ctr] | Win P | Kelly% | ¼-Kelly $`.
  The top row is highlighted green with a ★. When zero strikes have
  positive edge: "Market is pricing the move you expect — premium decay
  will win. Sit out."

### Math verified

Bearish-skew test distribution against a synthetic chain:

- ATM put (K=5800, $8): Kelly 42.5%, win prob 50%
- OTM put (K=5750, $2): Kelly 23.5%, win prob 28%
- ITM put (K=5825, $14): Kelly 59.5%, win prob 70%

(The high Kelly numbers reflect artificially cheap synthetic premiums — in
practice real OPRA mids dampen Kelly significantly because the market is
pricing the distribution too.)

---

## 8. Trade journal

**Why:** Six months from now, the ground-truth dataset of "what the model
said when I traded" is more valuable than any model tweak. Captures the
full prediction snapshot at entry time so even if the model is later
updated, you can still see what the *old* model thought about each trade.

### Files

#### `server.py` additions

- `trade_journal` table auto-created on first use:

  ```sql
  id INTEGER PK AUTO,
  entered_at TEXT,
  prediction_id INTEGER,            -- FK predictions.id (nullable)
  prediction_snapshot_json TEXT,    -- full row JSON, frozen at entry
  symbol TEXT, expiry TEXT, strike REAL, right TEXT,
  contracts INTEGER, entry_premium REAL, entry_underlying REAL,
  exit_at TEXT, exit_premium REAL, exit_underlying REAL,
  pnl_dollars REAL,
  status TEXT  -- OPEN | CLOSED | EXPIRED
  notes TEXT
  ```

- `_capture_prediction_snapshot(conn, prediction_id)` — fetches the row
  (or the latest if None), inflates `distribution_json`/`drivers_json`/
  `top_risks` into objects, returns `(id, json-encoded snapshot)`.
- `_capture_spot(conn, symbol)` — reads latest from `indices`.

#### CRUD endpoints

- `GET  /api/trades` — `{ open: [...], closed: [...], stats: {...} }`.
  Stats: open / closed counts, win rate, total P&L, avg P&L, best / worst.
  Each trade has the snapshot inflated to `prediction_snapshot`.
- `POST /api/trades` — required:
  `symbol, strike, right, contracts, entry_premium`. Auto-captures the
  prediction snapshot + spot.
- `PATCH /api/trades/{id}` — body: `exit_premium` (0 = expired worthless).
  Computes `pnl = (exit − entry) × 100 × contracts`. Status
  → `EXPIRED` if `exit_premium == 0` else `CLOSED`.
- `DELETE /api/trades/{id}` — permanent removal.
- OPTIONS preflight updated to include `PATCH, DELETE`.

#### `JournalPanel.jsx` (new)

- 6-stat summary row: Open / Closed / Win rate / Total P&L / Avg per trade
  / Best-worst. Color-coded green/red.
- "Log trade manually" collapsible form: symbol, strike, right, contracts,
  entry premium, expiry, notes.
- **Open Positions** table with inline Close action (expands to take exit
  premium + notes, then PATCHes).
- **History** table — closed/expired trades with P&L coloring, direction
  from the captured prediction snapshot, status pill.
- Both tables have a delete (✕) button with `confirm()` guard.

#### `SizerPanel.jsx` integration

- New **📓 Log this trade (N ctr)** button under the Sizing card.
  Disabled when event-blocked, no recommendation, or contracts = 0.
- POSTs current state (Kelly-selected strike, contract count, live OPRA
  mid as entry premium, captured underlying, chain expiry, prediction_id,
  auto-notes with sizing mode used).
- Inline green-success or red-error banner.

#### `App.jsx` nav

Added **📓 Journal** entry, routed to `JournalPanel`.

### Verified

Synthetic create → snapshot captured → close at $11.20 (entry $8.50) →
P&L correctly $270 → stats roll up to win-rate 100%, total +$270 → delete
works.

---

## 9. Spot-source fix (final correctness pass)

**Why:** The user noticed the Sizer's spot ($7,435.94) didn't match the
chain query's `underlying_price` ($7,408.50). Two different sources,
captured at different times, AND the Kelly math was using the yfinance
spot while the strikes had been picked against IBKR's spot — payoffs
were referenced against the wrong price.

### Change

`SizerPanel.jsx`:

```js
const indicesSpot = spotRow?.price ?? null;
const chainSpot = (chainFresh && chain?.underlying_price) || null;
const spot = chainSpot ?? indicesSpot;
const spotSource = chainSpot ? "chain" : (indicesSpot ? "indices" : null);
```

The SPX spot stat now shows the source: `from chain @ Xs ago` when chain
is loaded, `from yfinance (15-min)` otherwise. Kelly math, strike
heuristics, and BSM premium estimation all now use the same spot value
the strikes were generated against.

---

## Architecture as it stands now

### Data pipeline

```
                        ┌───────────────────┐
                        │  scheduler.py     │ 15-min loop, daily backtest
                        └─────────┬─────────┘
                                  │ subprocess.run
                ┌─────────────────┼──────────────────────────────────┐
                ▼                 ▼                                  ▼
        ┌──────────────┐  ┌──────────────────────┐        ┌────────────────┐
        │ scraper.py   │  │ playwright_scraper   │        │ qwen_analyzer  │
        │ (yfinance,   │  │ (TV, Investing,      │        │ (Qwen3-30B via │
        │  RSS, X,     │  │  CBOE, Finviz, FF)   │        │  Ollama)       │
        │  CNN F&G)    │  └──────────┬───────────┘        └────────┬───────┘
        └───────┬──────┘             │                             │
                │                    │                             │
                └────────────────────┼─────────────────────────────┘
                                     ▼
                            ┌────────────────┐
                            │  trading.db    │  SQLite + WAL
                            └────────┬───────┘
                                     │
                ┌────────────────────┼─────────────────────┐
                │                    │                     │
                ▼                    ▼                     ▼
        ┌──────────────┐    ┌─────────────────┐    ┌──────────────┐
        │ backtest.py  │    │ ibkr_options.py │    │  server.py   │
        │ (next-day    │    │ (OPRA chain     │    │  127.0.0.1   │
        │  outcomes)   │    │  via IB         │    │  :8088       │
        └──────────────┘    │  Gateway 4001)  │    └──────┬───────┘
                            └─────────────────┘           │
                                                          │ JSON
                                                          ▼
                                                ┌──────────────────┐
                                                │  React dashboard │
                                                │  (Vite :5173)    │
                                                └──────────────────┘
```

### Python modules

| File | Role |
|---|---|
| `config.py` | shared constants + `db_connect()` with WAL |
| `scraper.py` | yfinance / RSS / Twitter syndication / CNN F&G / earnings |
| `playwright_scraper.py` | TradingView / Investing / CBOE / Finviz / ForexFactory |
| `qwen_analyzer.py` | Qwen3-30B sentiment + distribution prediction + auto-SKIP |
| `event_window.py` | high-impact USD event detection (ET timezone-aware) |
| `backtest.py` | next-session outcome scoring (binary + multi-bucket + log-score) |
| `ibkr_client.py` | `ib_session()` context manager + `safe_float()` |
| `ibkr_options.py` | OPRA chain fetch + `option_chain` upsert |
| `server.py` | threaded HTTP API + job lock |
| `scheduler.py` | 15-min market-hours refresh + daily backtest |
| `migrate_db.py` | one-shot legacy migration |

### Frontend modules

| File | Role |
|---|---|
| `dashboard/src/App.jsx` | top-level layout, nav, fetches, routes |
| `dashboard/src/theme.js` | `API`, `fmt()`, `sentimentBadge()` |
| `components/DashboardPanel` | home overview |
| `components/IndicesPanel` | world indices + futures + sectors |
| `components/NewsPanel` | scored news + detail view |
| `components/FinvizNewsPanel` | ticker news |
| `components/SocialCatalystsPanel` | scored political socials |
| `components/EventsPanel` | econ calendar + earnings + detail view |
| `components/TechnicalsPanel` | TradingView / Investing technicals |
| `components/PredictPanel` | distribution + drivers + verdict + event banner |
| `components/SizerPanel` | side + strike + Kelly + sizing + log-trade button |
| `components/JournalPanel` | trade journal CRUD + stats + history |
| `components/SettingsPanel` | static settings |
| `util/kelly.js` | bucket-payoff Kelly math |

### Database tables

| Table | Purpose |
|---|---|
| `indices` | symbol, name, price, change_pct, fetched_at |
| `futures` | name, last, change_pct, fetched_at |
| `sectors` | name, change_pct, fetched_at |
| `news` | id, headline, source, url, published, fetched_at |
| `scored_news` | id, headline, source, sentiment, category, crash_relevance, political, author, gemma_note, scored_at |
| `political_socials` | id, author, handle, text, likes, retweets, created_at, fetched_at, qwen_sentiment |
| `econ_calendar` | id, date, time, currency, impact, event, actual, forecast, previous, fetched_at |
| `finviz_news` | id, ticker, headline, source, url, time_str, fetched_at |
| `earnings` | ticker, company, earnings_date, timing, eps_estimate/low/high, revenue_estimate, fetched_at |
| `earnings_history` | id, ticker, date, eps_estimate, eps_actual, surprise_pct, fetched_at |
| `technicals` | id, source, symbol, timeframe, summary, buy/sell/neutral counts, MA / oscillator breakdowns |
| `putcall` | id, date, equity / index / total ratios, fetched_at |
| `fear_greed` | id, score, label, previous_close, one_week_ago, one_month_ago, fetched_at |
| `predictions` | id, crash_prob, primary_driver, top_risks, confidence, model, created_at, **direction, expected_move_pct, max_upside_pct, max_downside_pct, ndx_upside_pct, ndx_downside_pct, distribution_json, drivers_json, verdict, action, event_block_reason** |
| `prediction_outcomes` | prediction_id, prediction_at, target_session, spx/ndx return, crashed, crash_prob, brier, scored_at, **realized_bucket, predicted_bucket, bucket_prob, log_score, multi_brier, directional_hit, expected_move_pct** |
| `option_chain` | symbol, expiry, strike, right, bid, ask, last, mid, iv, delta, gamma, theta, vega, underlying_price, fetched_at |
| `trade_journal` | id, entered_at, prediction_id, prediction_snapshot_json, symbol, expiry, strike, right, contracts, entry_premium, entry_underlying, exit_at, exit_premium, exit_underlying, pnl_dollars, status, notes |

(Bold = added in this session.)

### HTTP API

| Endpoint | Verb | Returns / does |
|---|---|---|
| `/api/indices` | GET | all rows from `indices` |
| `/api/futures`, `/api/sectors`, `/api/calendar`, `/api/technicals`, `/api/putcall`, `/api/finviz-news`, `/api/earnings`, `/api/socials` | GET | per-table dumps |
| `/api/news` | GET | scored_news joined to news for URLs |
| `/api/macro-window` | GET | high-impact USD events for yesterday/today/tomorrow |
| `/api/earnings-window` | GET | same shape for earnings |
| `/api/earnings-history?ticker=X` | GET | historical EPS surprises |
| `/api/fear-greed` | GET | latest CNN F&G |
| `/api/prediction` | GET | latest predictions row, with JSON fields inflated |
| `/api/event-window` | GET | `{ block_window_min, blocking, approaching, should_skip }` |
| `/api/calibration` | GET | brier/multi-brier/log-score + bins + bucket calibration |
| `/api/options?symbol=&right=` | GET | latest consistent chain snapshot |
| `/api/trades` | GET | `{ open, closed, stats }` with snapshots inflated |
| `/api/trades` | POST | create entry; captures latest prediction + spot |
| `/api/trades/{id}` | PATCH | record exit premium; computes P&L; sets status |
| `/api/trades/{id}` | DELETE | remove |
| `/api/jobs` | GET | currently running job kinds |
| `/api/jobs` | POST | `{kind}` — start lockable job |
| `/api/refresh` | GET | start `refresh` job (scraper + scorer sequential) |
| `/api/refresh-deep` | GET | start `deep` job (Playwright) |
| `/api/predict` | GET | start `predict` job (Qwen analyzer only) |
| `/api/backtest` | GET | start `backtest` job |
| `/api/refresh-options` | GET | start `options` job (IBKR/OPRA fetch) |

`OPTIONS` preflight responds with `GET, POST, PATCH, DELETE, OPTIONS`.

### Job kinds (gated by the in-process lock)

```
refresh   = scraper.py     → qwen_analyzer.py    (sequential)
deep      = playwright_scraper.py
predict   = qwen_analyzer.py
backtest  = backtest.py
options   = ibkr_options.py
```

---

## How to run the system

### One-time setup

```powershell
cd "D:\Antigravity\Trading tool"
pip install -r requirements.txt
python -m playwright install chromium
cd dashboard
npm install
cd ..
```

### Daily startup (two terminals)

```powershell
# Terminal 1: API server
cd "D:\Antigravity\Trading tool"
python server.py

# Terminal 2: dashboard
cd "D:\Antigravity\Trading tool\dashboard"
npm run dev          # then open http://localhost:5173/
```

### Optional terminal 3 — auto-refresh

```powershell
cd "D:\Antigravity\Trading tool"
python scheduler.py
```

### Manual one-off scripts

```powershell
python scraper.py              # fast scrape (~30s)
python playwright_scraper.py   # deep scrape  (~60s)
python qwen_analyzer.py        # score + predict (~60s, needs Ollama)
python ibkr_options.py --symbols SPX     # OPRA chain (~15s, needs IB Gateway)
python backtest.py             # score yesterday's predictions
```

### Smoke test

```powershell
curl http://127.0.0.1:8088/api/jobs
curl http://127.0.0.1:8088/api/trades
curl http://127.0.0.1:8088/api/event-window
curl "http://127.0.0.1:8088/api/options?symbol=SPX"
```

### Common gotchas

- **Ollama not running** → `qwen_analyzer.py` fails on `localhost:11434`.
  Start Ollama and `ollama pull qwen3:30b`.
- **IB Gateway clientId conflict** → change `IB_CLIENT_ID` in `config.py`.
- **IB Gateway Read-Only API** → fine for chain-only reads; uncheck before
  adding order submission.
- **Error 354 from IBKR** ("Requested market data is not subscribed") →
  expected for SPX index spot without CBOE Streaming Market Indexes
  subscription. The `reqMarketDataType(4)` line + indices-table fallback
  handle this; subscribe ($3.50/mo) for real-time spot.
- **Sizer says "no chain — using BSM"** → click **↻ Refresh chain (OPRA)**
  on the Sizer page, wait ~15s.
- **Port 8088 in use** → change `API_PORT` in `config.py` AND `API` in
  `dashboard/src/theme.js` to the same new port.

---

## What's still TODO

Roughly in order of value-to-effort ratio:

1. **CSV export from the Journal** — small, useful when you want to do
   external analysis.
2. **Auto-match IBKR fills via `ib.positions()` / `ib.trades()`** — pull
   actual fills and propose matches in the Journal instead of manual entry.
   Requires Read-Only API to be off.
3. **Calibration panel on the dashboard** — `/api/calibration` already
   returns everything; just needs a UI to render Brier / log-score / bucket
   calibration over time.
4. **Live SPX/NDX tick streaming via `ib_insync`** — replaces the 15-min
   yfinance delay with sub-second ticks. Foundation for everything 0DTE
   timing-sensitive.
5. **Order submission button** — given how complete the Sizer is, a single
   "Submit IOC" button that places the recommended trade through IBKR. High
   value, high risk; requires very careful confirmations.
6. **Holiday / half-day awareness in scheduler** — `pandas_market_calendars`.
7. **Better Twitter syndication fallback** — Nitter mirror or skip-with-alert
   on parse failure so silent breaks don't poison the predictor's inputs.

---

*Document generated as the running log of all changes made during the session.
Each section reflects the actual code in the repository at the time of
writing.*
