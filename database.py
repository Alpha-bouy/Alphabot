"""
database.py — SQLite persistence for positions, trade history, and bot stats.
Thread-safe using a module-level lock. No ORM needed — raw SQL is fastest.
"""

import sqlite3
import json
import threading
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from enum import Enum

DB_PATH = "cricket_bot.db"
_lock = threading.Lock()


# ── Enums ─────────────────────────────────────────────────────────────────

class PositionStatus(str, Enum):
    PENDING_BUY    = "pending_buy"      # Order placed, waiting for fill
    OPEN           = "open"             # Filled, monitoring for stop loss
    CLOSING        = "closing"          # Stop loss sell order placed
    CLOSED_WIN     = "closed_win"       # Market resolved — we won
    CLOSED_STOPLOSS= "closed_stoploss"  # Sold at stop loss (5% loss)
    CLOSED_MANUAL  = "closed_manual"    # Manually closed via Telegram


# ── Dataclass ─────────────────────────────────────────────────────────────

@dataclass
class Position:
    market_id:        str
    condition_id:     str
    token_id:         str           # YES token ID on Polymarket CLOB
    question:         str           # "Will India win vs Australia?"
    team_name:        str           # Which team we bet on
    tournament:       str           # "IPL 2025" / "ICC T20 WC" / etc.
    buy_price:        float         # Price paid per share (e.g. 0.90)
    shares:           float         # Number of shares bought
    usdc_spent:       float         # Total USDC committed
    buy_order_id:     str = ""      # Polymarket order ID
    sell_order_id:    str = ""      # Stop loss sell order ID (if placed)
    status:           str = PositionStatus.PENDING_BUY
    signal_score:     int = 0       # Signal engine score at time of entry
    last_price:       float = 0.0   # Last seen market price
    created_at:       str = ""
    updated_at:       str = ""
    closed_at:        str = ""
    pnl_usdc:         float = 0.0   # Realized P&L (set when closed)
    notes:            str = ""      # Any extra info


@dataclass
class TradeLog:
    """Immutable record written for every buy/sell action."""
    position_id:  int
    action:       str     # "BUY" | "SELL_STOPLOSS" | "RESOLVED_WIN"
    price:        float
    shares:       float
    usdc_amount:  float
    order_id:     str
    timestamp:    str


