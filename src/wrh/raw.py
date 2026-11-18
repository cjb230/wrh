"""The raw archive: copies of every file we import, encrypted.

Files are stored content-addressed (by the SHA-256 of the *plaintext*) under
raw/<source>/<yyyy>/<mm>/<hash>__<name>.enc, so the same file is never stored
twice, and every import can be replayed with `wrh reimport <source>`.

Each file is encrypted with AES-256-GCM in 1 MiB chunks, using a key derived
from the database key with HKDF-SHA256 (so one key to back up, but the raw
archive and the database never share key material directly). Format:

    magic   b"WRHRAW1\\n"   8 bytes
    nonce   random          7 bytes  (per file)
    chunks  AES-GCM(chunk)  each up to 1 MiB + 16-byte tag

Chunk i uses nonce = file_nonce || i (4 bytes, big endian) || last (1 byte),
and the header as associated data, so chunks cannot be reordered, dropped,
or truncated without detection. Decrypted copies only ever exist in a
temporary 0700 folder under the data directory while an import runs.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import keys
from .config import Paths
from .timeutil import now_iso

MAGIC = b"WRHRAW1\n"
CHUNK = 1 << 20
TAG = 16


class RawDecryptError(RuntimeError):
    pass


def raw_key(db_key: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"wrh raw archive v1"
    ).derive(bytes.fromhex(keys.validate(db_key)))


def _nonce(prefix: bytes, i: int, last: bool) -> bytes:
    return prefix + i.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


def encrypt_file(src: Path, dest: Path, db_key: str) -> None:
    aead = AESGCM(raw_key(db_key))
    prefix = secrets.token_bytes(7)
    header = MAGIC + prefix
    tmp = dest.with_name(dest.name + ".part")
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        os.chmod(tmp, 0o600)
        fout.write(header)
        i = 0
        chunk = fin.read(CHUNK)
        while True:
            nxt = fin.read(CHUNK)
            last = not nxt
            fout.write(aead.encrypt(_nonce(prefix, i, last), chunk, header))
            if last:
                break
            chunk, i = nxt, i + 1
    tmp.replace(dest)


def decrypt_file(src: Path, dest: Path, db_key: str) -> None:
    aead = AESGCM(raw_key(db_key))
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        os.chmod(dest, 0o600)
        header = fin.read(len(MAGIC) + 7)
        if not header.startswith(MAGIC):
            raise RawDecryptError(f"{src} is not a wrh raw archive file")
        prefix = header[len(MAGIC):]
        i = 0
        chunk = fin.read(CHUNK + TAG)
        while True:
            nxt = fin.read(CHUNK + TAG)
            last = not nxt
            try:
                fout.write(aead.decrypt(_nonce(prefix, i, last), chunk, header))
            except Exception as e:
                raise RawDecryptError(f"{src}: wrong key or damaged file (chunk {i})") from e
            if last:
                break
            chunk, i = nxt, i + 1


@contextlib.contextmanager
def plaintext(paths: Paths, rel: str, db_key: str | None = None):
    """Decrypt an archived file to a private temp file; delete it afterwards."""
    tmp_root = paths.home / "tmp"
    tmp_root.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_root) as d:
        out = Path(d) / Path(rel).name.removesuffix(".enc")
        decrypt_file(paths.raw / rel, out, db_key or keys.get_key())
        yield out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def utcnow() -> str:
    return now_iso()


def archive(conn, paths: Paths, source: str, file: Path, meta: dict | None = None,
            db_key: str | None = None):
    """Encrypt ``file`` into the raw archive and record it.

    Returns (raw_file_id, is_new). If the identical file was archived before,
    nothing is stored and is_new is False.
    """
    file = Path(file)
    digest = sha256_file(file)
    row = conn.execute("SELECT id FROM raw_files WHERE sha256 = ?", (digest,)).fetchone()
    if row:
        return row[0], False
    now = dt.datetime.now(dt.timezone.utc)
    rel = Path(source) / f"{now:%Y}" / f"{now:%m}" / f"{digest[:16]}__{file.name}.enc"
    dest = paths.raw / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    encrypt_file(file, dest, db_key or keys.get_key())
    cur = conn.execute(
        "INSERT INTO raw_files (source, sha256, path, original_name, meta, archived_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (source, digest, rel.as_posix(), file.name, json.dumps(meta or {}), utcnow()),
    )
    return cur.lastrowid, True


def mark_imported(conn, raw_file_id: int) -> None:
    conn.execute("UPDATE raw_files SET imported_at = ? WHERE id = ?", (utcnow(), raw_file_id))
