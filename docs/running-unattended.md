# Running unattended on the server

Everything below runs on the server (macbook-pro-2), in your own login
session. One LaunchAgent runs `wrh run --due` every 15 minutes; it runs
each job whose interval has passed, records every run in the database, and
once a day checks that every source is still working.

## 1. Settings

```sh
cp docs/config.example.toml ~/wrh-data/config.toml
```

Edit it. Each section you keep is a job; delete or comment out what you
don't use. Secrets go in the Keychain, never in the file:

```sh
uv run wrh secret set email/gmail          # the Google app password (below)
uv run wrh secret set calendar/personal    # the secret iCal address (below)
```

### Gmail (and other email)

wrh reads mail over IMAP, read-only, with an **app password**. No Google
Cloud project and no sign-in token that expires.

1. Turn on 2-Step Verification for the Google account, if it isn't on.
2. Go to <https://myaccount.google.com/apppasswords>, create one called
   "wrh", and store it with `wrh secret set email/gmail`.
3. IMAP is on by default for personal Gmail; if not: Gmail > Settings >
   Forwarding and POP/IMAP > Enable IMAP.

wrh reads "[Gmail]/All Mail", so each message is fetched once, with its
labels. Gmail's All Mail already leaves out Spam and Trash. The first sync
of a big mailbox is spread over several runs (`max_messages_per_run`, 5,000
by default, so roughly 240,000 a day).

**Filtering.** Before fetching anything, wrh reads the To/Cc/Bcc of your
Sent folder: anyone you have written to always counts as personal. Every
other message is sorted into personal, list, automated, bulk or spam (rules
in `src/wrh/sources/email/classify.py`, using List-Unsubscribe, Precedence,
mailing-service headers, no-reply senders and Gmail's Promotions, Updates,
Social and Forums tabs). By default bulk and spam are not kept at all;
`[email.keep]` in config.toml changes that per category. `category` and
`category_reason` in `email_messages` show why each message was filed
where it was. Changing the rules affects mail fetched from then on.

**Attachments** are taken out of each message and stored once each,
encrypted, under `raw/email-attachments/`; the archived message keeps a
stub pointing at the file. Inline images under 20 KB (logos) and files over
50 MB are not stored. `wrh attachment <sha256> [--out file]` decrypts one;
the sha256 is in the `attachments` column of `email_messages`.

For an old mailbox you no longer have IMAP access to, import a Google
Takeout mbox instead: `wrh import email "All mail Including Spam and Trash.mbox"`.

Other providers work the same way with their IMAP host and an app password.
Outlook.com and Microsoft 365 no longer accept app passwords over IMAP;
those need their own importer.

### Google Calendar

For each calendar: Google Calendar on the web > Settings > the calendar
under "Settings for my calendars" > Integrate calendar > **Secret address in
iCal format**. Add a `[[calendar.feeds]]` entry and store the address with
`wrh secret set calendar/<name>`. Anyone with that address can read the
calendar, which is why it lives in the Keychain. "Reset" on the same page
revokes it (the next sync will then fail and `wrh check` will tell you).

### Messages (iMessage and SMS)

1. On the iPhone: Settings > your name > iCloud > Messages > on. Also
   Settings > Messages > **Text Message Forwarding** > the server, so SMS
   reach the Mac too.
2. On the server, sign in to Messages with the same Apple ID and turn on
   Messages > Settings > iMessage > Enable Messages in iCloud. Wait for the
   history to download (it can take hours the first time).
3. Messages > Settings > General > Keep messages: **Forever**.

### Photos

1. On the server, sign in to Photos with the same Apple ID and turn on
   Photos > Settings > iCloud > iCloud Photos. "Optimize Mac Storage" is
   fine: wrh only reads metadata, which is always downloaded.
2. Leave Photos open for a while the first time, so it can download the
   library and finish recognising people and places.

## 2. Full Disk Access

Messages and Photos keep their databases in protected folders. Give Full
Disk Access to the Python interpreter that uv uses for wrh:

```sh
cd ~/repos/wrh && uv run python -c "import sys, os; print(os.path.realpath(sys.executable))"
```

System Settings > Privacy & Security > Full Disk Access > `+` > press
Cmd-Shift-G, paste that path, add it. Do this again if uv installs a new
Python. Without it, `wrh status` shows the messages and photos jobs failing
with "Cannot read" or "Operation not permitted".

## 3. Keep the Mac awake

```sh
sudo pmset -c sleep 0 disksleep 0     # never sleep on mains power
sudo pmset -c womp 1                  # wake for network access
```

With FileVault on, after a power cut or restart nothing runs until you log
in once (the Keychain holding the database key is locked until then).

## 4. Start the schedule

```sh
cp collectors/macos/com.wrh.run.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.wrh.ingest.plist 2>/dev/null   # replaced by com.wrh.run
launchctl load ~/Library/LaunchAgents/com.wrh.run.plist
```

The first run will ask, once per item, whether Python may use the Keychain
items "wrh". Choose **Always Allow**. Run `uv run wrh run` by hand once in
Terminal first to get these prompts out of the way.

## 5. Checking on it

| Command | What it shows |
| --- | --- |
| `wrh status` | Each job: how often it runs, last run, last success, STALE if overdue |
| `wrh check` | Only the problems; exits 1 if there are any |
| `wrh problems` | The latest warnings and errors, newest first (`--run N` for one run) |
| `wrh sync <source>` | Run one source now and print the result |
| `wrh run` | Run every job now, due or not |

The same information is in the database for Datasette: the `import_runs`
table (one row per run) and `import_problems` (one row per warning or error,
with tracebacks), plus the "Import health" query. A plain-text log is kept
in `~/wrh-data/logs/wrh.log`.

With an `[alerts]` section, the daily check emails you when a source fails
or goes stale, through your own Gmail account. It sends at most one email a
day unless the problems change.
