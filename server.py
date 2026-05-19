"""
Local API server — serves data to the React dashboard
Runs at http://127.0.0.1:8088
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json, subprocess, sys, threading
from datetime import datetime, timedelta

from config import API_HOST, API_PORT, db_connect
from event_window import active_window_events, WINDOW_MIN


# ---------------------------------------------------------------------------
# Job queue: gate subprocess spawns so duplicate clicks (or a click during the
# scheduler's cycle) don't race two writers against the same SQLite file.
# ---------------------------------------------------------------------------
JOB_SCRIPTS = {
    # scrape then score — must be sequential so the scorer sees fresh rows
    "refresh":  ["scraper.py", "qwen_analyzer.py"],
    "deep":     ["playwright_scraper.py"],
    "predict":  ["qwen_analyzer.py"],
    "backtest": ["backtest.py"],
    "options":  ["ibkr_options.py"],
    "live":     ["ibkr_live.py"],   # long-running streamer (SPX/VIX real-time)
}

_jobs_lock = threading.Lock()
_running: set[str] = set()


def _run_job(kind: str) -> None:
    try:
        for script in JOB_SCRIPTS[kind]:
            subprocess.run([sys.executable, script], check=False)
    finally:
        with _jobs_lock:
            _running.discard(kind)


def start_job(kind: str) -> str:
    if kind not in JOB_SCRIPTS:
        return "unknown"
    with _jobs_lock:
        if kind in _running:
            return "already_running"
        _running.add(kind)
    threading.Thread(target=_run_job, args=(kind,), daemon=True).start()
    return "started"


def _running_snapshot() -> list[str]:
    with _jobs_lock:
        return sorted(_running)


# ---------------------------------------------------------------------------
# Window helpers (yesterday / today / tomorrow, weekend-aware)
# ---------------------------------------------------------------------------
def _three_week_window() -> tuple[str, str, str, str, str, str]:
    now = datetime.now()
    # Current week: Monday to Sunday
    current_start = now - timedelta(days=now.weekday())
    current_end = current_start + timedelta(days=6)
    
    past_start = current_start - timedelta(days=7)
    past_end = current_start - timedelta(days=1)
    
    next_start = current_end + timedelta(days=1)
    next_end = current_end + timedelta(days=7)
    
    return (past_start.strftime("%Y-%m-%d"), past_end.strftime("%Y-%m-%d"),
            current_start.strftime("%Y-%m-%d"), current_end.strftime("%Y-%m-%d"),
            next_start.strftime("%Y-%m-%d"), next_end.strftime("%Y-%m-%d"))


def _options_payload(conn, symbol: str, right: str | None) -> dict:
    """Return the latest option_chain snapshot for `symbol`, optionally filtered by right.

    Resilient to the table not existing yet (returns empty rows).
    """
    try:
        # Find the latest fetched_at for this symbol so we always return one consistent snapshot.
        latest_row = conn.execute(
            "SELECT MAX(fetched_at) FROM option_chain WHERE symbol = ?", (symbol,)
        ).fetchone()
    except Exception:
        return {"symbol": symbol, "rows": [], "fetched_at": None, "underlying_price": None}

    latest = latest_row[0] if latest_row else None
    if not latest:
        return {"symbol": symbol, "rows": [], "fetched_at": None, "underlying_price": None}

    params: list = [symbol, latest]
    where = "symbol = ? AND fetched_at = ?"
    if right in ("C", "P"):
        where += " AND right = ?"
        params.append(right)
    rows = conn.execute(
        f"SELECT * FROM option_chain WHERE {where} ORDER BY right, strike",
        params,
    ).fetchall()
    rows_d = [dict(r) for r in rows]
    spot = rows_d[0]["underlying_price"] if rows_d else None
    expiry = rows_d[0]["expiry"] if rows_d else None
    return {
        "symbol": symbol,
        "expiry": expiry,
        "fetched_at": latest,
        "underlying_price": spot,
        "rows": rows_d,
    }


# ---------------------------------------------------------------------------
# Trade journal — every contract you actually buy, with the prediction
# snapshot at entry. The point is to accumulate ground truth so you can later
# answer: when the model said X, and I traded the strike it recommended, what
# happened?
# ---------------------------------------------------------------------------
def _ensure_trade_journal(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_journal (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            entered_at               TEXT NOT NULL,
            prediction_id            INTEGER,
            prediction_snapshot_json TEXT,
            symbol                   TEXT NOT NULL,
            expiry                   TEXT,
            strike                   REAL NOT NULL,
            right                    TEXT NOT NULL,
            contracts                INTEGER NOT NULL,
            entry_premium            REAL NOT NULL,
            entry_underlying         REAL,
            exit_at                  TEXT,
            exit_premium             REAL,
            exit_underlying          REAL,
            pnl_dollars              REAL,
            status                   TEXT NOT NULL DEFAULT 'OPEN',
            notes                    TEXT
        )
    """)
    conn.commit()


