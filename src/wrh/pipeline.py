"""Archive -> import -> timeline. Shared by every source."""

from __future__ import annotations

import json
from pathlib import Path

from . import db, raw
from .config import Paths
from .sources import SOURCES, get_source


def open_db(paths: Paths, key: str | None = None, *, create: bool = False):
    if create:
        paths.ensure()
    conn = db.connect(paths.db, key, create=create)
    db.init_core(conn)
    for s in SOURCES.values():
        s.init(conn)
    conn.commit()
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


def ingest_inbox(conn, paths: Paths) -> list[dict]:
    """Import every collector snapshot waiting in inbox/<source>/.

    A snapshot is picked up only when its .json sidecar exists. Once it is
    safely archived, both files are removed from the inbox.
    """
    results = []
    for source_dir in sorted(p for p in paths.inbox.iterdir() if p.is_dir()):
        if source_dir.name not in SOURCES:
            continue
        for sidecar in sorted(source_dir.glob("*.json")):
            data_files = [p for p in source_dir.glob(sidecar.stem + ".*") if p != sidecar]
            if len(data_files) != 1:
                continue
            meta = json.loads(sidecar.read_text())
            results.append(import_file(conn, paths, source_dir.name, data_files[0], meta))
            data_files[0].unlink()
            sidecar.unlink()
    return results


def reimport(conn, paths: Paths, source_name: str) -> dict:
    """Rebuild a source's tables and timeline rows from the raw archive."""
    source = get_source(source_name)
    rows = conn.execute(
        "SELECT id, path, meta FROM raw_files WHERE source = ? ORDER BY id", (source.name,)
    ).fetchall()
    with conn:
        db.delete_timeline(conn, source.name)
        source.reset(conn)
        total = 0
        for r in rows:
            with raw.plaintext(paths, r["path"]) as plain:
                total += source.import_file(conn, plain, json.loads(r["meta"]), r["id"])
            raw.mark_imported(conn, r["id"])
        t = db.upsert_timeline(conn, source.name, source.timeline_table, source.timeline_rows(conn, None))
    return {"raw_files": len(rows), "rows_read": total, "timeline_rows": t}


def search(conn, query: str, limit: int = 20, source: str | None = None):
    sql = """
    SELECT t.ts, t.source, t.title, t.url
    FROM timeline_fts f JOIN timeline t ON t.id = f.rowid
    WHERE timeline_fts MATCH :q AND (:source IS NULL OR t.source = :source)
    ORDER BY f.rank LIMIT :limit
    """
    return conn.execute(sql, {"q": query, "source": source, "limit": limit}).fetchall()
