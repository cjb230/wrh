"""Browser history collector. Runs on your everyday machines (macOS, Windows).

It finds each browser profile's history database, takes a consistent
snapshot of it (browsers keep these files open, and recent changes may still
be in a -wal file), and writes the snapshot plus a small JSON sidecar into an
outbox folder. Copy the outbox to the server's inbox/browser/ and run
`wrh ingest` there.

Uses only the standard library: no key, no SQLCipher, nothing to unlock.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
import shutil
import socket
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HistoryFile:
    browser: str
    profile: str
    path: Path


def _chromium_profiles(browser: str, user_data: Path) -> list[HistoryFile]:
    found = []
    if not user_data.is_dir():
        return found
    for history in sorted(user_data.glob("*/History")):
        found.append(HistoryFile(browser, history.parent.name, history))
    return found


def find_history_files(home: Path | None = None) -> list[HistoryFile]:
    home = home or Path.home()
    found: list[HistoryFile] = []
    if sys.platform == "darwin":
        sup = home / "Library" / "Application Support"
        for browser, rel in [
            ("chrome", "Google/Chrome"),
            ("edge", "Microsoft Edge"),
            ("brave", "BraveSoftware/Brave-Browser"),
            ("arc", "Arc/User Data"),
        ]:
            found += _chromium_profiles(browser, sup / rel)
        safari = home / "Library" / "Safari" / "History.db"
        if safari.exists():
            found.append(HistoryFile("safari", "default", safari))
        ff_root = sup / "Firefox" / "Profiles"
    elif sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        for browser, rel in [
            ("chrome", "Google/Chrome/User Data"),
            ("edge", "Microsoft/Edge/User Data"),
            ("brave", "BraveSoftware/Brave-Browser/User Data"),
        ]:
            found += _chromium_profiles(browser, local / rel)
        ff_root = roaming / "Mozilla" / "Firefox" / "Profiles"
    else:
        for browser, rel in [("chrome", ".config/google-chrome"), ("chromium", ".config/chromium")]:
            found += _chromium_profiles(browser, home / rel)
        ff_root = home / ".mozilla" / "firefox"
    if ff_root.is_dir():
        for places in sorted(ff_root.glob("*/places.sqlite")):
            found.append(HistoryFile("firefox", places.parent.name, places))
    return found


def snapshot(src: Path, dest: Path) -> None:
    """Make a self-contained copy of a live SQLite file at ``dest``.

    Copies the file and any -wal/-journal sidecars to a temp folder (so we
    never touch the browser's own files), then uses SQLite's backup API to
    fold them into one consistent file.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / src.name
        shutil.copy2(src, tmp_db)
        for suffix in ("-wal", "-journal"):
            side = src.with_name(src.name + suffix)
            if side.exists():
                shutil.copy2(side, tmp_db.with_name(tmp_db.name + suffix))
        s = sqlite3.connect(str(tmp_db))
        d = sqlite3.connect(str(dest))
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()


def device_name() -> str:
    name = socket.gethostname() or platform.node() or "unknown"
    return name.split(".")[0]


def collect(outbox: Path, device: str | None = None, home: Path | None = None) -> list[Path]:
    """Snapshot every history file found into ``outbox``. Returns the snapshots."""
    device = device or device_name()
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    written = []
    for hf in find_history_files(home):
        safe_profile = "".join(c if c.isalnum() or c in "-_" else "_" for c in hf.profile)
        base = f"{device}__{hf.browser}__{safe_profile}__{stamp}"
        dest = outbox / f"{base}.sqlite"
        try:
            snapshot(hf.path, dest)
        except (OSError, sqlite3.Error) as e:
            print(f"skip {hf.browser}/{hf.profile}: {e}", file=sys.stderr)
            if hf.browser == "safari":
                print(
                    "  Safari's history needs Full Disk Access for the app running this "
                    "(System Settings > Privacy & Security > Full Disk Access).",
                    file=sys.stderr,
                )
            dest.unlink(missing_ok=True)
            continue
        meta = {
            "source": "browser",
            "browser": hf.browser,
            "profile": hf.profile,
            "device": device,
            "original_path": str(hf.path),
            "collected_at": stamp,
        }
        # Write the sidecar last: `wrh ingest` only picks up snapshots that have one.
        dest.with_suffix(".json").write_text(json.dumps(meta, indent=2))
        written.append(dest)
    return written