def _capture_prediction_snapshot(conn, prediction_id: int | None) -> tuple[int | None, str | None]:
    """If prediction_id given, fetch that row. Else fetch the latest prediction.
    Returns (id, json-encoded snapshot) — both None if no predictions exist."""
    if prediction_id is not None:
        row = conn.execute("SELECT * FROM predictions WHERE id = ?", (prediction_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM predictions ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        return None, None
    d = dict(row)
    # Inflate distribution_json/drivers_json/top_risks so the snapshot is human-readable later.
    for k in ("distribution_json", "drivers_json", "top_risks"):
        if d.get(k):
            try:
                d[k.replace("_json", "")] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d["id"], json.dumps(d)


def _capture_spot(conn, symbol: str) -> float | None:
    name_map = {"SPX": "S&P 500", "NDX": "NASDAQ"}
    name = name_map.get(symbol.upper())
    if not name:
        return None
    row = conn.execute("SELECT price FROM indices WHERE name = ?", (name,)).fetchone()
    return float(row[0]) if row else None


def _trades_payload(conn) -> dict:
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_journal ORDER BY entered_at DESC"
        ).fetchall()]
    except Exception:
        return {"trades": [], "open": [], "closed": [], "stats": _empty_trade_stats()}

    # Inflate snapshot blob if present (consumer doesn't have to parse twice).
    for t in rows:
        if t.get("prediction_snapshot_json"):
            try:
                t["prediction_snapshot"] = json.loads(t["prediction_snapshot_json"])
            except json.JSONDecodeError:
                t["prediction_snapshot"] = None

    open_rows   = [t for t in rows if t["status"] == "OPEN"]
    closed_rows = [t for t in rows if t["status"] != "OPEN"]

    pnls = [t["pnl_dollars"] for t in closed_rows if t["pnl_dollars"] is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    total_pnl = sum(pnls) if pnls else 0
    avg_pnl   = (total_pnl / len(pnls)) if pnls else None
    win_rate  = (100.0 * wins / len(pnls)) if pnls else None

    stats = {
        "open_count":   len(open_rows),
        "closed_count": len(closed_rows),
        "wins":         wins,
        "losses":       losses,
        "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
        "total_pnl":    round(total_pnl, 2),
        "avg_pnl":      round(avg_pnl, 2) if avg_pnl is not None else None,
        "best_pnl":     round(max(pnls), 2) if pnls else None,
        "worst_pnl":    round(min(pnls), 2) if pnls else None,
    }
    return {"open": open_rows, "closed": closed_rows, "stats": stats}


def _empty_trade_stats() -> dict:
    return {"open_count": 0, "closed_count": 0, "wins": 0, "losses": 0,
            "win_rate_pct": None, "total_pnl": 0, "avg_pnl": None,
            "best_pnl": None, "worst_pnl": None}


def _create_trade(conn, payload: dict) -> dict:
    _ensure_trade_journal(conn)
    required = ("symbol", "strike", "right", "contracts", "entry_premium")
    missing = [k for k in required if payload.get(k) in (None, "")]
    if missing:
        return {"error": f"missing fields: {', '.join(missing)}"}

    symbol = str(payload["symbol"]).upper()
    side   = str(payload["right"]).upper()
    if side not in ("C", "P"):
        return {"error": "right must be C or P"}

    pred_id, snapshot_json = _capture_prediction_snapshot(conn, payload.get("prediction_id"))
    underlying = payload.get("entry_underlying")
    if underlying is None:
        underlying = _capture_spot(conn, symbol)

    cur = conn.execute(
        "INSERT INTO trade_journal ("
        "entered_at, prediction_id, prediction_snapshot_json, symbol, expiry, strike, right, "
        "contracts, entry_premium, entry_underlying, status, notes"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            datetime.now().isoformat(),
            pred_id,
            snapshot_json,
            symbol,
            payload.get("expiry"),
            float(payload["strike"]),
            side,
            int(payload["contracts"]),
            float(payload["entry_premium"]),
            float(underlying) if underlying is not None else None,
            "OPEN",
            payload.get("notes"),
        ),
    )
    conn.commit()
    return {"id": cur.lastrowid, "status": "created"}


