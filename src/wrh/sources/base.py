"""The contract every source implements. See docs/adding-a-source.md."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping


class Source:
    #: Short, stable name. Used in the timeline, raw/<name>/ and inbox/<name>/.
    name: str = ""
    #: The table whose rows feed the timeline (source_key is its primary key).
    timeline_table: str = ""
    #: CREATE TABLE IF NOT EXISTS ... for this source's own tables. Prefix
    #: table names with the source name so sources never collide.
    schema: str = ""

    #: True if the source can fetch its own data (``wrh sync <name>``, and the
    #: scheduler). Such sources implement ``sync(ctx)``.
    can_sync: bool = False

    def init(self, conn) -> None:
        conn.executescript(self.schema)

    def reset(self, conn) -> None:
        """Empty this source's tables before `wrh reimport`. Override if the
        source has more tables than ``timeline_table``."""
        conn.execute(f"DELETE FROM {self.timeline_table}")

    def import_file(self, conn, path: Path, meta: Mapping, raw_file_id: int) -> int:
        """Parse one raw file and write rows to this source's own tables.

        Must be idempotent: importing the same file twice changes nothing.
        Returns the number of rows read.
        """
        raise NotImplementedError

    def timeline_rows(self, conn, raw_file_id: int | None) -> Iterable[Mapping]:
        """Yield the core fields for the timeline.

        Each row has: ts, source_key, title, body, url. ``raw_file_id`` limits
        it to rows that came from one file; None means every row.
        """
        raise NotImplementedError

    def sync(self, ctx) -> None:
        """Fetch new data and hand it to ``ctx.save(...)``, which archives it
        and runs ``import_file`` on it, exactly like a collector snapshot.

        ``ctx`` is a ``pipeline.SyncContext``: ``ctx.conn``, ``ctx.settings``
        (this source's section of config.toml), ``ctx.run`` (the run log:
        ``ctx.run.warn(...)``), ``ctx.get_state(key)`` / ``ctx.set_state(key, value)``
        for cursors. Save a cursor only after the data it covers is saved.
        """
        raise NotImplementedError
