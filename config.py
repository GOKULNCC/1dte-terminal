"""Shared configuration and DB helpers.

Centralizes paths and connection settings so scripts don't drift.
"""
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
DB_PATH = str(PROJECT_ROOT / "trading.db")

OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
QWEN_MODEL = "qwen3:30b"

API_HOST = "127.0.0.1"
API_PORT = 8088

# Interactive Brokers gateway (live trading account).
# Default port 4001 = IB Gateway live; 4002 = paper; 7496/7497 for TWS.
IB_HOST      = "127.0.0.1"
IB_PORT      = 4001
IB_CLIENT_ID = 17       # arbitrary; pick a value not used by other tools


def db_connect(row_factory: bool = False) -> sqlite3.Connection:
    """Open SQLite with WAL + sane defaults.

    WAL lets readers run concurrently with a single writer, which matters
    because the scheduler, the on-demand refresh worker, and the API server
    all touch trading.db.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn
