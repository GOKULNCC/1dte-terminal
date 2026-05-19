"""
Fetch 0DTE / nearest-expiry option chain for SPX & NDX from IBKR (OPRA),
store bid / ask / last / IV / delta / gamma / theta / vega per strike.

Run manually or from the scheduler. Idempotent: upserts by
(symbol, expiry, strike, right).
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ib_insync import Index, Option

from config import db_connect
from ibkr_client import ib_session, safe_float

ET = ZoneInfo("America/New_York")

# Per-symbol option-chain conventions for cash-settled weekly index options.
SYMBOL_SPECS = {
    "SPX": {"exchange": "CBOE", "trading_class": "SPXW", "strike_step": 5,   "currency": "USD"},
    "NDX": {"exchange": "NASDAQ", "trading_class": "NDX",  "strike_step": 25,  "currency": "USD"},
}

# How many strikes above & below ATM to fetch. SPX ±20 ≈ ±1.7% at 5800.
STRIKE_HALF_WIDTH = 20


def _init_option_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS option_chain (
            symbol             TEXT NOT NULL,
            expiry             TEXT NOT NULL,   -- YYYYMMDD as IBKR returns it
            strike             REAL NOT NULL,
            right              TEXT NOT NULL,   -- 'C' or 'P'
            bid                REAL,
            ask                REAL,
            last               REAL,
            mid                REAL,
            iv                 REAL,
            delta              REAL,
            gamma              REAL,
            theta              REAL,
            vega               REAL,
            underlying_price   REAL,
            fetched_at         TEXT NOT NULL,
            PRIMARY KEY (symbol, expiry, strike, right)
        )
    """)
    conn.commit()


def _today_et_yyyymmdd() -> str:
    return datetime.now(timezone.utc).astimezone(ET).strftime("%Y%m%d")


def _pick_expiry(expirations: list[str], min_yyyymmdd: str) -> Optional[str]:
    valid = sorted([e for e in expirations if e >= min_yyyymmdd])
    return valid[0] if valid else None


