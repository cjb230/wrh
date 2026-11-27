"""Email attachments, stored once each, outside the messages.

During sync each attachment is taken out of its message and encrypted on
its own under raw/email-attachments/<aa>/<sha256>.enc (same encryption as
the raw archive). The archived message keeps an empty stub part with
``X-WRH-Attachment-SHA256`` and ``X-WRH-Attachment-Size`` headers, so a
reimport still knows which files belonged to it. The same file attached to
many messages is stored once.

``email_attachment_files`` lists the stored files. It is not rebuilt by
``wrh reimport`` (the files are not in the raw messages any more).
``wrh attachment <sha256>`` decrypts one to disk.
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path

from ... import keys, raw
from ...config import Paths
from ...timeutil import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS email_attachment_files (
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    type TEXT,                     -- MIME type
    name TEXT,                     -- file name, the first time it was seen
    path TEXT NOT NULL,            -- relative to raw/
    stored_at TEXT NOT NULL
);
"""

DIR = "email-attachments"


def store(conn, paths: Paths, data: bytes, sha256: str, ctype: str | None, name: str | None) -> bool:
    """Encrypt and record one attachment. Returns False if it was already stored."""
    if conn.execute("SELECT 1 FROM email_attachment_files WHERE sha256 = ?", (sha256,)).fetchone():
        return False
    rel = Path(DIR) / sha256[:2] / f"{sha256}.enc"
    dest = paths.raw / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_root = paths.home / "tmp"
    tmp_root.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_root) as d:
        src = Path(d) / "a"
        src.write_bytes(data)
        raw.encrypt_file(src, dest, keys.get_key())
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO email_attachment_files (sha256, size, type, name, path, stored_at)"
            " VALUES (?, ?, ?, ?, ?, ?)", (sha256, len(data), ctype, name, rel.as_posix(), now_iso()))
    return True


@contextlib.contextmanager
def plaintext(conn, paths: Paths, sha256: str):
    row = conn.execute("SELECT path, name FROM email_attachment_files WHERE sha256 = ?", (sha256,)).fetchone()
    if row is None:
        raise KeyError(f"No stored attachment {sha256}")
    with raw.plaintext(paths, row["path"]) as p:
        yield p, row["name"]
