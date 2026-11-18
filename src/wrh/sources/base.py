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

    def init(self, conn) -> None:
        conn.executescript(self.schema)

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