# ── DB Init ───────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    with _lock:
        conn = _connect()
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id     TEXT NOT NULL,
                condition_id  TEXT NOT NULL,
                token_id      TEXT NOT NULL,
                question      TEXT NOT NULL,
                team_name     TEXT NOT NULL,
                tournament    TEXT NOT NULL DEFAULT '',
                buy_price     REAL NOT NULL,
                shares        REAL NOT NULL,
                usdc_spent    REAL NOT NULL,
                buy_order_id  TEXT DEFAULT '',
                sell_order_id TEXT DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'pending_buy',
                signal_score  INTEGER DEFAULT 0,
                last_price    REAL DEFAULT 0,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                closed_at     TEXT DEFAULT '',
                pnl_usdc      REAL DEFAULT 0,
                notes         TEXT DEFAULT ''
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS trade_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id  INTEGER NOT NULL,
                action       TEXT NOT NULL,
                price        REAL NOT NULL,
                shares       REAL NOT NULL,
                usdc_amount  REAL NOT NULL,
                order_id     TEXT DEFAULT '',
                timestamp    TEXT NOT NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_stats (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Seed stats if missing
        for key, default in [
            ("total_trades", "0"),
            ("total_wins", "0"),
            ("total_stoploss_hits", "0"),
            ("total_pnl_usdc", "0.0"),
            ("bot_started_at", datetime.utcnow().isoformat()),
        ]:
            c.execute(
                "INSERT OR IGNORE INTO bot_stats (key, value) VALUES (?, ?)",
                (key, default)
            )

        conn.commit()
        conn.close()


# ── Internal helpers ──────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.utcnow().isoformat()


# ── Position CRUD ─────────────────────────────────────────────────────────

def insert_position(pos: Position) -> int:
    """Insert a new position. Returns the DB row id."""
    now = _now()
    with _lock:
        conn = _connect()
        c = conn.cursor()
        c.execute("""
            INSERT INTO positions
              (market_id, condition_id, token_id, question, team_name,
               tournament, buy_price, shares, usdc_spent, buy_order_id,
               sell_order_id, status, signal_score, last_price,
               created_at, updated_at, pnl_usdc, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            pos.market_id, pos.condition_id, pos.token_id,
            pos.question, pos.team_name, pos.tournament,
            pos.buy_price, pos.shares, pos.usdc_spent,
            pos.buy_order_id, pos.sell_order_id, pos.status,
            pos.signal_score, pos.last_price,
            now, now, pos.pnl_usdc, pos.notes,
        ))
        row_id = c.lastrowid
        conn.commit()
        conn.close()
        return row_id


def update_position(position_id: int, **kwargs) -> None:
    """Update any fields on a position by ID."""
    kwargs["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [position_id]
    with _lock:
        conn = _connect()
        conn.execute(f"UPDATE positions SET {cols} WHERE id = ?", vals)
        conn.commit()
        conn.close()


def get_open_positions() -> List[Dict]:
    """Return all positions with status OPEN or CLOSING."""
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT * FROM positions
            WHERE status IN ('pending_buy','open','closing')
            ORDER BY created_at DESC
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]


def get_all_positions(limit: int = 50) -> List[Dict]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM positions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]


def get_position_by_market(market_id: str) -> Optional[Dict]:
    """Return the most recent open/closing position for a market_id."""
    with _lock:
        conn = _connect()
        row = conn.execute("""
            SELECT * FROM positions
            WHERE market_id = ? AND status IN ('pending_buy','open','closing')
            ORDER BY created_at DESC LIMIT 1
        """, (market_id,)).fetchone()
        conn.close()
        return dict(row) if row else None


def position_exists_for_market(market_id: str) -> bool:
    return get_position_by_market(market_id) is not None


# ── Trade Log ──────────────────────────────────────────────────────────────

def log_trade(log: TradeLog) -> None:
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO trade_logs
              (position_id, action, price, shares, usdc_amount, order_id, timestamp)
            VALUES (?,?,?,?,?,?,?)
        """, (
            log.position_id, log.action, log.price,
            log.shares, log.usdc_amount, log.order_id, _now()
        ))
        conn.commit()
        conn.close()


def get_recent_trade_logs(limit: int = 20) -> List[Dict]:
    with _lock:
        conn = _connect()
        rows = conn.execute("""
            SELECT tl.*, p.question, p.team_name
            FROM trade_logs tl
            LEFT JOIN positions p ON tl.position_id = p.id
            ORDER BY tl.timestamp DESC LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]


# ── Stats ──────────────────────────────────────────────────────────────────

def get_stat(key: str) -> str:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT value FROM bot_stats WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return row["value"] if row else "0"


def set_stat(key: str, value: Any) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO bot_stats (key, value) VALUES (?,?)",
            (key, str(value))
        )
        conn.commit()
        conn.close()


def increment_stat(key: str, by: float = 1) -> None:
    current = float(get_stat(key))
    set_stat(key, current + by)


def get_pnl_summary() -> Dict:
    """Aggregate P&L stats for Telegram /pnl command."""
    with _lock:
        conn = _connect()

        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM positions WHERE status NOT IN ('pending_buy','open','closing')"
        ).fetchone()["cnt"]

        wins = conn.execute(
            "SELECT COUNT(*) as cnt FROM positions WHERE status = 'closed_win'"
        ).fetchone()["cnt"]

        stoploss = conn.execute(
            "SELECT COUNT(*) as cnt FROM positions WHERE status = 'closed_stoploss'"
        ).fetchone()["cnt"]

        pnl = conn.execute(
            "SELECT SUM(pnl_usdc) as total FROM positions WHERE status NOT IN ('pending_buy','open','closing')"
        ).fetchone()["total"] or 0.0

        open_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM positions WHERE status IN ('open','closing')"
        ).fetchone()["cnt"]

        open_exposure = conn.execute(
            "SELECT SUM(usdc_spent) as total FROM positions WHERE status IN ('open','closing')"
        ).fetchone()["total"] or 0.0

        conn.close()

    return {
        "total_closed": total,
        "wins": wins,
        "stoploss_hits": stoploss,
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0.0,
        "realized_pnl": round(float(pnl), 4),
        "open_positions": open_count,
        "open_exposure_usdc": round(float(open_exposure), 4),
    }
