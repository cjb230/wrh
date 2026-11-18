"""Browser history importer.

Reads a snapshot of a browser's own history database (made by the collector
in collector.py) and writes one row per page visit to ``browser_visits``.

Supported formats, detected from the tables in the file:

* Chromium family (Chrome, Edge, Brave, Arc): ``History`` with urls + visits.
  Times are microseconds since 1601-01-01 UTC.
* Firefox: ``places.sqlite`` with moz_places + moz_historyvisits.
  Times are microseconds since 1970-01-01 UTC.
* Safari: ``History.db`` with history_items + history_visits.
  Times are seconds since 2001-01-01 UTC.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3  # the raw snapshots are plain, unencrypted browser files
from pathlib import Path
from typing import Iterable, Iterator, Mapping
from urllib.parse import urlsplit

from ...timeutil import to_iso as iso
from ..base import Source

SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_visits (
    id TEXT PRIMARY KEY,           -- stable hash, see visit_id()
    ts TEXT NOT NULL,              -- ISO 8601 UTC
    url TEXT NOT NULL,
    title TEXT,
    domain TEXT,
    browser TEXT NOT NULL,         -- chrome, edge, brave, arc, firefox, safari
    profile TEXT,
    device TEXT,
    transition TEXT,               -- how you got there: link, typed, reload, ...
    duration_s REAL,               -- Chromium only
    source_visit_id INTEGER,       -- the visit's id inside the browser's own db
    raw_file_id INTEGER REFERENCES raw_files(id)
);
CREATE INDEX IF NOT EXISTS browser_visits_ts ON browser_visits(ts);
CREATE INDEX IF NOT EXISTS browser_visits_domain ON browser_visits(domain);
CREATE INDEX IF NOT EXISTS browser_visits_raw ON browser_visits(raw_file_id);
"""

CHROME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
UNIX_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
COCOA_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)

CHROME_TRANSITIONS = {
    0: "link", 1: "typed", 2: "auto_bookmark", 3: "auto_subframe", 4: "manual_subframe",
    5: "generated", 6: "auto_toplevel", 7: "form_submit", 8: "reload", 9: "keyword",
    10: "keyword_generated",
}
FIREFOX_TRANSITIONS = {
    1: "link", 2: "typed", 3: "bookmark", 4: "embed", 5: "redirect_permanent",
    6: "redirect_temporary", 7: "download", 8: "framed_link", 9: "reload",
}
# Visits that are not really "pages you looked at". Skipped on import.
SKIP_TRANSITIONS = {"auto_subframe", "embed"}


def detect_format(conn: sqlite3.Connection) -> str:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {"urls", "visits"} <= tables:
        return "chromium"
    if {"moz_places", "moz_historyvisits"} <= tables:
        return "firefox"
    if {"history_items", "history_visits"} <= tables:
        return "safari"
    raise ValueError("Not a recognised browser history database")


def _domain(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if host and host.startswith("www."):
        host = host[4:]
    return host


def read_visits(path: Path) -> tuple[str, Iterator[dict]]:
    """Return (format, iterator of visit dicts) for a browser history file."""
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro&immutable=1", uri=True)
    fmt = detect_format(conn)
    reader = {"chromium": _chromium, "firefox": _firefox, "safari": _safari}[fmt]

    def gen():
        try:
            yield from reader(conn)
        finally:
            conn.close()

    return fmt, gen()


def _chromium(conn) -> Iterator[dict]:
    sql = """
    SELECT v.id, v.visit_time, v.transition, v.visit_duration, u.url, u.title
    FROM visits v JOIN urls u ON u.id = v.url
    WHERE v.visit_time > 0
    """
    for vid, t, transition, duration, url, title in conn.execute(sql):
        yield {
            "source_visit_id": vid,
            "raw_time": t,
            "ts": iso(CHROME_EPOCH + dt.timedelta(microseconds=t)),
            "url": url,
            "title": title or None,
            "transition": CHROME_TRANSITIONS.get((transition or 0) & 0xFF, "other"),
            "duration_s": (duration / 1e6) if duration else None,
        }


def _firefox(conn) -> Iterator[dict]:
    sql = """
    SELECT v.id, v.visit_date, v.visit_type, p.url, p.title
    FROM moz_historyvisits v JOIN moz_places p ON p.id = v.place_id
    WHERE v.visit_date > 0
    """
    for vid, t, vtype, url, title in conn.execute(sql):
        yield {
            "source_visit_id": vid,
            "raw_time": t,
            "ts": iso(UNIX_EPOCH + dt.timedelta(microseconds=t)),
            "url": url,
            "title": title or None,
            "transition": FIREFOX_TRANSITIONS.get(vtype, "other"),
            "duration_s": None,
        }


def _safari(conn) -> Iterator[dict]:
    sql = """
    SELECT v.id, v.visit_time, i.url, v.title, v.redirect_destination
    FROM history_visits v JOIN history_items i ON i.id = v.history_item
    """
    for vid, t, url, title, redirect_dest in conn.execute(sql):
        yield {
            "source_visit_id": vid,
            "raw_time": t,
            "ts": iso(COCOA_EPOCH + dt.timedelta(seconds=t)),
            "url": url,
            "title": title or None,
            "transition": "redirect" if redirect_dest else None,
            "duration_s": None,
        }


def visit_id(device, browser, profile, source_visit_id, raw_time) -> str:
    """A stable id, so overlapping snapshots of the same history dedupe."""
    key = "|".join(str(x) for x in (device, browser, profile, source_visit_id, raw_time))
    return hashlib.sha1(key.encode()).hexdigest()


class BrowserHistory(Source):
    name = "browser"
    timeline_table = "browser_visits"
    schema = SCHEMA

    def import_file(self, conn, path: Path, meta: Mapping, raw_file_id: int) -> int:
        fmt, visits = read_visits(path)
        browser = meta.get("browser") or ("safari" if fmt == "safari" else fmt)
        profile = meta.get("profile")
        device = meta.get("device")
        sql = """
        INSERT OR IGNORE INTO browser_visits
            (id, ts, url, title, domain, browser, profile, device, transition,
             duration_s, source_visit_id, raw_file_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        n = 0
        for v in visits:
            if v["transition"] in SKIP_TRANSITIONS:
                continue
            conn.execute(
                sql,
                (
                    visit_id(device, browser, profile, v["source_visit_id"], v["raw_time"]),
                    v["ts"], v["url"], v["title"], _domain(v["url"]), browser, profile,
                    device, v["transition"], v["duration_s"], v["source_visit_id"], raw_file_id,
                ),
            )
            n += 1
        return n

    def timeline_rows(self, conn, raw_file_id: int | None) -> Iterable[Mapping]:
        sql = """
        SELECT id AS source_key, ts, title, url, domain, browser, device
        FROM browser_visits
        WHERE (:raw IS NULL OR raw_file_id = :raw)
        """
        for r in conn.execute(sql, {"raw": raw_file_id}):
            yield {
                "source_key": r["source_key"],
                "ts": r["ts"],
                "title": r["title"] or r["url"],
                "body": " · ".join(x for x in (r["domain"], r["browser"], r["device"]) if x),
                "url": r["url"],
            }
