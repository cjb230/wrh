import datetime as dt
import sqlite3

from wrh import pipeline
from wrh.sources.messages import importer as im

COCOA = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)


def ns(t: dt.datetime) -> int:
    return int((t - COCOA).total_seconds() * 1e9)


def attributed(text: str) -> bytes:
    b = text.encode()
    n = bytes([len(b)]) if len(b) < 0x80 else b"\x81" + len(b).to_bytes(2, "little")
    return b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject" \
           b"\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + n + b + b"\x86\x84\x02iI\x01"


def make_chat_db(path, now):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
    CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT, display_name TEXT,
                       service_name TEXT);
    CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
    CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB, subject TEXT,
        handle_id INTEGER, service TEXT, date INTEGER, date_read INTEGER, date_delivered INTEGER,
        date_edited INTEGER, is_from_me INTEGER, associated_message_guid TEXT, associated_message_type INTEGER,
        thread_originator_guid TEXT, item_type INTEGER, is_audio_message INTEGER, cache_has_attachments INTEGER);
    CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
    CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT, transfer_name TEXT, mime_type TEXT,
                             total_bytes INTEGER);
    CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
    """)
    c.execute("INSERT INTO handle VALUES (1, '+48 600 100 200', 'iMessage'), (2, 'bob@example.org', 'iMessage')")
    c.execute("INSERT INTO chat VALUES (1, 'iMessage;-;+48600100200', '+48600100200', NULL, 'iMessage'),"
              " (2, 'iMessage;+;chat99', 'chat99', 'Bakers', 'iMessage')")
    c.execute("INSERT INTO chat_handle_join VALUES (1, 1), (2, 1), (2, 2)")
    t = ns(now - dt.timedelta(hours=1))
    msgs = [
        (1, "G1", "Is the starter ready?", None, 1, "iMessage", t, 0, 0),
        (2, "G2", None, attributed("Yes, it doubled overnight"), 1, "iMessage", t + 10**9, 1, 0),
        (3, "G3", None, None, 1, "iMessage", t + 2 * 10**9, 1, 2001),  # tapback "like" on G1
        (4, "G4", "Bread at 6?", None, 2, "SMS", t + 3 * 10**9, 0, 0),
    ]
    for rowid, guid, text, ab, h, svc, d, me, atype in msgs:
        c.execute("INSERT INTO message (ROWID, guid, text, attributedBody, handle_id, service, date, is_from_me,"
                  " associated_message_guid, associated_message_type, item_type) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                  (rowid, guid, text, ab, h, svc, d, me, "p:0/G1" if atype else None, atype))
    c.execute("INSERT INTO chat_message_join VALUES (1, 1), (1, 2), (1, 3), (2, 4)")
    c.execute("INSERT INTO attachment VALUES (1, '~/Library/Messages/Attachments/a/IMG_1.HEIC', 'IMG_1.HEIC',"
              " 'image/heic', 12345)")
    c.execute("INSERT INTO message_attachment_join VALUES (2, 1)")
    c.commit()
    return c


def make_contacts(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZMIDDLENAME TEXT, ZLASTNAME TEXT,
                              ZORGANIZATION TEXT);
    CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
    CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT);
    INSERT INTO ZABCDRECORD VALUES (1, 'Anna', NULL, 'Nowak', NULL), (2, NULL, NULL, NULL, 'Bob''s Bakery');
    INSERT INTO ZABCDPHONENUMBER VALUES (1, '600-100-200');
    INSERT INTO ZABCDEMAILADDRESS VALUES (2, 'Bob@Example.org');
    """)
    c.commit()
    c.close()


def test_decode_attributed_body():
    assert im.decode_attributed_body(attributed("hello")) == "hello"
    long = "x" * 300
    assert im.decode_attributed_body(attributed(long)) == long
    assert im.decode_attributed_body(b"garbage") is None


def test_sync_and_timeline(conn, paths, tmp_path):
    now = dt.datetime.now(dt.timezone.utc)
    db = make_chat_db(tmp_path / "chat.db", now)
    (tmp_path / "ab").mkdir()
    make_contacts(tmp_path / "ab" / "AddressBook-v22.abcddb")
    (paths.home / "config.toml").write_text(
        f'[messages]\nchat_db = "{tmp_path / "chat.db"}"\ncontacts_glob = "{tmp_path}/ab/*.abcddb"\n')

    r = pipeline.sync_source(conn, paths, "messages")
    assert (r.status, r.rows_read, r.timeline_rows) == ("ok", 4, 3)  # the tapback is not on the timeline

    m = {x["id"]: x for x in conn.execute("SELECT * FROM messages_messages")}
    assert m["G2"]["text"] == "Yes, it doubled overnight" and m["G2"]["is_from_me"] == 1
    assert (m["G3"]["kind"], m["G3"]["reaction"], m["G3"]["reaction_to"]) == ("reaction", "like", "G1")
    names = dict(conn.execute("SELECT id, name FROM messages_handles").fetchall())
    assert names == {"+48 600 100 200": "Anna Nowak", "bob@example.org": "Bob's Bakery"}

    titles = {h["title"] for h in pipeline.search(conn, "starter OR doubled OR bread")}
    assert titles == {"iMessage from Anna Nowak", "iMessage to Anna Nowak", "SMS in Bakers from Bob's Bakery"}
    assert pipeline.search(conn, "IMG_1")  # attachment names are searchable

    # Nothing new: nothing archived. A new message: only it is exported.
    assert pipeline.sync_source(conn, paths, "messages").files == 0
    db.execute("INSERT INTO message (ROWID, guid, text, handle_id, service, date, is_from_me, item_type,"
               " associated_message_type) VALUES (5, 'G5', 'Sure', 2, 'SMS', ?, 1, 0, 0)", (ns(now),))
    db.execute("INSERT INTO chat_message_join VALUES (2, 5)")
    db.commit()
    r = pipeline.sync_source(conn, paths, "messages")
    assert (r.files, r.rows_read, r.message) == (1, 1, "1 new, 1 exported")
    assert pipeline.search(conn, "sure")[0]["title"] == "SMS in Bakers"


def test_missing_db_is_an_error(conn, paths, tmp_path):
    (paths.home / "config.toml").write_text(f'[messages]\nchat_db = "{tmp_path / "nope.db"}"\n')
    try:
        pipeline.sync_source(conn, paths, "messages")
    except FileNotFoundError:
        pass
    row = conn.execute("SELECT status, error FROM import_runs").fetchone()
    assert row["status"] == "error" and "not found" in row["error"]
