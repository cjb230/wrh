import sqlite3

import pytest

from wrh import db, keys


def test_database_file_is_encrypted(conn, paths):
    conn.execute("INSERT INTO timeline (ts, source, source_table, source_key, title) "
                 "VALUES ('2026-01-01T00:00:00.000000Z', 't', 't', '1', 'secret words')")
    conn.commit()
    data = paths.db.read_bytes()
    assert not data.startswith(b"SQLite format 3")
    assert b"secret words" not in data
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(paths.db).execute("SELECT * FROM sqlite_master").fetchall()


def test_wrong_key_is_rejected(conn, paths):
    conn.close()
    with pytest.raises(db.DatabaseError):
        db.connect(paths.db, "ab" * 32)


def test_fts5_works_inside_sqlcipher(conn):
    db.upsert_timeline(conn, "t", "t", [
        {"ts": "2026-01-01T00:00:00.000000Z", "source_key": 1, "title": "Running a marathon",
         "body": None, "url": "https://x"},
    ])
    conn.commit()
    rows = conn.execute("SELECT rowid FROM timeline_fts WHERE timeline_fts MATCH 'run'").fetchall()
    assert len(rows) == 1  # porter stemming: run ~ running


def test_key_validation():
    with pytest.raises(keys.KeyError_):
        keys.validate("not-a-key")
    assert len(keys.generate()) == 64


@pytest.mark.parametrize("size", [0, 5, 1 << 20, (1 << 20) * 2 + 7])
def test_raw_roundtrip_and_tamper(tmp_path, key, size):
    import os

    from wrh import raw

    src = tmp_path / "in"
    src.write_bytes(os.urandom(size))
    enc, out = tmp_path / "enc", tmp_path / "out"
    raw.encrypt_file(src, enc, key)
    raw.decrypt_file(enc, out, key)
    assert out.read_bytes() == src.read_bytes()

    with pytest.raises(raw.RawDecryptError):
        raw.decrypt_file(enc, out, "ab" * 32)  # wrong key
    if size > (1 << 20):
        data = enc.read_bytes()
        enc.write_bytes(data[: len(raw.MAGIC) + 7 + (1 << 20) + 16])  # truncated
        with pytest.raises(raw.RawDecryptError):
            raw.decrypt_file(enc, out, key)
