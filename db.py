"""
db.py — SQLite article cache.

Schema:
  articles(
    url        TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    text       TEXT NOT NULL,
    category   TEXT NOT NULL,
    summary    TEXT NOT NULL,
    published  TEXT NOT NULL,   -- formatted time string e.g. "09:45 AM"
    fetched_at TEXT NOT NULL,   -- ISO date "2025-04-13"
    region     TEXT NOT NULL DEFAULT 'MY'  -- 'MY' = Malaysia, 'GLOBAL' = worldwide
  )

One row per URL. On each run, URLs already present for today are returned
from the DB instantly; only new URLs go through the LLM.
"""

from zoneinfo import ZoneInfo
import sqlite3
import logging
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "news.db"


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).strftime("%Y-%m-%d")


@contextmanager
def _conn():
    """Yield a thread-safe connection with WAL mode enabled."""
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    """Create the articles table if it doesn't exist."""
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                url        TEXT PRIMARY KEY,
                title      TEXT NOT NULL,
                text       TEXT NOT NULL,
                category   TEXT NOT NULL,
                summary    TEXT NOT NULL,
                published  TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                region     TEXT NOT NULL DEFAULT 'MY'
            )
        """)
    logger.info("DB initialised at %s", DB_PATH)
    _migrate_db()


def _migrate_db() -> None:
    """Add new columns to existing DBs without losing data."""
    with _conn() as con:
        cols = {row[1] for row in con.execute("PRAGMA table_info(articles)").fetchall()}
        if "region" not in cols:
            con.execute("ALTER TABLE articles ADD COLUMN region TEXT NOT NULL DEFAULT 'MY'")
            logger.info("Migrated DB: added 'region' column")


def get_today_urls(region: str = "MY") -> set:
    """Return the set of URLs already cached for today (for a given region)."""
    with _conn() as con:
        rows = con.execute(
            "SELECT url FROM articles WHERE fetched_at = ? AND region = ?",
            (_today(), region),
        ).fetchall()
    return {row["url"] for row in rows}


def get_today_articles(region: str = "MY") -> list[dict]:
    """Return all cached articles for today as a list of dicts."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM articles WHERE fetched_at = ? AND region = ? ORDER BY published",
            (_today(), region),
        ).fetchall()
    return [dict(row) for row in rows]


def save_article(url: str, title: str, text: str,
                 category: str, summary: str, published: str,
                 region: str = "MY") -> None:
    """Insert or replace one article for today."""
    with _conn() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO articles
                (url, title, text, category, summary, published, fetched_at, region)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (url, title, text, category, summary, published, _today(), region),
        )


def save_articles(articles: list[dict], region: str = "MY") -> None:
    """Bulk-save a list of article dicts (must have all required keys)."""
    today = _today()
    with _conn() as con:
        con.executemany(
            """
            INSERT OR REPLACE INTO articles
                (url, title, text, category, summary, published, fetched_at, region)
            VALUES (:url, :title, :text, :category, :summary, :published, :fetched_at, :region)
            """,
            [{**a, "fetched_at": today, "region": region} for a in articles],
        )
    logger.info("Saved %d articles to DB (region=%s)", len(articles), region)


def clear_today(region: str = "MY") -> int:
    """Delete today's articles for a given region. Returns rows deleted."""
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM articles WHERE fetched_at = ? AND region = ?",
            (_today(), region),
        )
    logger.info("Cleared %d cached articles for today (region=%s)", cur.rowcount, region)
    return cur.rowcount


def prune_old(keep_days: int = 3) -> int:
    """Delete articles older than keep_days. Returns rows deleted."""
    with _conn() as con:
        cur = con.execute(
            """
            DELETE FROM articles
            WHERE fetched_at < date('now', ?)
            """,
            (f"-{keep_days} days",),
        )
    if cur.rowcount:
        logger.info("Pruned %d articles older than %d days", cur.rowcount, keep_days)
    return cur.rowcount