def _close_trade(conn, trade_id: int, payload: dict) -> dict:
    row = conn.execute("SELECT * FROM trade_journal WHERE id = ?", (trade_id,)).fetchone()
    if not row:
        return {"error": "not found"}
    row = dict(row)
    if row["status"] != "OPEN":
        return {"error": f"trade already {row['status']}"}

    if "exit_premium" not in payload:
        return {"error": "exit_premium required"}
    exit_prem = float(payload["exit_premium"])
    exit_und  = payload.get("exit_underlying")
    if exit_und is None:
        exit_und = _capture_spot(conn, row["symbol"])

    # Long premium P&L: (exit - entry) * 100 * contracts
    pnl = (exit_prem - row["entry_premium"]) * 100.0 * row["contracts"]
    new_status = "EXPIRED" if exit_prem == 0 else "CLOSED"

    conn.execute(
        "UPDATE trade_journal SET exit_at = ?, exit_premium = ?, exit_underlying = ?, "
        "pnl_dollars = ?, status = ?, notes = COALESCE(?, notes) WHERE id = ?",
        (datetime.now().isoformat(), exit_prem,
         float(exit_und) if exit_und is not None else None,
         round(pnl, 2), new_status, payload.get("notes"), trade_id),
    )
    conn.commit()
    return {"id": trade_id, "status": new_status, "pnl_dollars": round(pnl, 2)}


def _delete_trade(conn, trade_id: int) -> dict:
    cur = conn.execute("DELETE FROM trade_journal WHERE id = ?", (trade_id,))
    conn.commit()
    return {"deleted": cur.rowcount}


