"""Database migration: create tables required by the Hold Agent.

Usage:
    python -m rookiecard.db.migrate
"""

from rookiecard.db.connection import get_db


SCHEMA_SQL = """
-- ══════════════════════════════════════
-- Core Tables
-- ══════════════════════════════════════

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    team TEXT,
    draft_year INTEGER NOT NULL,
    draft_position INTEGER NOT NULL,
    height TEXT,
    weight TEXT,
    college TEXT,
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ══════════════════════════════════════
-- Card Market Tables
-- ══════════════════════════════════════

CREATE TABLE IF NOT EXISTS card_sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    series TEXT NOT NULL,
    parallel TEXT NOT NULL,
    grade TEXT NOT NULL,
    price REAL NOT NULL,
    sale_date DATE NOT NULL,
    listing_type TEXT,
    platform TEXT DEFAULT 'ebay',
    url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(url)
);

CREATE TABLE IF NOT EXISTS pop_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    set_name TEXT NOT NULL,
    grade_10 INTEGER DEFAULT 0,
    grade_9 INTEGER DEFAULT 0,
    grade_8 INTEGER DEFAULT 0,
    grade_7 INTEGER DEFAULT 0,
    grade_below_7 INTEGER DEFAULT 0,
    total_graded INTEGER DEFAULT 0,
    psa_10_ratio REAL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(player_id, set_name, fetched_at)
);

-- ══════════════════════════════════════
-- Portfolio Tables
-- ══════════════════════════════════════

CREATE TABLE IF NOT EXISTS portfolio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    series TEXT NOT NULL,
    parallel TEXT NOT NULL,
    grade TEXT NOT NULL,
    buy_price REAL NOT NULL,
    buy_date DATE NOT NULL,
    sell_price REAL,
    sell_date DATE,
    status TEXT DEFAULT 'active',
    notes TEXT,
    target_price REAL,
    search_query TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trade_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolio(id),
    action TEXT NOT NULL,
    price REAL NOT NULL,
    date DATE NOT NULL,
    agent_signal TEXT,
    agent_confidence REAL,
    actual_return_pct REAL,
    holding_days INTEGER,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ══════════════════════════════════════
-- Indexes
-- ══════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_card_sales_lookup ON card_sales(player_id, series, parallel, grade, sale_date);
CREATE INDEX IF NOT EXISTS idx_portfolio_status ON portfolio(status);
"""

EXPECTED_TABLES = ["players", "card_sales", "pop_reports", "portfolio", "trade_journal"]


def create_tables(db_path: str | None = None):
    """Create all tables. Idempotent — safe to run multiple times."""
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA_SQL)


def verify_tables(db_path: str | None = None) -> list[str]:
    """Return list of existing table names."""
    with get_db(db_path) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [row["name"] for row in cursor.fetchall()]


if __name__ == "__main__":
    from pathlib import Path
    from rookiecard.config import Config

    db_dir = Path(Config.DB_PATH).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    print(f"Creating database at {Config.DB_PATH}...")
    create_tables()
    tables = verify_tables()
    print(f"Created {len(tables)} tables: {', '.join(tables)}")
