"""The encrypted database and the shared core tables.

Every connection to wrh.db goes through :func:`connect`, which applies the
SQLCipher key before anything else touches the file. Use ``sqlcipher3`` (not
the standard library ``sqlite3``) for anything that reads wrh.db.

Core tables (owned by this module, not by any source):

* ``raw_files``  - one row per archived raw file, so anything can be re-imported.
* ``timeline``   - one row per event from any source, with the shared core
  fields: ts, source, title, body, url (the link back to the original), plus
  source_table/source_key pointing at the full row in the source's own table.
* ``timeline_fts`` - an FTS5 index over timeline(title, body, url), kept in
  sync by triggers. Datasette picks it up automatically for the search box.
"""

from __future__ import annotations

import os
from pathlib import Path

import sqlcipher3.dbapi2 as sqlcipher

from . import keys

DatabaseError = sqlcipher.DatabaseError

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_files (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,            -- relative to the raw/ directory
    original_name TEXT,
    meta TEXT,                     -- JSON from the collector (device, profile, ...)
    archived_at TEXT NOT NULL,
    imported_at TEXT
);

CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,              -- see timeutil.py: 2026-11-18T01:12:12.000000Z
    source TEXT NOT NULL,          -- e.g. 'browser'
    source_table TEXT NOT NULL,    -- e.g. 'browser_visits'
    source_key TEXT NOT NULL,      -- primary key of the row in source_table
    title TEXT,
    body TEXT,
    url TEXT,                      -- link or reference back to the original
    UNIQUE (source_table, source_key)
);
CREATE INDEX IF NOT EXISTS timeline_ts ON timeline(ts);
CREATE INDEX IF NOT EXISTS timeline_source_ts ON timeline(source, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS timeline_fts USING fts5(
    title, body, url,
    content='timeline', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS timeline_ai AFTER INSERT ON timeline BEGIN
    INSERT INTO timeline_fts(rowid, title, body, url)
    VALUES (new.id, new.title, new.body, new.url);
END;
CREATE TRIGGER IF NOT EXISTS timeline_ad AFTER DELETE ON timeline BEGIN
    INSERT INTO timeline_fts(timeline_fts, rowid, title, body, url)
    VALUES ('delete', old.id, old.title, old.body, old.url);
END;
CREATE TRIGGER IF NOT EXISTS timeline_au AFTER UPDATE ON timeline BEGIN
    INSERT INTO timeline_fts(timeline_fts, rowid, title, body, url)
    VALUES ('delete', old.id, old.title, old.body, old.url);
    INSERT INTO timeline_fts(rowid, title, body, url)
    VALUES (new.id, new.title, new.body, new.url);
END;
"""


def apply_key(conn, key: str) -> None:
    conn.execute(keys.pragma(key))


def connect(path: str | os.PathLike, key: str | None = None, *, create: bool = False):
    """Open wrh.db with its key and check the key is right.

    Raises FileNotFoundError if the file is missing and ``create`` is false,
    and DatabaseError if the key is wrong.
    """
    path = Path(path)
    if not create and not path.exists():
        raise FileNotFoundError(f"{path} does not exist. Run `wrh init` first.")
    key = key or keys.get_key()
    conn = sqlcipher.connect(str(path))
    apply_key(conn, key)
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except DatabaseError as e:
        conn.close()
        raise DatabaseError(f"Could not open {path}: wrong key, or not a wrh database ({e})") from e
    conn.row_factory = sqlcipher.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if create:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_core(conn) -> None:
    conn.executescript(CORE_SCHEMA)
    conn.commit()


def upsert_timeline(conn, source: str, source_table: str, rows) -> int:
    """Insert or update timeline rows.

    ``rows`` yields mappings with keys ts, source_key, title, body, url.
    Idempotent: re-importing the same data updates rows in place.
    """
    sql = """
    INSERT INTO timeline (ts, source, source_table, source_key, title, body, url)
    VALUES (:ts, :source, :source_table, :source_key, :title, :body, :url)
    ON CONFLICT (source_table, source_key) DO UPDATE SET
        ts = excluded.ts, title = excluded.title, body = excluded.body, url = excluded.url
    WHERE timeline.ts IS NOT excluded.ts OR timeline.title IS NOT excluded.title
       OR timeline.body IS NOT excluded.body OR timeline.url IS NOT excluded.url
    """
    n = 0
    for r in rows:
        conn.execute(
            sql,
            {
                "ts": r["ts"],
                "source": source,
                "source_table": source_table,
                "source_key": str(r["source_key"]),
                "title": r["title"],
                "body": r["body"],
                "url": r["url"],
            },
        )
        n += 1
    return n


def delete_timeline(conn, source: str) -> None:
    conn.execute("DELETE FROM timeline WHERE source = ?", (source,))
