"""The run log: what every importer did, and what went wrong.

Every import, ingest and sync is recorded in two helper tables:

* ``import_runs``     - one row per run: source, job, when it started and
  finished, status (running, ok, warning, error), counts, and the error.
* ``import_problems`` - one row per problem found during a run: a message
  that would not parse, a file that failed, a traceback. ``level`` is
  ``warning`` (the run carried on) or ``error`` (the run, or one file, failed).

Problems are kept in memory during the run and written when it finishes,
after the run's own data has been committed or rolled back, so a failed
import never takes its error report down with it. A run killed half-way
(power cut, ``kill -9``) is left as ``running`` and is marked ``interrupted``
by the next run of the same source.

Each run is also written to ``logs/wrh.log`` in the data directory, for
reading over ssh without opening the database.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import socket
import traceback
from dataclasses import dataclass, field

from . import __version__
from .config import Paths
from .timeutil import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,          -- e.g. 'email', or '*' for a job across sources
    job TEXT NOT NULL,             -- sync, import, ingest, reimport, check
    trigger TEXT,                  -- schedule or manual
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,          -- running, ok, warning, error, interrupted
    files INTEGER,                 -- raw files archived by this run
    rows_read INTEGER,             -- rows the importer read
    timeline_rows INTEGER,         -- timeline rows written or updated
    problems INTEGER,              -- rows in import_problems for this run
    message TEXT,                  -- one-line summary
    error TEXT,                    -- the exception that ended the run, if any
    host TEXT,
    pid INTEGER,
    version TEXT
);
CREATE INDEX IF NOT EXISTS import_runs_source ON import_runs(source, started_at);

CREATE TABLE IF NOT EXISTS import_problems (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES import_runs(id),
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    level TEXT NOT NULL,           -- warning or error
    item TEXT,                     -- which file, message or record
    message TEXT NOT NULL,
    detail TEXT                    -- traceback or extra context
);
CREATE INDEX IF NOT EXISTS import_problems_run ON import_problems(run_id);
CREATE INDEX IF NOT EXISTS import_problems_ts ON import_problems(ts);

-- Cursors for sources that pull data (last IMAP UID, last message ROWID, ...).
CREATE TABLE IF NOT EXISTS sync_state (
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, key)
);
"""

# At most this many problems are stored per run; the rest are only counted.
MAX_PROBLEMS = 500

_log = logging.getLogger("wrh")


def setup_file_logging(paths: Paths) -> None:
    """Log to <data>/logs/wrh.log (rotated at 5 MB, 5 files kept)."""
    if any(getattr(h, "_wrh", False) for h in _log.handlers):
        return
    d = paths.home / "logs"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(d / "wrh.log", maxBytes=5 << 20, backupCount=5)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    h._wrh = True
    _log.addHandler(h)
    _log.setLevel(logging.INFO)


def init(conn) -> None:
    conn.executescript(SCHEMA)


@dataclass
class Run:
    conn: object
    source: str
    job: str
    trigger: str = "manual"
    id: int = 0
    files: int = 0
    rows_read: int = 0
    timeline_rows: int = 0
    message: str | None = None
    _problems: list = field(default_factory=list)
    _problem_count: int = 0
    _errors: int = 0

    def warn(self, message: str, item: str | None = None, detail: str | None = None) -> None:
        self._add("warning", message, item, detail)

    def error(self, message: str, item: str | None = None, detail: str | None = None) -> None:
        """A part of the run failed (one file, one account), but the run went on."""
        self._errors += 1
        self._add("error", message, item, detail)

    def exception(self, message: str, item: str | None = None) -> None:
        self.error(message, item, traceback.format_exc())

    def _add(self, level, message, item, detail):
        self._problem_count += 1
        (_log.warning if level == "warning" else _log.error)(
            "%s %s: %s%s", self.source, self.job, message, f" [{item}]" if item else "")
        if len(self._problems) < MAX_PROBLEMS:
            self._problems.append((now_iso(), level, item, message, detail))

    def add(self, result: dict) -> None:
        """Add the counts from one pipeline.import_file() result."""
        if result.get("skipped"):
            return
        self.files += 1
        self.rows_read += result.get("rows_read") or 0
        self.timeline_rows += result.get("timeline_rows") or 0

    @property
    def status(self) -> str:
        if self._errors:
            return "error"
        return "warning" if self._problem_count else "ok"


