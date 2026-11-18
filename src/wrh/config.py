"""Where wrh keeps its data.

Everything lives under one data directory, outside the git repo:

    $WRH_HOME/            (default ~/wrh-data)
        wrh.db            the SQLCipher-encrypted database
        raw/<source>/...  unchanged copies of every file that was imported
        inbox/<source>/   where collectors drop new files for `wrh ingest`
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def db(self) -> Path:
        return self.home / "wrh.db"

    @property
    def raw(self) -> Path:
        return self.home / "raw"

    @property
    def inbox(self) -> Path:
        return self.home / "inbox"

    def ensure(self) -> "Paths":
        for p in (self.home, self.raw, self.inbox):
            p.mkdir(parents=True, exist_ok=True)
            try:
                p.chmod(0o700)
            except OSError:
                pass
        return self


def get_paths(home: str | os.PathLike | None = None) -> Paths:
    if home is None:
        home = os.environ.get("WRH_HOME") or Path.home() / "wrh-data"
    return Paths(Path(home).expanduser().resolve())
