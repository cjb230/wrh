# Architecture

## Layout

```
src/wrh/
  cli.py            the `wrh` command
  config.py         where the data directory is (WRH_HOME, default ~/wrh-data)
  keys.py           the database key: Keychain or WRH_DB_KEY
  db.py             SQLCipher connection + core tables (raw_files, timeline, timeline_fts)
  raw.py            content-addressed, encrypted raw archive
  pipeline.py       archive -> import -> timeline; ingest; reimport; search
  serve.py          Datasette on the encrypted db
  timeutil.py       the one timestamp format
  datasette/metadata.yml   titles, default sorts, facets, canned queries
  sources/
    __init__.py     the registry
    base.py         the Source contract
    browser/        importer.py (server side) + collector.py (everyday machines)
collectors/macos/   scheduling glue (launchd, rsync)
tests/
```

## Data flow

1. A **collector** on an everyday machine copies the source's native data
   (no parsing, no key) into an outbox with a JSON sidecar describing it
   (device, browser, profile...).
2. The outbox is copied to the server's `inbox/<source>/`.
3. `wrh ingest` encrypts each file into `raw/<source>/yyyy/mm/` (deduplicated
   by SHA-256), then calls the source's `import_file`, which writes rows to
   that source's own tables.
4. The source's `timeline_rows` maps its rows onto the shared core fields,
   which are upserted into `timeline`; triggers keep `timeline_fts` in sync.

## Core fields

Every timeline row has:

| Column | Meaning |
| --- | --- |
| `ts` | When it happened, UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ` |
| `source` | Which source, e.g. `browser` |
| `title` | Short human text |
| `body` | Longer text to search (message body, note, context) |
| `url` | Link or reference back to the original |
| `source_table`, `source_key` | The full row in the source's own table |

## Design choices

* **Timeline is derived.** Sources own their tables; the timeline can be
  rebuilt from them, and they can be rebuilt from `raw/`.
* **Idempotent imports.** Rows have stable ids (browser visits hash
  device, browser, profile, visit id and visit time), so overlapping
  snapshots never duplicate.
* **Plain SQL, no ORM.** Easy to port to Postgres later if needed.
* **Schema changes** are `CREATE TABLE IF NOT EXISTS` for now. When a source's
  table needs to change, drop it and `wrh reimport <source>`. Add real
  migrations when that gets painful.
