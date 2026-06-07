from __future__ import annotations

import sqlite3

from .errors import USAPError


def require_lastrowid(cur: sqlite3.Cursor) -> int:
    """
    Return cursor.lastrowid as int, or fail loudly if SQLite did not produce one.
    """
    if cur.lastrowid is None:
        raise USAPError("Expected SQLite lastrowid, but got None.")

    return cur.lastrowid