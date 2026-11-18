# Encryption at rest

## What is encrypted

| Thing | Protection |
| --- | --- |
| `wrh.db` (and its `-wal` file) | SQLCipher 4: AES-256 per page, HMAC-SHA512 per page. Without the key the file is random bytes. |
| `raw/` archive | AES-256-GCM in 1 MiB chunks, key derived from the database key with HKDF-SHA256. See "The raw archive" below. |
| `inbox/` | Plain snapshots, only until `wrh ingest` archives them and deletes them. FileVault and `0700` permissions. |
| Whole disk of the M1 | FileVault. Turn it on: System Settings > Privacy & Security > FileVault. |
| Snapshots in transit | rsync over SSH. |

FileVault protects a powered-off or stolen machine. SQLCipher additionally
protects the database from anything that can read your files while you are
logged in (backups, sync tools, malware that grabs `*.db` files), and keeps
it encrypted if it is ever copied to the cloud.

## How it works with SQLite tools

* `sqlcipher3` (package `sqlcipher3-wheels`) is a drop-in for Python's
  `sqlite3`, built with SQLCipher and FTS5. All wrh code that reads `wrh.db`
  opens it through `wrh.db.connect()`, which runs `PRAGMA key` first.
* **Datasette** works unchanged. Datasette prefers a module called `pysqlite3`
  when it exists. `wrh serve` registers SQLCipher under that name, with a
  `connect()` that keys every file connection, and then starts Datasette in
  the same process (`src/wrh/serve.py`). Table browsing, SQL, facets and the
  FTS search box all work; `tests/test_datasette.py` checks this.
* **FTS5** works inside SQLCipher, including the porter stemmer
  (`tests/test_encryption.py`).
* Standard `sqlite3`, DB Browser for SQLite, `sqlite-utils` etc. cannot open
  the file. Use `uv run wrh serve`, or the `sqlcipher` CLI
  (`brew install sqlcipher`) with `PRAGMA key = "x'<key>'";` as the first
  statement.

## The key

* A random 256-bit key, used as a SQLCipher *raw* key (`x'...'`), so there
  is no password to guess and no slow key derivation on every open.
* Stored in the **macOS login Keychain** on the server (service `wrh`,
  account `db-key`) via the `keyring` library. `wrh key init` creates it.
* `WRH_DB_KEY` (64 hex chars) overrides the Keychain. Tests use this. Don't
  put it in shell profiles or files in the repo.
* Collectors never need the key; they only copy plain browser files.

**Back it up.** `wrh key init` prints the key once. Put it in your password
manager. If the Keychain is lost (new Mac, reset), `wrh key import` restores it.
Without the key neither the database nor the raw archive can be recovered.

### Headless server notes

* The login Keychain is unlocked only while you are logged in. Run jobs as
  **LaunchAgents** (your session), not LaunchDaemons. With FileVault on, the
  M1 needs your password after a reboot before anything runs.
* The first time a given Python reads the key, macOS asks "python wants to
  use your confidential information stored in 'wrh'". Choose **Always
  Allow**. Recreating the virtualenv or upgrading Python can ask again.
* `wrh serve` listens on 127.0.0.1 only. Datasette has no login by default,
  so do not bind it to `0.0.0.0` on your LAN. To use it from other devices,
  put it behind Tailscale and/or add `datasette-auth-passwords` first.

## The raw archive

Raw files are as sensitive as the database (they are your browsers' own
history files), so they are encrypted too, with the same backed-up key:

* Each file becomes `raw/<source>/<yyyy>/<mm>/<sha256>__<name>.enc`
  (deduplicated by the hash of the plaintext).
* AES-256-GCM, 1 MiB chunks, a random per-file nonce, and the chunk number
  and a last-chunk flag in each chunk's nonce, so a reordered, damaged or
  truncated file fails to decrypt rather than importing partial data.
  The format is described at the top of `src/wrh/raw.py`.
* The AES key is derived from the database key with HKDF-SHA256, so the one
  key in your password manager unlocks both.
* Imports always read the archived copy: it is decrypted to a temporary
  file under `~/wrh-data/tmp/` (mode `0700`), parsed, and deleted.

## Changing or moving the key

Not wrapped in a command yet. It needs `PRAGMA rekey = "x'<new>'"` on the
database *and* re-encrypting every file in `raw/`, then storing the new key
with `wrh key import`.

## Moving to Postgres or the cloud later

Nothing in the core depends on SQLCipher beyond `wrh/db.py` and
`wrh/serve.py`. A move to Postgres would mean encrypted storage on the host
(e.g. an encrypted volume or a managed database) rather than per-file
encryption.