def _mark_interrupted(conn, source: str) -> None:
    host = socket.gethostname()
    for r in conn.execute(
        "SELECT id, pid, host FROM import_runs WHERE source = ? AND status = 'running'", (source,)
    ).fetchall():
        if r["host"] == host and r["pid"] and _pid_alive(r["pid"]) and r["pid"] != os.getpid():
            continue
        conn.execute(
            "UPDATE import_runs SET status = 'interrupted', finished_at = ?,"
            " error = 'The process stopped before the run finished' WHERE id = ?",
            (now_iso(), r["id"]),
        )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class run:
    """Context manager that records one run.

        with runlog.run(conn, "email", "sync") as r:
            ...            # r.warn(...), r.add(result), r.message = "..."

    An exception inside the block rolls back the open transaction, is stored
    as the run's error, and is then re-raised unless ``swallow=True``.
    """

    def __init__(self, conn, source: str, job: str, trigger: str = "manual", swallow: bool = False):
        self.r = Run(conn, source, job, trigger)
        self.swallow = swallow

    def __enter__(self) -> Run:
        c = self.r.conn
        _mark_interrupted(c, self.r.source)
        cur = c.execute(
            "INSERT INTO import_runs (source, job, trigger, started_at, status, host, pid, version)"
            " VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
            (self.r.source, self.r.job, self.r.trigger, now_iso(), socket.gethostname(),
             os.getpid(), __version__),
        )
        self.r.id = cur.lastrowid
        c.commit()
        _log.info("%s %s: started (run %s)", self.r.source, self.r.job, self.r.id)
        return self.r

    def __exit__(self, exc_type, exc, tb):
        r, c = self.r, self.r.conn
        error = None
        if exc is not None:
            try:
                c.rollback()
            except Exception:
                pass
            error = f"{exc_type.__name__}: {exc}"
            r._errors += 1
            r._add("error", error, None, "".join(traceback.format_exception(exc_type, exc, tb)))
        status = r.status
        if error is None and status == "error":
            # The run finished, but part of it failed: show the first failure.
            error = next((msg for _, level, _, msg, _ in r._problems if level == "error"), None)
        c.executemany(
            "INSERT INTO import_problems (run_id, ts, source, level, item, message, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(r.id, ts, r.source, level, item, msg, detail) for ts, level, item, msg, detail in r._problems],
        )
        c.execute(
            "UPDATE import_runs SET finished_at = ?, status = ?, files = ?, rows_read = ?,"
            " timeline_rows = ?, problems = ?, message = ?, error = ? WHERE id = ?",
            (now_iso(), status, r.files, r.rows_read, r.timeline_rows, r._problem_count,
             r.message, error, r.id),
        )
        c.commit()
        _log.info("%s %s: %s (run %s, %s files, %s rows, %s timeline, %s problems)%s",
                  r.source, r.job, status, r.id, r.files, r.rows_read, r.timeline_rows,
                  r._problem_count, f" {r.message}" if r.message else "")
        return self.swallow and exc is not None


# --- sync state ---------------------------------------------------------------

def get_state(conn, source: str, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE source = ? AND key = ?", (source, key)).fetchone()
    return row[0] if row else default


def set_state(conn, source: str, key: str, value) -> None:
    conn.execute(
        "INSERT INTO sync_state (source, key, value, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (source, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (source, key, None if value is None else str(value), now_iso()),
    )