def _calibration_payload(conn) -> dict:
    """Rolling scores + per-bucket calibration for the distribution predictor.

    Reference points for the legacy binary Brier (crash 0/1):
      perfect=0.0, always-base-rate(~3.4%)~0.033, random=0.25.
    For multi-Brier over 9 buckets: uniform 1/9 across all → ~0.889.
    For log-score: lower = better; uniform 1/9 → ln(9) ≈ 2.197.
    """
    from qwen_analyzer import BUCKETS, BUCKET_LABELS

    rows = [dict(r) for r in conn.execute("""
        SELECT prediction_at, target_session, crash_prob, crashed, brier,
               spx_return_pct, ndx_return_pct,
               realized_bucket, predicted_bucket, bucket_prob, log_score,
               multi_brier, directional_hit, expected_move_pct
        FROM prediction_outcomes ORDER BY target_session DESC
    """).fetchall()]
    total = len(rows)

    def _avg(slice_rows, key):
        vals = [r[key] for r in slice_rows if r.get(key) is not None]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 4)

    # Crash-prob calibration bins (back-compat)
    crash_bins = []
    for i in range(10):
        lo, hi = i * 10, (i + 1) * 10
        bucket = [r for r in rows if lo <= r["crash_prob"] < hi] if hi < 100 \
                 else [r for r in rows if lo <= r["crash_prob"] <= hi]
        if not bucket:
            crash_bins.append({"bin": f"{lo}-{hi}%", "count": 0,
                               "predicted_avg": None, "actual_freq": None})
            continue
        crash_bins.append({
            "bin": f"{lo}-{hi}%",
            "count": len(bucket),
            "predicted_avg": round(sum(r["crash_prob"] for r in bucket) / len(bucket), 2),
            "actual_freq": round(100.0 * sum(r["crashed"] for r in bucket) / len(bucket), 2),
        })

    # Per-bucket calibration for the distribution: for each bucket k,
    # average P_predicted(k) across all rows that had a distribution, and
    # compare to the empirical frequency of realized == k.
    rows_with_dist = [r for r in rows if r.get("realized_bucket")]
    n_dist = len(rows_with_dist)
    bucket_calibration = []
    # We need P_k per row — we don't store it; reconstruct from predictions table.
    if n_dist:
        pred_ids = [r["prediction_at"] for r in rows_with_dist]
        # Pull distributions back
        # (join by prediction_at since prediction_outcomes uses it as the link)
        placeholders = ",".join("?" * len(pred_ids))
        dist_rows = conn.execute(
            f"SELECT created_at, distribution_json FROM predictions "
            f"WHERE created_at IN ({placeholders})",
            pred_ids,
        ).fetchall()
        dist_by_ts: dict[str, dict] = {}
        for dr in dist_rows:
            try:
                dist_by_ts[dr[0]] = json.loads(dr[1]) if dr[1] else {}
            except json.JSONDecodeError:
                dist_by_ts[dr[0]] = {}

        for k in BUCKETS:
            pks, hits = [], 0
            denom = 0
            for r in rows_with_dist:
                dist = dist_by_ts.get(r["prediction_at"], {})
                if not dist:
                    continue
                denom += 1
                pks.append(float(dist.get(k, 0) or 0))
                if r["realized_bucket"] == k:
                    hits += 1
            if denom == 0:
                bucket_calibration.append({"bucket": k, "label": BUCKET_LABELS[k],
                                           "predicted_avg_pct": None,
                                           "actual_freq_pct": None, "count": 0})
                continue
            bucket_calibration.append({
                "bucket": k,
                "label": BUCKET_LABELS[k],
                "count": denom,
                "predicted_avg_pct": round(100.0 * sum(pks) / denom, 2),
                "actual_freq_pct":   round(100.0 * hits / denom, 2),
            })

    # Directional accuracy
    dir_rows = [r for r in rows if r.get("directional_hit") is not None]
    directional = None
    if dir_rows:
        directional = round(100.0 * sum(r["directional_hit"] for r in dir_rows) / len(dir_rows), 2)

    return {
        "total": total,
        "actual_crash_rate_pct": round(100.0 * sum(r["crashed"] for r in rows) / total, 2) if total else None,
        # Legacy binary crash scoring
        "brier_all": _avg(rows, "brier"),
        "brier_30":  _avg(rows[:30], "brier"),
        "brier_90":  _avg(rows[:90], "brier"),
        # New distribution scoring
        "multi_brier_all": _avg(rows, "multi_brier"),
        "multi_brier_30":  _avg(rows[:30], "multi_brier"),
        "multi_brier_90":  _avg(rows[:90], "multi_brier"),
        "log_score_all":   _avg(rows, "log_score"),
        "log_score_30":    _avg(rows[:30], "log_score"),
        "log_score_90":    _avg(rows[:90], "log_score"),
        "directional_accuracy_pct": directional,
        "directional_n": len(dir_rows),
        "crash_prob_bins": crash_bins,
        "bucket_calibration": bucket_calibration,
        "recent": rows[:10],
    }


