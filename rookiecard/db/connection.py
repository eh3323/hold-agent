import sqlite3
from contextlib import contextmanager
from rookiecard.config import Config


@contextmanager
def get_db(db_path: str | None = None):
    """Context manager for SQLite database connections.

    Usage:
        with get_db() as conn:
            conn.execute("SELECT * FROM players")
    """
    path = db_path or Config.DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
