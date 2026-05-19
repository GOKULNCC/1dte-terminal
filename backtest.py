"""
Backtest / calibration job.

For every row in `predictions` whose target session has already closed,
record the realized SPX/NDX return and whether it counts as a crash by the
predictor's own definition (SPX <= -2% OR NDX <= -2.5%). Writes one row per
prediction into `prediction_outcomes`, plus a per-row Brier contribution.

Target-session rule: the first trading day strictly after `predictions.created_at`'s
date. Conservative — when run intraday, the same day's return is not scored.

Run manually or from the scheduler. Idempotent: existing outcomes are skipped.
"""
from __future__ import annotations

import json, math
from datetime import datetime, timedelta
import yfinance as yf

from config import db_connect
from qwen_analyzer import BUCKETS, classify_bucket


SPX_TICKER = "^GSPC"
NDX_TICKER = "^NDX"
SPX_CRASH_PCT = -2.0
NDX_CRASH_PCT = -2.5


def _init_outcomes_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prediction_outcomes (
            prediction_id     INTEGER PRIMARY KEY,
            prediction_at     TEXT NOT NULL,
            target_session    TEXT NOT NULL,
            spx_return_pct    REAL,
            ndx_return_pct    REAL,
            crashed           INTEGER NOT NULL,
            crash_prob        REAL NOT NULL,
            brier             REAL NOT NULL,
            scored_at         TEXT NOT NULL
        )
    """)
    # Idempotent additions for the distribution-based scoring.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(prediction_outcomes)").fetchall()}
    additions = [
        ("realized_bucket",   "TEXT"),
        ("predicted_bucket",  "TEXT"),    # argmax of the distribution
        ("bucket_prob",       "REAL"),    # P(realized bucket)
        ("log_score",         "REAL"),    # -ln(bucket_prob)
        ("multi_brier",       "REAL"),    # sum_k (p_k - 1[realized=k])^2
        ("directional_hit",   "INTEGER"), # sign(expected) == sign(realized)
        ("expected_move_pct", "REAL"),    # snapshot of predictor's E[move]
    ]
    for col, ddl in additions:
        if col not in cols:
            conn.execute(f"ALTER TABLE prediction_outcomes ADD COLUMN {col} {ddl}")
    conn.commit()


def _load_unscored(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT p.id, p.crash_prob, p.created_at, p.distribution_json, p.expected_move_pct
        FROM predictions p
        LEFT JOIN prediction_outcomes o ON o.prediction_id = p.id
        WHERE o.prediction_id IS NULL
        ORDER BY p.created_at ASC
    """).fetchall()
    out = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r[2])
        except Exception:
            continue
        try:
            dist = json.loads(r[3]) if r[3] else {}
        except (TypeError, json.JSONDecodeError):
            dist = {}
        out.append({
            "id": r[0],
            "crash_prob": r[1] or 0.0,
            "created_at": ts,
            "distribution": dist,
            "expected_move_pct": r[4],
        })
    return out


def _fetch_returns(start_date: str):
    """Return {date_str: {'spx': pct, 'ndx': pct}} for sessions with both tickers."""
    df = yf.download(
        [SPX_TICKER, NDX_TICKER],
        start=start_date,
        progress=False,
        auto_adjust=False,
        group_by="ticker",
        threads=False,
    )
    if df is None or df.empty:
        return {}
    out: dict[str, dict] = {}
    try:
        spx_close = df[SPX_TICKER]["Close"].pct_change() * 100.0
        ndx_close = df[NDX_TICKER]["Close"].pct_change() * 100.0
    except KeyError:
        return {}
    for ts, spx_pct in spx_close.items():
        d = ts.strftime("%Y-%m-%d")
        ndx_pct = ndx_close.get(ts)
        if spx_pct != spx_pct or ndx_pct is None or ndx_pct != ndx_pct:  # NaN check
            continue
        out[d] = {"spx": float(spx_pct), "ndx": float(ndx_pct)}
    return out


def _next_session(after_date_str: str, sessions_sorted: list[str]) -> str | None:
    for s in sessions_sorted:
        if s > after_date_str:
            return s
    return None


def run() -> dict:
    conn = db_connect()
    _init_outcomes_table(conn)

    unscored = _load_unscored(conn)
    if not unscored:
        print("[backtest] nothing to score")
        conn.close()
        return {"scored": 0, "skipped": 0}

    # Pad start back ~10 calendar days so pct_change has a prior bar to reference.
    earliest_dt = min(p["created_at"] for p in unscored) - timedelta(days=10)
    earliest = earliest_dt.strftime("%Y-%m-%d")
    print(f"[backtest] downloading SPX/NDX from {earliest}...")
    returns = _fetch_returns(earliest)
    sessions = sorted(returns.keys())

    now_iso = datetime.now().isoformat()
    scored = 0
    skipped = 0

    for p in unscored:
        pred_date = p["created_at"].strftime("%Y-%m-%d")
        target = _next_session(pred_date, sessions)
        if target is None:
            skipped += 1
            continue

        rets = returns[target]
        crashed = 1 if (rets["spx"] <= SPX_CRASH_PCT or rets["ndx"] <= NDX_CRASH_PCT) else 0
        prob = float(p["crash_prob"]) / 100.0
        brier = (prob - crashed) ** 2

        # --- distribution-based scoring (scored against SPX realized move) ---
        realized_bucket = classify_bucket(rets["spx"])
        dist = p["distribution"] or {}
        # Defensive: zero out non-canonical keys, treat missing as 0.
        clean = {k: max(0.0, float(dist.get(k, 0) or 0)) for k in BUCKETS}
        s = sum(clean.values())
        if s > 0:
            clean = {k: v / s for k, v in clean.items()}
        else:
            clean = None
        if clean is None:
            predicted_bucket = None
            bucket_prob = None
            log_score = None
            multi_brier = None
        else:
            predicted_bucket = max(clean, key=clean.get)
            bucket_prob = clean[realized_bucket]
            log_score = -math.log(max(bucket_prob, 1e-6))
            multi_brier = sum(
                (clean[k] - (1.0 if k == realized_bucket else 0.0)) ** 2
                for k in BUCKETS
            )

        expected = p.get("expected_move_pct")
        if expected is None:
            directional_hit = None
        else:
            # Treat |expected| < 0.05% as a non-call; nothing to score.
            if abs(expected) < 0.05:
                directional_hit = 1 if abs(rets["spx"]) < 0.5 else 0
            else:
                directional_hit = 1 if (expected > 0) == (rets["spx"] > 0) else 0

        conn.execute(
            "INSERT OR REPLACE INTO prediction_outcomes ("
            "prediction_id, prediction_at, target_session, spx_return_pct, ndx_return_pct, "
            "crashed, crash_prob, brier, scored_at, "
            "realized_bucket, predicted_bucket, bucket_prob, log_score, multi_brier, "
            "directional_hit, expected_move_pct"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["id"], p["created_at"].isoformat(), target, rets["spx"], rets["ndx"],
             crashed, p["crash_prob"], brier, now_iso,
             realized_bucket, predicted_bucket, bucket_prob, log_score, multi_brier,
             directional_hit, expected),
        )
        scored += 1

    conn.commit()
    conn.close()
    print(f"[backtest] scored {scored}, skipped (next session not closed yet) {skipped}")
    return {"scored": scored, "skipped": skipped}


if __name__ == "__main__":
    run()
