"""Run Datasette against the encrypted database.

Datasette uses ``pysqlite3`` instead of the standard library's ``sqlite3``
when it can import it. We put a small stand-in module under that name into
``sys.modules`` *before* Datasette is imported. It is SQLCipher, with a
``connect()`` that sends ``PRAGMA key`` as the first statement on every
connection to a file. Everything else in Datasette (FTS search, SQL queries,
facets) then works unchanged. The key stays in this process's memory and is
never written to disk or passed on the command line.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import sqlcipher3.dbapi2 as _sqlcipher

from . import keys

DATASETTE_DIR = Path(__file__).resolve().parent / "datasette"


def _is_memory(database) -> bool:
    s = str(database)
    return s == ":memory:" or "mode=memory" in s


def install_keyed_sqlite(key: str) -> types.ModuleType:
    if "datasette" in sys.modules:
        raise RuntimeError("install_keyed_sqlite() must run before datasette is imported")
    stmt = keys.pragma(key)
    mod = types.ModuleType("pysqlite3")
    for name in dir(_sqlcipher):
        if not name.startswith("__"):
            setattr(mod, name, getattr(_sqlcipher, name))

    def connect(database, *args, **kwargs):
        conn = _sqlcipher.connect(database, *args, **kwargs)
        if not _is_memory(database):
            conn.execute(stmt)
        return conn

    mod.connect = connect
    mod.dbapi2 = mod
    sys.modules["pysqlite3"] = mod
    sys.modules["pysqlite3.dbapi2"] = mod
    return mod


def run(db_path: Path, key: str, host: str, port: int, extra: list[str]) -> None:
    install_keyed_sqlite(key)
    from datasette.cli import cli as datasette_cli

    args = [
        "serve", str(db_path),
        "--metadata", str(DATASETTE_DIR / "metadata.yml"),
        "--host", host, "--port", str(port),
        *extra,
    ]
    datasette_cli.main(args=args, prog_name="wrh serve")