class Handler(BaseHTTPRequestHandler):
    # --- helpers ---------------------------------------------------------
    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- routing ---------------------------------------------------------
    def do_GET(self):
        conn = db_connect(row_factory=True)
        try:
            path = self.path

            if path == "/api/indices":
                rows = conn.execute("SELECT * FROM indices ORDER BY name").fetchall()
                return self._send_json([dict(r) for r in rows])

            if path == "/api/news":
                rows = conn.execute("""
                    SELECT s.*, n.url FROM scored_news s
                    JOIN news n ON s.id = n.id
                    ORDER BY s.scored_at DESC LIMIT 20
                """).fetchall()
                return self._send_json([dict(r) for r in rows])

            if path == "/api/prediction":
                row = conn.execute("SELECT * FROM predictions ORDER BY created_at DESC LIMIT 1").fetchone()
                if not row:
                    return self._send_json({})
                d = dict(row)
                # Inflate JSON-encoded fields so the client doesn't have to.
                for k in ("distribution_json", "drivers_json", "top_risks"):
                    if k in d and isinstance(d[k], str) and d[k]:
                        try:
                            d[k.replace("_json", "")] = json.loads(d[k])
                        except json.JSONDecodeError:
                            pass
                return self._send_json(d)

            if path == "/api/fear-greed":
                row = conn.execute("SELECT * FROM fear_greed ORDER BY fetched_at DESC LIMIT 1").fetchone()
                return self._send_json(dict(row) if row else {})

            if path == "/api/sectors":
                rows = conn.execute("SELECT * FROM sectors ORDER BY change_pct DESC").fetchall()
                return self._send_json([dict(r) for r in rows])

            if path == "/api/futures":
                rows = conn.execute("SELECT * FROM futures ORDER BY name").fetchall()
                return self._send_json([dict(r) for r in rows])

            if path == "/api/socials":
                rows = conn.execute(
                    "SELECT * FROM political_socials WHERE qwen_sentiment IN ('BULLISH','BEARISH') "
                    "ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
                return self._send_json([dict(r) for r in rows])

            if path == "/api/live-tick":
                try:
                    rows = conn.execute("SELECT * FROM live_ticks ORDER BY symbol").fetchall()
                    ticks = {r["symbol"]: dict(r) for r in rows}
                except Exception:
                    ticks = {}
                return self._send_json(ticks)

            if path == "/api/calendar":
                rows = conn.execute("SELECT * FROM econ_calendar ORDER BY date ASC, time ASC").fetchall()
                return self._send_json([dict(r) for r in rows])

            if path == "/api/macro-window":
                ps, pe, cs, ce, ns, ne = _three_week_window()
                rows = conn.execute(
                    "SELECT * FROM econ_calendar WHERE date >= ? AND date <= ? "
                    "AND currency = 'USD' AND impact = 'high' ORDER BY date ASC, time ASC",
                    (ps, ne),
                ).fetchall()
                res = {"past_week": {"date": f"{ps[5:]} to {pe[5:]}", "events": []},
                       "current_week": {"date": f"{cs[5:]} to {ce[5:]}", "events": []},
                       "next_week": {"date": f"{ns[5:]} to {ne[5:]}", "events": []}}
                for r in rows:
                    d = dict(r)
                    if ps <= d["date"] <= pe: res["past_week"]["events"].append(d)
                    elif cs <= d["date"] <= ce: res["current_week"]["events"].append(d)
                    elif ns <= d["date"] <= ne: res["next_week"]["events"].append(d)
                return self._send_json(res)

            if path == "/api/technicals":
                rows = conn.execute("SELECT * FROM technicals ORDER BY source, symbol, timeframe").fetchall()
                return self._send_json([dict(r) for r in rows])

            if path == "/api/putcall":
                row = conn.execute("SELECT * FROM putcall ORDER BY fetched_at DESC LIMIT 1").fetchone()
                return self._send_json(dict(row) if row else {})

            if path == "/api/finviz-news":
                rows = conn.execute("SELECT * FROM finviz_news ORDER BY fetched_at DESC LIMIT 50").fetchall()
                return self._send_json([dict(r) for r in rows])

            if path == "/api/earnings":
                rows = conn.execute("SELECT * FROM earnings ORDER BY earnings_date ASC").fetchall()
                return self._send_json([dict(r) for r in rows])

            if path == "/api/earnings-window":
                ps, pe, cs, ce, ns, ne = _three_week_window()
                rows = conn.execute(
                    "SELECT * FROM earnings WHERE earnings_date >= ? AND earnings_date <= ? ORDER BY earnings_date ASC",
                    (ps, ne),
                ).fetchall()
                res = {"past_week": {"date": f"{ps[5:]} to {pe[5:]}", "events": []},
                       "current_week": {"date": f"{cs[5:]} to {ce[5:]}", "events": []},
                       "next_week": {"date": f"{ns[5:]} to {ne[5:]}", "events": []}}
                for r in rows:
                    d = dict(r)
                    if ps <= d["earnings_date"] <= pe: res["past_week"]["events"].append(d)
                    elif cs <= d["earnings_date"] <= ce: res["current_week"]["events"].append(d)
                    elif ns <= d["earnings_date"] <= ne: res["next_week"]["events"].append(d)
                return self._send_json(res)

            if path.startswith("/api/earnings-history"):
                import urllib.parse as urlparse
                qs = urlparse.parse_qs(urlparse.urlparse(path).query)
                ticker = qs.get("ticker", [None])[0]
                if not ticker:
                    return self._send_json([])
                rows = conn.execute(
                    "SELECT * FROM earnings_history WHERE ticker = ? ORDER BY date DESC",
                    (ticker,),
                ).fetchall()
                return self._send_json([dict(r) for r in rows])

            if path.startswith("/api/analyze-event"):
                import urllib.parse as urlparse
                qs = urlparse.parse_qs(urlparse.urlparse(path).query)
                ticker = qs.get("ticker", [""])[0]
                name = qs.get("name", [""])[0]
                
                # Fetch history to feed into Qwen for better context
                history_rows = []
                if ticker:
                    history_rows = conn.execute(
                        "SELECT * FROM earnings_history WHERE ticker = ? ORDER BY date DESC LIMIT 4",
                        (ticker,)
                    ).fetchall()
                
                hist_text = "\\n".join([f"Date: {r['date']}, Actual EPS: {r['eps_actual']}, Est EPS: {r['eps_estimate']}, Surprise: {r['surprise_pct']}" for r in history_rows]) if history_rows else "No historical DB records available."
                
                prompt = f"""You are a financial analyst operating in the year 2026. IMPORTANT: Do NOT claim that 2025 or 2026 dates are fictional or future-dated. Accept all provided dates and data as current reality.

Provide a detailed financial analysis of the upcoming event: '{name}' (Ticker: {ticker}).
Please structure your response with these exact sections:
1. **Event Details**: What is this event about in detail?
2. **Expectations**: What are the current market expectations?
3. **Historical Context**: Based on previous occurrences, how has the market typically reacted to this?
4. **Post-Event (If applicable)**: If this event recently completed, what was the expectation vs actual result?

Here is some recent historical data for context (if any):
{hist_text}

Be concise, format with Markdown, focus strictly on actionable market intelligence, and DO NOT question the timeline."""
                
                import requests
                from config import OLLAMA_URL, QWEN_MODEL
                try:
                    resp = requests.post(OLLAMA_URL, json={
                        "model": QWEN_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                    }, timeout=45)
                    answer = resp.json()["choices"][0]["message"]["content"]
                    
                    # Try to strip out <think> tags if Qwen includes them
                    import re
                    answer = re.sub(r'<think>.*?</think>', '', answer, flags=re.DOTALL).strip()
                    
                    return self._send_json({"analysis": answer})
                except Exception as e:
                    return self._send_json({"error": str(e)}, status=500)

            if path == "/api/calibration":
                return self._send_json(_calibration_payload(conn))

            if path == "/api/trades":
                _ensure_trade_journal(conn)
                return self._send_json(_trades_payload(conn))

            if path.startswith("/api/options"):
                import urllib.parse as urlparse
                qs = urlparse.parse_qs(urlparse.urlparse(path).query)
                symbol = (qs.get("symbol", ["SPX"])[0] or "SPX").upper()
                right  = qs.get("right", [None])[0]
                payload = _options_payload(conn, symbol=symbol, right=right)
                return self._send_json(payload)

            if path == "/api/event-window":
                # Wider forward look so the UI can warn ahead; SKIP gate stays at +/- 30.
                events = active_window_events(window_min=WINDOW_MIN, look_ahead_min=60)
                blocking = [e for e in events if -WINDOW_MIN <= e["minutes_until"] <= WINDOW_MIN]
                return self._send_json({
                    "block_window_min": WINDOW_MIN,
                    "blocking": blocking,        # within hard SKIP window (+/- 30 min)
                    "approaching": events,       # everything in +/- 30 .. +60 min view
                    "should_skip": bool(blocking),
                })

            # --- job triggers (GET kept for back-compat with current dashboard) ---
            if path == "/api/refresh":
                return self._send_json({"status": start_job("refresh")})
            if path == "/api/refresh-deep":
                return self._send_json({"status": start_job("deep")})
            if path == "/api/predict":
                return self._send_json({"status": start_job("predict")})
            if path == "/api/backtest":
                return self._send_json({"status": start_job("backtest")})
            if path == "/api/refresh-options":
                return self._send_json({"status": start_job("options")})
            if path == "/api/start-live":
                return self._send_json({"status": start_job("live")})

            if path == "/api/jobs":
                return self._send_json({"running": _running_snapshot()})

            if path.startswith("/api/"):
                return self._send_json({"error": "unknown endpoint"}, status=404)

            # Serve static files from dashboard/dist
            import os, mimetypes
            static_path = path.split("?")[0]
            if static_path == "/":
                static_path = "/index.html"
            
            filepath = os.path.join(os.path.dirname(__file__), "dashboard", "dist", static_path.lstrip("/"))
            if not os.path.exists(filepath):
                # Fallback to index.html for React Router
                filepath = os.path.join(os.path.dirname(__file__), "dashboard", "dist", "index.html")
            
            if os.path.exists(filepath):
                try:
                    with open(filepath, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    ctype, _ = mimetypes.guess_type(filepath)
                    if ctype:
                        self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.end_headers()
                    self.wfile.write(content)
                    return
                except Exception:
                    return self._send_json({"error": "file read error"}, status=500)

            return self._send_json({"error": "not found"}, status=404)
        finally:
            conn.close()

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return None

    def do_POST(self):
        payload = self._read_json_body()
        if payload is None:
            return self._send_json({"error": "bad json"}, status=400)

        if self.path == "/api/jobs":
            kind = payload.get("kind", "")
            status = start_job(kind)
            code = 200 if status == "started" else (409 if status == "already_running" else 400)
            return self._send_json({"kind": kind, "status": status, "running": _running_snapshot()}, status=code)

        if self.path == "/api/trades":
            conn = db_connect(row_factory=True)
            try:
                result = _create_trade(conn, payload)
            finally:
                conn.close()
            code = 400 if "error" in result else 201
            return self._send_json(result, status=code)

        return self._send_json({"error": "unknown endpoint"}, status=404)

    def do_PATCH(self):
        if self.path.startswith("/api/trades/"):
            try:
                trade_id = int(self.path.rsplit("/", 1)[-1])
            except ValueError:
                return self._send_json({"error": "bad id"}, status=400)
            payload = self._read_json_body()
            if payload is None:
                return self._send_json({"error": "bad json"}, status=400)
            conn = db_connect(row_factory=True)
            try:
                result = _close_trade(conn, trade_id, payload)
            finally:
                conn.close()
            code = 404 if result.get("error") == "not found" \
                   else 400 if "error" in result else 200
            return self._send_json(result, status=code)
        return self._send_json({"error": "unknown endpoint"}, status=404)

    def do_DELETE(self):
        if self.path.startswith("/api/trades/"):
            try:
                trade_id = int(self.path.rsplit("/", 1)[-1])
            except ValueError:
                return self._send_json({"error": "bad id"}, status=400)
            conn = db_connect()
            try:
                result = _delete_trade(conn, trade_id)
            finally:
                conn.close()
            return self._send_json(result)
        return self._send_json({"error": "unknown endpoint"}, status=404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


if __name__ == "__main__":
    print(f"Starting API server at http://{API_HOST}:{API_PORT}")
    ThreadingHTTPServer((API_HOST, API_PORT), Handler).serve_forever()
