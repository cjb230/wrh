# wrh

A personal data warehouse in the spirit of [Dogsheep](https://dogsheep.github.io/):
pull your digital history into one SQLite database, encrypted at rest, and
search and browse it with [Datasette](https://datasette.io/).

What it is for:

1. **Remembering.** A searchable record of what you read, wrote and did.
2. **Provenance.** When and where you first came across an idea or a person.
3. **Self-research.** Keeping the data so you can analyse it later.

Status: scaffold plus two sources (browser history, Claude and ChatGPT chats).

## How it fits together

```
everyday machines (macOS, Windows)              M1 server (always on)
-----------------------------------             -------------------------------------------
wrh collect browser  --> outbox/  --rsync-->    ~/wrh-data/inbox/browser/
                                                   | wrh ingest
                                                   v
                                                ~/wrh-data/raw/       encrypted copies, forever
                                                ~/wrh-data/wrh.db     SQLCipher-encrypted
                                                   browser_visits ... (one set of tables per source)
                                                   timeline + timeline_fts (shared, searchable)
                                                   | wrh serve
                                                   v
                                                Datasette on http://127.0.0.1:8001
```

* One repo holds everything: the shared core (`src/wrh/`), each source's
  importer and collector (`src/wrh/sources/<name>/`), the scheduling glue
  (`collectors/`), and the server (`wrh serve`, which runs Datasette).
* Each source writes only its own tables. It also maps its rows onto the
  shared **timeline** (ts, source, title, body, url back to the original),
  which has a full-text index across every source.
* Raw files are archived byte-for-byte (encrypted with the same key), so `wrh reimport <source>` can rebuild
  everything after a schema change or a bug fix.

More: [docs/architecture.md](docs/architecture.md),
[docs/encryption.md](docs/encryption.md),
[docs/adding-a-source.md](docs/adding-a-source.md).

## Setup on the M1 server

Needs [uv](https://docs.astral.sh/uv/). Turn on FileVault too (see
[docs/encryption.md](docs/encryption.md)).

```sh
cd ~/repos/wrh
uv sync
uv run wrh key init        # makes a key, stores it in the Keychain, prints a copy
                           # -> save that copy in your password manager NOW
uv run wrh init            # creates ~/wrh-data/wrh.db (encrypted)
uv run wrh key check
```

## Browser history

On any machine (this one included):

```sh
uv run wrh collect browser --out ~/.wrh-outbox/browser
```

This snapshots history for Chrome, Edge, Brave, Arc, Firefox and Safari,
whichever are installed, across all profiles. Safari needs Full Disk Access
for the app running the command (Terminal, iTerm, or `uv`'s Python for
launchd jobs): System Settings > Privacy & Security > Full Disk Access.
iPhone Safari history reaches the Mac's Safari database if Safari syncing
is on in iCloud settings.

Move the snapshots into the server's `~/wrh-data/inbox/browser/` (on the
server itself, `--out ~/wrh-data/inbox/browser` does it directly), then:

```sh
uv run wrh ingest
uv run wrh search 'dogsheep'
uv run wrh serve           # http://127.0.0.1:8001
```

To automate: `collectors/macos/collect-browser.sh` plus its launchd plist on
each Mac (hourly snapshot and rsync over SSH), and
`collectors/macos/com.wrh.ingest.plist` on the server. On Windows, schedule
`uv run wrh collect browser --out ...` with Task Scheduler and copy the
folder with `scp` or a synced folder.

You can also import a single file by hand:

```sh
uv run wrh import browser ~/Downloads/History --meta browser=chrome --meta device=old-laptop
```

## Claude and ChatGPT chats

Both services give you a zip of your whole chat history by email:

* **Claude:** claude.ai > Settings > Privacy > Export data. The download
  link arrives by email.
* **ChatGPT:** chatgpt.com > Settings > Data controls > Export data >
  Confirm export. The download link arrives by email and expires after 24 hours.

Then, on the server:

```sh
uv run wrh import llm ~/Downloads/data-2026-11-18-*.zip   # Claude
uv run wrh import llm ~/Downloads/<chatgpt-export>.zip    # ChatGPT
uv run wrh search 'sourdough' --source llm
```

The zip is archived encrypted in `raw/llm/` and the format is detected
automatically. You get `llm_conversations` and `llm_messages` (time, role,
model where ChatGPT records it, attachment text for Claude, and every
ChatGPT edit/regeneration branch, with `on_current_branch` marking the one
you kept). Your messages and the assistant's replies go on the timeline,
linking back to the conversation on claude.ai or chatgpt.com.

Request a fresh export whenever you like and import it the same way: each
export contains everything, so conversations are updated in place and only
new messages are added. Conversations you delete on the service stay here.
Delete the zip from Downloads after importing; the encrypted copy is the one to keep.

## Commands

| Command | What it does |
| --- | --- |
| `wrh key init / import / check` | Create, restore, or test the database key |
| `wrh init` | Create the data directory and encrypted database |
| `wrh collect browser --out DIR` | Snapshot local browser history (no key needed) |
| `wrh ingest` | Archive and import everything in `inbox/` |
| `wrh import SOURCE FILE...` | Archive and import specific files |
| `wrh reimport SOURCE` | Rebuild a source from the raw archive |
| `wrh search QUERY` | Full-text search of the timeline |
| `wrh serve` | Datasette on the encrypted database |

The data directory is `~/wrh-data`, or set `WRH_HOME`.

## Development

```sh
uv sync
uv run pytest
```

Tests use a throwaway key from `WRH_DB_KEY` and never touch the Keychain.
