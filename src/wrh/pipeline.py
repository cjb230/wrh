"""Archive -> import -> timeline. Shared by every source."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import db, raw, runlog
from . import settings as settings_mod
from .config import Paths
from .sources import SOURCES, get_source


def open_db(paths: Paths, key: str | None = None, *, create: bool = False):
    if create:
        paths.ensure()
    conn = db.connect(paths.db, key, create=create)
    db.init_core(conn)
    runlog.init(conn)
    for s in SOURCES.values():
        s.init(conn)
    conn.commit()
    runlog.setup_file_logging(paths)
    return conn


def import_file(conn, paths: Paths, source_name: str, file: Path, meta: dict) -> dict:
    """Archive one raw file, import it, and add its rows to the timeline."""
    source = get_source(source_name)
    with conn:
        raw_id, is_new = raw.archive(conn, paths, source.name, file, meta)
    if not is_new:
        return {"file": str(file), "skipped": "already archived", "raw_file_id": raw_id}
    rel = conn.execute("SELECT path FROM raw_files WHERE id = ?", (raw_id,)).fetchone()[0]
    # Import from the archived copy (decrypted to a temp file), so a fresh
    # import and a later reimport read exactly the same bytes.
    with conn, raw.plaintext(paths, rel) as archived:
        n = source.import_file(conn, archived, meta, raw_id)
        t = db.upsert_timeline(conn, source.name, source.timeline_table, source.timeline_rows(conn, raw_id))
        raw.mark_imported(conn, raw_id)
    return {"file": str(file), "raw_file_id": raw_id, "rows_read": n, "timeline_rows": t}


def import_files(conn, paths: Paths, source_name: str, files, meta: dict, trigger="manual") -> list[dict]:
    """`wrh import`: several files, logged as one run. One bad file does not stop the rest."""
    results = []
    with runlog.run(conn, source_name, "import", trigger) as r:
        for f in files:
            try:
                res = import_file(conn, paths, source_name, Path(f), meta)
            except Exception as e:
                r.exception(f"Import failed: {e}", item=str(f))
                res = {"file": str(f), "error": str(e)}
            r.add(res)
            results.append(res)
    return results


def ingest_inbox(conn, paths: Paths, trigger="manual") -> list[dict]:
    """Import every collector snapshot waiting in inbox/<source>/.

    A snapshot is picked up only when its .json sidecar exists. Once it is
    safely archived, both files are removed from the inbox. A snapshot that
    fails to import is moved to inbox/<source>/failed/ and logged, so it is
    not retried every 15 minutes; move it back to retry after a fix.
    """
    results = []
    if not paths.inbox.exists():
        return results
    for source_dir in sorted(p for p in paths.inbox.iterdir() if p.is_dir()):
        if source_dir.name not in SOURCES:
            continue
        sidecars = sorted(source_dir.glob("*.json"))
        if not sidecars:
            continue
        with runlog.run(conn, source_dir.name, "ingest", trigger) as r:
            for sidecar in sidecars:
                data_files = [p for p in source_dir.glob(sidecar.stem + ".*") if p != sidecar]
                if len(data_files) != 1:
                    r.warn(f"Expected one data file next to the sidecar, found {len(data_files)}",
                           item=sidecar.name)
                    continue
                try:
                    meta = json.loads(sidecar.read_text())
                    res = import_file(conn, paths, source_dir.name, data_files[0], meta)
                except Exception as e:
                    r.exception(f"Import failed, moved to failed/: {e}", item=data_files[0].name)
                    failed = source_dir / "failed"
                    failed.mkdir(exist_ok=True)
                    for p in (data_files[0], sidecar):
                        shutil.move(str(p), failed / p.name)
                    results.append({"file": str(data_files[0]), "error": str(e)})
                    continue
                r.add(res)
                results.append(res)
                data_files[0].unlink()
                sidecar.unlink()
    return results


def reimport(conn, paths: Paths, source_name: str) -> dict:
    """Rebuild a source's tables and timeline rows from the raw archive."""
    source = get_source(source_name)
    with runlog.run(conn, source.name, "reimport") as r:
        rows = conn.execute(
            "SELECT id, path, meta FROM raw_files WHERE source = ? ORDER BY id", (source.name,)
        ).fetchall()
        with conn:
            db.delete_timeline(conn, source.name)
            source.reset(conn)
            total = 0
            for row in rows:
                with raw.plaintext(paths, row["path"]) as plain:
                    total += source.import_file(conn, plain, json.loads(row["meta"]), row["id"])
                raw.mark_imported(conn, row["id"])
            t = db.upsert_timeline(conn, source.name, source.timeline_table, source.timeline_rows(conn, None))
        r.files, r.rows_read, r.timeline_rows = len(rows), total, t
    return {"raw_files": len(rows), "rows_read": total, "timeline_rows": t}


# --- sources that fetch their own data ---------------------------------------

@dataclass
class SyncContext:
    conn: object
    paths: Paths
    source: object
    settings: dict
    run: runlog.Run

    def get_state(self, key: str, default=None):
        return runlog.get_state(self.conn, self.source.name, key, default)

    def set_state(self, key: str, value) -> None:
        with self.conn:
            runlog.set_state(self.conn, self.source.name, key, value)

    def save(self, name: str, data: bytes, meta: dict | None = None) -> dict:
        """Archive ``data`` as a raw file called ``name`` and import it."""
        tmp_root = self.paths.home / "tmp"
        tmp_root.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as d:
            f = Path(d) / name
            f.write_bytes(data)
            res = import_file(self.conn, self.paths, self.source.name, f, meta or {})
        self.run.add(res)
        return res


def sync_source(conn, paths: Paths, source_name: str, trigger="manual", settings: dict | None = None):
    """Run one source's sync, logged as a run. Returns the Run."""
    source = get_source(source_name)
    if not source.can_sync:
        raise ValueError(f"Source {source.name!r} does not fetch its own data; use `wrh import`.")
    if settings is None:
        settings = settings_mod.load(paths)
    sect = settings_mod.section(settings, source.name)
    if sect is None:
        raise ValueError(f"No [{source.name}] section in {settings_mod.path(paths)}.")
    with runlog.run(conn, source.name, "sync", trigger) as r:
        source.sync(SyncContext(conn, paths, source, sect, r))
    return r


def search(conn, query: str, limit: int = 20, source: str | None = None):
    sql = """
    SELECT t.ts, t.source, t.title, t.url
    FROM timeline_fts f JOIN timeline t ON t.id = f.rowid
    WHERE timeline_fts MATCH :q AND (:source IS NULL OR t.source = :source)
    ORDER BY f.rank LIMIT :limit
    """
    return conn.execute(sql, {"q": query, "source": source, "limit": limit}).fetchall()