def fetch_chain(symbol: str, max_strikes_each_side: int = STRIKE_HALF_WIDTH) -> dict:
    spec = SYMBOL_SPECS.get(symbol)
    if not spec:
        raise ValueError(f"unsupported symbol {symbol}")

    out: dict = {"symbol": symbol, "rows": [], "spot": None, "expiry": None}

    with ib_session() as ib:
        # Market data type 4 = delayed-frozen. Subscribed feeds (OPRA options)
        # still return real-time; unsubscribed feeds (SPX index spot — needs
        # CBOE Streaming Market Indices, which retail accounts usually skip)
        # return 15-min-delayed or last frozen value. Spot is only used to
        # pick which strikes to fetch (ATM +/- N), so delayed is fine.
        ib.reqMarketDataType(4)

        # --- spot ---
        idx = Index(symbol, spec["exchange"], spec["currency"])
        [idx] = ib.qualifyContracts(idx)
        spot_ticker = ib.reqMktData(idx, "", False, False)
        ib.sleep(3.0)
        spot = (safe_float(spot_ticker.last)
                or safe_float(spot_ticker.close)
                or safe_float(spot_ticker.marketPrice()))
        ib.cancelMktData(idx)

        # Fallback: if IB still won't give us a spot (out of hours, no delayed
        # cache yet), read the latest yfinance-populated value from indices.
        if not spot:
            from config import db_connect
            conn = db_connect(row_factory=True)
            try:
                name = "S&P 500" if symbol == "SPX" else "NASDAQ"
                row = conn.execute("SELECT price FROM indices WHERE name = ?", (name,)).fetchone()
                if row and row["price"]:
                    spot = float(row["price"])
                    print(f"[ibkr_options] {symbol}: using fallback spot {spot:.2f} from indices table")
            finally:
                conn.close()
        if not spot:
            raise RuntimeError(f"could not get spot for {symbol} (no IB data and no indices fallback)")
        out["spot"] = spot

        # --- chain params (find nearest expiry & valid strikes) ---
        chains = ib.reqSecDefOptParams(idx.symbol, "", idx.secType, idx.conId)
        chain = next(
            (c for c in chains
             if c.tradingClass == spec["trading_class"]
                and c.exchange in ("SMART", spec["exchange"])),
            None,
        ) or (chains[0] if chains else None)
        if chain is None:
            raise RuntimeError(f"no option chain params returned for {symbol}")

        expiry = _pick_expiry(list(chain.expirations), _today_et_yyyymmdd())
        if not expiry:
            raise RuntimeError(f"no upcoming expiry for {symbol}")
        out["expiry"] = expiry

        step = spec["strike_step"]
        atm = round(spot / step) * step
        wanted = {
            atm + i * step
            for i in range(-max_strikes_each_side, max_strikes_each_side + 1)
        }
        strikes = sorted(s for s in chain.strikes if s in wanted)
        if not strikes:
            raise RuntimeError(f"no strikes in window for {symbol}")

        # --- build & qualify contracts ---
        contracts = [
            Option(symbol, expiry, s, r,
                   "SMART", "100", spec["currency"],
                   tradingClass=spec["trading_class"])
            for s in strikes for r in ("C", "P")
        ]
        contracts = ib.qualifyContracts(*contracts)

        # --- subscribe streaming mkt data, wait, harvest, cancel ---
        tickers = [ib.reqMktData(c, "", False, False) for c in contracts]
        # 4s is usually enough for OPRA bid/ask + greeks to populate.
        ib.sleep(4.0)

        now_iso = datetime.now().isoformat()
        for t in tickers:
            c = t.contract
            bid  = safe_float(t.bid)
            ask  = safe_float(t.ask)
            last = safe_float(t.last)
            mid  = ((bid + ask) / 2.0) if (bid is not None and ask is not None and bid > 0 and ask > 0) else None
            g    = t.modelGreeks
            row = {
                "symbol":           symbol,
                "expiry":           c.lastTradeDateOrContractMonth,
                "strike":           float(c.strike),
                "right":            c.right,
                "bid":              bid,
                "ask":              ask,
                "last":             last,
                "mid":              mid,
                "iv":               safe_float(g.impliedVol) if g else None,
                "delta":            safe_float(g.delta)      if g else None,
                "gamma":            safe_float(g.gamma)      if g else None,
                "theta":            safe_float(g.theta)      if g else None,
                "vega":             safe_float(g.vega)       if g else None,
                "underlying_price": spot,
                "fetched_at":       now_iso,
            }
            out["rows"].append(row)
            ib.cancelMktData(c)

    return out


def save_chain(conn, payload: dict) -> int:
    rows = payload["rows"]
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO option_chain ("
            "symbol, expiry, strike, right, bid, ask, last, mid, iv, "
            "delta, gamma, theta, vega, underlying_price, fetched_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["symbol"], r["expiry"], r["strike"], r["right"],
                r["bid"], r["ask"], r["last"], r["mid"], r["iv"],
                r["delta"], r["gamma"], r["theta"], r["vega"],
                r["underlying_price"], r["fetched_at"],
            ),
        )
    conn.commit()
    return len(rows)


def run(symbols: list[str]) -> None:
    conn = db_connect()
    _init_option_table(conn)

    for sym in symbols:
        t0 = time.time()
        try:
            payload = fetch_chain(sym)
        except Exception as exc:
            print(f"[ibkr_options] {sym}: FAILED ({exc.__class__.__name__}: {exc})")
            continue
        n = save_chain(conn, payload)
        dt = time.time() - t0
        print(f"[ibkr_options] {sym}: expiry {payload['expiry']}, spot {payload['spot']:.2f}, "
              f"saved {n} rows in {dt:.1f}s")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["SPX", "NDX"],
                    help="index symbols to fetch (default: SPX NDX)")
    args = ap.parse_args()
    run(args.symbols)
