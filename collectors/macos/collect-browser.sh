#!/bin/sh
# Run on an everyday Mac: snapshot browser history and push it to the server.
# Edit SERVER and WRH_REPO, then schedule it with the launchd plist next to this file.
set -eu
SERVER="${WRH_SERVER:-james@m1-server.local}"     # ssh destination of the M1
REMOTE_INBOX="${WRH_REMOTE_INBOX:-wrh-data/inbox/browser/}"
WRH_REPO="${WRH_REPO:-$HOME/repos/wrh}"
OUTBOX="${WRH_OUTBOX:-$HOME/.wrh-outbox/browser}"

cd "$WRH_REPO"
uv run wrh collect browser --out "$OUTBOX"
# Copy snapshots (data first, sidecars last, so the server never sees half a pair),
# then delete the local copies that were sent.
rsync -a --remove-source-files --include='*.sqlite' --exclude='*' "$OUTBOX/" "$SERVER:$REMOTE_INBOX"
rsync -a --remove-source-files --include='*.json' --exclude='*' "$OUTBOX/" "$SERVER:$REMOTE_INBOX"
