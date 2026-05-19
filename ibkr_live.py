"""
Live SPX / VIX tick streamer via IBKR + CBOE Streaming Market Indexes.

Runs as a long-lived process: connects to IB Gateway, subscribes to real-time
market data for SPX and VIX, and writes the latest tick into `live_ticks`
every ~1 second.  The API server reads from this table to feed the dashboard
with sub-second pricing instead of yfinance's 15-min delay.

Usage:
    python ibkr_live.py              # streams SPX + VIX
    python ibkr_live.py --symbols SPX  # SPX only

Requires CBOE Streaming Market Indexes subscription ($3.50/mo) for real-time
SPX/VIX index values.  Without it, IB returns delayed-frozen data.
"""
from __future__ import annotations

import argparse
import signal
import time
import threading
from datetime import datetime

from ib_insync import IB, Index, util

from config import db_connect, IB_HOST, IB_PORT, IB_CLIENT_ID

import random
# Use a random clientId so this doesn't conflict with the option-chain fetcher or zombie processes.
LIVE_CLIENT_ID = IB_CLIENT_ID + random.randint(100, 9999)

# Symbols to stream and their IBKR contract specs.
SYMBOL_SPECS = {
    "SPX": {"exchange": "CBOE",  "currency": "USD", "index_name": "S&P 500"},
    "VIX": {"exchange": "CBOE",  "currency": "USD", "index_name": "VIX"},
}

# How often (seconds) to flush the latest tick to SQLite.
FLUSH_INTERVAL = 1.0


def _init_live_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_ticks (
            symbol           TEXT PRIMARY KEY,
            price            REAL,
            change_pct       REAL,
            bid              REAL,
            ask              REAL,
            high             REAL,
            low              REAL,
            close_prior      REAL,
            volume           INTEGER,
            updated_at       TEXT NOT NULL
        )
    """)
    conn.commit()


class LiveStreamer:
    """Connects to IB Gateway, subscribes to index ticks, flushes to SQLite."""

    def __init__(self, symbols: list[str]):
        self.symbols = [s.upper() for s in symbols if s.upper() in SYMBOL_SPECS]
        self._ib: IB | None = None
        self._tickers: dict[str, object] = {}   # symbol → ib_insync Ticker
        self._stop = threading.Event()

    # --- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self._ib = IB()
        print(f"[ibkr_live] Connecting to IB Gateway {IB_HOST}:{IB_PORT} "
              f"(clientId={LIVE_CLIENT_ID})...")
        self._ib.connect(IB_HOST, IB_PORT, clientId=LIVE_CLIENT_ID, timeout=15)
        print(f"[ibkr_live] Connected.")

        # Type 4 = Delayed Frozen (will use live if subscribed, otherwise gracefully falls back to delayed data)
        self._ib.reqMarketDataType(4)

        conn = db_connect()
        _init_live_table(conn)
        conn.close()

        for sym in self.symbols:
            spec = SYMBOL_SPECS[sym]
            contract = Index(sym, spec["exchange"], spec["currency"])
            [contract] = self._ib.qualifyContracts(contract)
            ticker = self._ib.reqMktData(contract, "", False, False)
            self._tickers[sym] = ticker
            print(f"[ibkr_live] Subscribed to {sym} ({spec['exchange']})")

        print(f"[ibkr_live] Streaming {', '.join(self.symbols)}. "
              f"Flushing every {FLUSH_INTERVAL}s. Ctrl+C to stop.\n")

    def run_forever(self) -> None:
        """Main loop: sleep → flush ticks → repeat."""
        while not self._stop.is_set():
            self._ib.sleep(FLUSH_INTERVAL)
            self._flush()

    def stop(self) -> None:
        self._stop.set()
        if self._ib and self._ib.isConnected():
            for sym, ticker in self._tickers.items():
                try:
                    self._ib.cancelMktData(ticker.contract)
                except Exception:
                    pass
            try:
                self._ib.disconnect()
            except Exception:
                pass
        print("\n[ibkr_live] Disconnected.")

    # --- flush ---------------------------------------------------------------
    def _flush(self) -> None:
        try:
            conn = db_connect()
            now_iso = datetime.now().isoformat()

            for sym, ticker in self._tickers.items():
                price = _best_price(ticker)
                if price is None:
                    continue

                close_prior = _sf(ticker.close)
                change_pct = None
                if price and close_prior and close_prior > 0:
                    change_pct = round((price - close_prior) / close_prior * 100.0, 4)

                conn.execute(
                    "INSERT OR REPLACE INTO live_ticks "
                    "(symbol, price, change_pct, bid, ask, high, low, close_prior, volume, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        sym,
                        price,
                        change_pct,
                        _sf(ticker.bid),
                        _sf(ticker.ask),
                        _sf(ticker.high),
                        _sf(ticker.low),
                        close_prior,
                        int(ticker.volume) if ticker.volume and ticker.volume == ticker.volume else None,
                        now_iso,
                    ),
                )

                # Also update the main indices table so the rest of the system
                # (predictor, sizer, dashboard panels) automatically picks up
                # real-time SPX without any code changes.
                index_name = SYMBOL_SPECS[sym]["index_name"]
                conn.execute(
                    "INSERT OR REPLACE INTO indices (symbol, name, price, change_pct, fetched_at) "
                    "VALUES (?,?,?,?,?)",
                    (f"^{sym}", index_name, price, change_pct or 0.0, now_iso),
                )

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[ibkr_live] Database flush error: {e}")
            return

        # Print a compact tick line.
        parts = []
        for sym in self.symbols:
            t = self._tickers[sym]
            p = _best_price(t)
            if p:
                cp = _sf(t.close)
                chg = f" ({(p - cp) / cp * 100:+.2f}%)" if cp and cp > 0 else ""
                parts.append(f"{sym} {p:,.2f}{chg}")
        if parts:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] {' | '.join(parts)}", end="\r")


def _sf(v) -> float | None:
    """ib_insync NaN → None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _best_price(ticker) -> float | None:
    """Pick the best available price from the ticker."""
    for attr in ("last", "close", "bid", "ask"):
        v = _sf(getattr(ticker, attr, None))
        if v and v > 0:
            return v
    mp = _sf(ticker.marketPrice()) if hasattr(ticker, "marketPrice") else None
    return mp if mp and mp > 0 else None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["SPX", "VIX"],
                    help="index symbols to stream (default: SPX VIX)")
    args = ap.parse_args()

    while True:
        streamer = LiveStreamer(args.symbols)
        # Graceful shutdown on Ctrl+C.
        signal.signal(signal.SIGINT, lambda *_: streamer.stop())
        signal.signal(signal.SIGTERM, lambda *_: streamer.stop())

        try:
            streamer.start()
            streamer.run_forever()
        except (KeyboardInterrupt, SystemExit):
            streamer.stop()
            break
        except Exception as e:
            print(f"[ibkr_live] Streamer crashed: {e}. Restarting in 5s...")
            streamer.stop()
            time.sleep(5)


if __name__ == "__main__":
    main()
