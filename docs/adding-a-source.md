# Adding a source

A source is a folder under `src/wrh/sources/` with an importer (runs on the
server, writes to the encrypted db) and, if the data lives on an everyday
machine, a collector (runs there, just copies files).

## 1. Create the package

```
src/wrh/sources/<name>/
  __init__.py        from .importer import MySource
  importer.py
  collector.py       optional
```

## 2. Write the importer

```python
from wrh.sources.base import Source
from wrh.timeutil import to_iso

class Notes(Source):
    name = "notes"                     # used in timeline, raw/notes/, inbox/notes/
    timeline_table = "notes_notes"
    schema = """
    CREATE TABLE IF NOT EXISTS notes_notes (
        id TEXT PRIMARY KEY,           -- stable across re-imports
        ts TEXT NOT NULL,              -- to_iso(datetime with tz)
        title TEXT,
        body TEXT,
        path TEXT,
        raw_file_id INTEGER REFERENCES raw_files(id)
    );
    """

    def import_file(self, conn, path, meta, raw_file_id):
        n = 0
        for note in parse(path):           # your parsing here
            conn.execute(
                "INSERT OR IGNORE INTO notes_notes VALUES (?, ?, ?, ?, ?, ?)",
                (note.id, to_iso(note.created), note.title, note.text, note.path, raw_file_id),
            )
            n += 1
        return n

    def timeline_rows(self, conn, raw_file_id):
        for r in conn.execute(
            "SELECT id, ts, title, body, path FROM notes_notes"
            " WHERE (:raw IS NULL OR raw_file_id = :raw)", {"raw": raw_file_id}):
            yield {"source_key": r["id"], "ts": r["ts"], "title": r["title"],
                   "body": r["body"], "url": f"file://{r['path']}"}
```

Rules:

* **Only write your own tables**, prefixed with the source name.
* **Be idempotent.** Use a stable primary key and `INSERT OR IGNORE` (or an
  upsert). Importing the same file twice must change nothing.
* **Store times with `to_iso`** so they sort with every other source.
* **Keep a way back to the original** in `url`: a web URL, `message://` link,
  file path, or anything else that lets you find the real thing.
* Don't modify the raw file; it is the archived copy.

## 3. Register it

In `src/wrh/sources/__init__.py`, add it to `SOURCES`. Add a section for its
tables in `src/wrh/datasette/metadata.yml` (default sort, facets).

## 4. Collector (optional)

Copy native files into an outbox, each with a `<same-name>.json` sidecar of
metadata (written last). Use only the standard library, and never the
database key. See `sources/browser/collector.py`, then add a `wrh collect
<name>` command in `cli.py`.

## 5. Test it

Build a tiny fake input in `tests/`, import it through `pipeline.import_file`,
and check both your table and `pipeline.search`. See `tests/test_browser.py`.
