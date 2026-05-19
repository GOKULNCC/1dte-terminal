"""
IBKR connection helper.

Use as a context manager so the socket is always cleaned up:

    from ibkr_client import ib_session
    with ib_session() as ib:
        ...

Connect failures raise; callers should let them propagate so the scheduler
records a clean failure rather than scribbling stale data.
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from typing import Optional

from ib_insync import IB

from config import IB_HOST, IB_PORT, IB_CLIENT_ID


@contextmanager
def ib_session(
    host: str = IB_HOST,
    port: int = IB_PORT,
    client_id: int = IB_CLIENT_ID,
    timeout: float = 10.0,
):
    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=timeout)
    try:
        yield ib
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def safe_float(v) -> Optional[float]:
    """ib_insync uses float('nan') for 'no data'. Normalize to None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f
