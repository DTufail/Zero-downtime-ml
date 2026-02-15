import logging
import os
import sqlite3
import time

from app.config import settings

logger = logging.getLogger(__name__)


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                message TEXT,
                response TEXT,
                tokens_generated INTEGER,
                inference_ms REAL,
                timestamp REAL
            )
        """)
        conn.commit()
        logger.info("Database initialized")
    finally:
        conn.close()


def save_conversation(
    user_id: str,
    message: str,
    response: str,
    tokens_generated: int,
    inference_ms: float,
) -> int:
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO conversations (user_id, message, response, tokens_generated, inference_ms, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, message, response, tokens_generated, inference_ms, time.time()),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_conversation_history(user_id: str, limit: int = 10) -> list[dict]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
