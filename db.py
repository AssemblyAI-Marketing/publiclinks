"""Tiny data layer for short links.

Uses Postgres when DATABASE_URL (or POSTGRES_URL) is set — that's production on
Vercel (Neon via the Marketplace). Falls back to a local SQLite file for dev.
SQL is written with %s placeholders and translated for SQLite.
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
SQLITE_PATH = os.getenv("SQLITE_PATH", "publiclinks.db")

SCHEMA = ["""
CREATE TABLE IF NOT EXISTS links (
    key        TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'app'
)
""", """
CREATE TABLE IF NOT EXISTS api_keys (
    id         TEXT PRIMARY KEY,
    key_hash   TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used  TEXT
)
"""]

_schema_ready = False


class KeyTaken(Exception):
    pass


def _connect():
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(DATABASE_URL, row_factory=dict_row)  # type: ignore[arg-type]
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _run(sql: Any, params: tuple = (), fetch: str | None = None) -> Any:
    global _schema_ready
    if not DATABASE_URL:
        sql = sql.replace("%s", "?")
    conn = _connect()
    try:
        if not _schema_ready:
            for stmt in SCHEMA:
                conn.execute(stmt)
            _schema_ready = True
        cur = conn.execute(sql, params)
        if fetch == "one":
            row = cur.fetchone()
            result = dict(row) if row else None
        elif fetch == "all":
            result = [dict(r) for r in cur.fetchall()]
        else:
            result = cur.rowcount
        conn.commit()
        return result
    except Exception as e:
        if _is_unique_violation(e):
            raise KeyTaken() from e
        raise
    finally:
        conn.close()


def _is_unique_violation(e: Exception) -> bool:
    if isinstance(e, sqlite3.IntegrityError):
        return True
    return getattr(e, "sqlstate", None) == "23505"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_link(key: str) -> dict | None:
    row = _run("SELECT * FROM links WHERE key = %s", (key,), fetch="one")
    if row is None:
        # dub.co keys were effectively case-insensitive; keep old links working.
        row = _run(
            "SELECT * FROM links WHERE lower(key) = lower(%s) LIMIT 1", (key,), fetch="one"
        )
    return row


def list_links() -> list[dict]:
    return _run("SELECT * FROM links ORDER BY created_at DESC", fetch="all")


def create_link(
    key: str,
    url: str,
    created_by: str | None,
    source: str = "app",
    created_at: str | None = None,
    skip_existing: bool = False,
) -> bool:
    """Insert a link. Raises KeyTaken unless skip_existing (then returns False)."""
    ts = created_at or _now()
    conflict = " ON CONFLICT (key) DO NOTHING" if skip_existing else ""
    n = _run(
        "INSERT INTO links (key, url, created_by, created_at, updated_at, source)"
        " VALUES (%s, %s, %s, %s, %s, %s)" + conflict,
        (key, url, created_by, ts, _now(), source),
    )
    return n > 0


def update_link(old_key: str, new_key: str, url: str) -> bool:
    n = _run(
        "UPDATE links SET key = %s, url = %s, updated_at = %s WHERE key = %s",
        (new_key, url, _now(), old_key),
    )
    return n > 0


def delete_link(key: str) -> bool:
    return _run("DELETE FROM links WHERE key = %s", (key,)) > 0


# --- API keys (only the sha256 hash is stored) ---

def create_api_key(key_id: str, key_hash: str, name: str, created_by: str) -> None:
    _run(
        "INSERT INTO api_keys (id, key_hash, name, created_by, created_at) VALUES (%s, %s, %s, %s, %s)",
        (key_id, key_hash, name, created_by, _now()),
    )


def email_for_api_key(key_hash: str) -> str | None:
    row = _run("SELECT id, created_by FROM api_keys WHERE key_hash = %s", (key_hash,), fetch="one")
    if not row:
        return None
    _run("UPDATE api_keys SET last_used = %s WHERE id = %s", (_now(), row["id"]))
    return row["created_by"]


def list_api_keys(created_by: str) -> list[dict]:
    return _run(
        "SELECT id, name, created_at, last_used FROM api_keys WHERE created_by = %s ORDER BY created_at DESC",
        (created_by,),
        fetch="all",
    )


def delete_api_key(key_id: str, created_by: str) -> bool:
    return _run("DELETE FROM api_keys WHERE id = %s AND created_by = %s", (key_id, created_by)) > 0
