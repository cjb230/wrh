"""Messages: iMessage, SMS and RCS, from the Mac's Messages database.

With "Messages in iCloud" on, the server's own ``~/Library/Messages/chat.db``
holds the iPhone's conversations too, so sync reads that file directly:
no phone, no backups. The process running it needs Full Disk Access.

Each sync exports messages added since the last one (by ROWID), plus the
last few days again so edits, unsends and read receipts are picked up, as
one ``.jsonl.gz`` raw file. Names come from the Mac's Contacts database,
matched on email address or the last 9 digits of a phone number.

Tables:

* ``messages_messages`` - one row per message, tapback or system event.
  ``kind`` is message, reaction or event; only messages reach the timeline.
* ``messages_chats``    - one row per conversation (1:1 or group).
* ``messages_handles``  - one row per phone number or email address, with
  the contact name if one was found.
"""

from __future__ import annotations

import datetime as dt
import glob
import gzip
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from ...timeutil import to_iso
from ..base import Source

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages_messages (
    id TEXT PRIMARY KEY,           -- the message GUID
    rowid INTEGER,                 -- ROWID in chat.db
    ts TEXT NOT NULL,
    chat_id TEXT REFERENCES messages_chats(id),
    handle TEXT,                   -- the other person (sender, if not you)
    is_from_me INTEGER,
    service TEXT,                  -- iMessage, SMS, RCS
    kind TEXT,                     -- message, reaction, event
    text TEXT,
    subject TEXT,
    attachments TEXT,              -- JSON list of {name, type, size, path}
    reply_to TEXT,                 -- GUID of the message this replies to (threads)
    reaction_to TEXT,              -- for tapbacks: GUID of the message reacted to
    reaction TEXT,                 -- love, like, dislike, laugh, emphasize, question (or removed_...)
    date_read TEXT,
    date_delivered TEXT,
    date_edited TEXT,
    is_audio INTEGER,
    raw_file_id INTEGER REFERENCES raw_files(id)
);
CREATE INDEX IF NOT EXISTS messages_messages_ts ON messages_messages(ts);
CREATE INDEX IF NOT EXISTS messages_messages_chat ON messages_messages(chat_id, ts);
CREATE INDEX IF NOT EXISTS messages_messages_handle ON messages_messages(handle);
CREATE INDEX IF NOT EXISTS messages_messages_raw ON messages_messages(raw_file_id);

CREATE TABLE IF NOT EXISTS messages_chats (
    id TEXT PRIMARY KEY,           -- chat GUID
    identifier TEXT,               -- phone, email or group id
    display_name TEXT,             -- group name, if set
    service TEXT,
    participants TEXT,             -- JSON list of handles
    is_group INTEGER
);

CREATE TABLE IF NOT EXISTS messages_handles (
    id TEXT PRIMARY KEY,           -- phone number or email, as Messages stores it
    service TEXT,
    name TEXT                      -- from Contacts, when matched
);
"""

DEFAULT_DB = "~/Library/Messages/chat.db"
CONTACTS_GLOB = "~/Library/Application Support/AddressBook/**/AddressBook-v22.abcddb"
RECHECK_DAYS = 3
MAX_PER_RUN = 50_000
COCOA = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)

REACTIONS = {2000: "love", 2001: "like", 2002: "dislike", 2003: "laugh", 2004: "emphasize",
             2005: "question", 2006: "emoji", 2007: "sticker"}


def apple_time(v) -> str | None:
    """chat.db dates: nanoseconds since 2001 (seconds in old databases); 0 means none."""
    if not v:
        return None
    v = int(v)
    secs = v / 1e9 if abs(v) > 1e11 else v
    return to_iso(COCOA + dt.timedelta(seconds=secs))


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Text from ``attributedBody`` (an NSAttributedString in typedstream form).

    Since macOS Ventura most messages keep their text only here. The string
    follows the "NSString" class name: a '+' marker, a length, then UTF-8.
    """
    if not blob:
        return None
    try:
        i = blob.index(b"NSString")
        b = blob[i + len(b"NSString"):]
        b = b[b.index(b"+") + 1:]
        n = b[0]
        if n == 0x81:
            n, b = int.from_bytes(b[1:3], "little"), b[3:]
        elif n == 0x82:
            n, b = int.from_bytes(b[1:4], "little"), b[4:]
        elif n == 0x83:
            n, b = int.from_bytes(b[1:5], "little"), b[5:]
        else:
            b = b[1:]
        return b[:n].decode("utf-8", errors="replace")
    except (ValueError, IndexError):
        return None


def phone_key(s: str | None) -> str | None:
    if not s or "@" in s:
        return s.lower() if s else None
    digits = re.sub(r"\D", "", s)
    return digits[-9:] if len(digits) >= 7 else None


def read_contacts(pattern: str = CONTACTS_GLOB) -> dict[str, str]:
    """{email or last-9-digits: name} from the Mac's Contacts databases."""
    out = {}
    for path in glob.glob(os.path.expanduser(pattern), recursive=True):
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            people = {r[0]: " ".join(x for x in r[1:4] if x) or r[4]
                      for r in c.execute("SELECT Z_PK, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZORGANIZATION"
                                         " FROM ZABCDRECORD")}
            for owner, value in c.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                k = phone_key(value)
                if k and people.get(owner):
                    out.setdefault(k, people[owner])
            for owner, value in c.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if value and people.get(owner):
                    out.setdefault(value.lower(), people[owner])
            c.close()
        except sqlite3.Error:
            continue
    return out


MESSAGE_SQL = """
SELECT m.ROWID AS rowid, m.guid, m.date, m.date_read, m.date_delivered,
       m.date_edited, m.is_from_me, m.service, m.text, m.attributedBody, m.subject,
       m.associated_message_guid, m.associated_message_type, m.thread_originator_guid,
       m.item_type, m.is_audio_message, m.cache_has_attachments,
       h.id AS handle, c.guid AS chat_guid
FROM message m
LEFT JOIN handle h ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cmj.chat_id
WHERE m.ROWID > :after OR m.date > :recent
ORDER BY m.ROWID
LIMIT :limit
"""


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def export(db_path: Path, after_rowid: int, recent_since: dt.datetime, limit: int = MAX_PER_RUN,
           contacts: dict[str, str] | None = None) -> tuple[list[dict], int]:
    """Read chat.db; return (JSON records, highest ROWID seen)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = MESSAGE_SQL
        cols = _columns(conn, "message")
        for c in ("date_edited", "thread_originator_guid", "attributedBody", "is_audio_message"):
            if c not in cols:
                sql = sql.replace(f"m.{c}", f"NULL AS {c}")
        recent = int((recent_since - COCOA).total_seconds() * 1e9)
        rows = conn.execute(sql, {"after": after_rowid, "recent": recent, "limit": limit}).fetchall()
        rowids = [r["rowid"] for r in rows]
        atts: dict[int, list] = {}
        if rowids:
            q = ("SELECT maj.message_id, a.filename, a.transfer_name, a.mime_type, a.total_bytes"
                 " FROM message_attachment_join maj JOIN attachment a ON a.ROWID = maj.attachment_id"
                 f" WHERE maj.message_id IN ({','.join('?' * len(rowids))})")
            for mid, fn, tn, mime, size in conn.execute(q, rowids):
                atts.setdefault(mid, []).append({"name": tn or (Path(fn).name if fn else None),
                                                 "type": mime, "size": size, "path": fn})
        chats = {}
        for c in conn.execute("SELECT ROWID, guid, chat_identifier, display_name, service_name FROM chat"):
            chats[c["ROWID"]] = {"kind": "chat", "id": c["guid"], "identifier": c["chat_identifier"],
                                 "display_name": c["display_name"] or None, "service": c["service_name"],
                                 "participants": []}
        for chat_id, handle in conn.execute(
                "SELECT chj.chat_id, h.id FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id"):
            if chat_id in chats:
                chats[chat_id]["participants"].append(handle)
        contacts = contacts or {}
        handles = [{"kind": "handle", "id": h["id"], "service": h["service"],
                    "name": contacts.get(phone_key(h["id"]) or "")}
                   for h in conn.execute("SELECT DISTINCT id, service FROM handle")]
    finally:
        conn.close()

    # Only the chats and people these messages involve.
    used_chats = [c for c in chats.values() if c["id"] in {r["chat_guid"] for r in rows}]
    used_handles = {r["handle"] for r in rows} | {p for c in used_chats for p in c["participants"]}
    out: list[dict] = [h for h in handles if h["id"] in used_handles] + used_chats
    for r in rows:
        text = r["text"] or decode_attributed_body(r["attributedBody"])
        out.append({
            "kind": "message", "rowid": r["rowid"], "guid": r["guid"], "date": r["date"],
            "date_read": r["date_read"], "date_delivered": r["date_delivered"], "date_edited": r["date_edited"],
            "is_from_me": r["is_from_me"], "service": r["service"], "text": text, "subject": r["subject"],
            "associated_message_guid": r["associated_message_guid"],
            "associated_message_type": r["associated_message_type"],
            "thread_originator_guid": r["thread_originator_guid"], "item_type": r["item_type"],
            "is_audio_message": r["is_audio_message"], "handle": r["handle"], "chat_guid": r["chat_guid"],
            "attachments": atts.get(r["rowid"], []),
            "undecoded": bool(r["attributedBody"] and not r["text"] and not text),
        })
    return out, max(rowids, default=after_rowid)


def _strip_assoc(guid: str | None) -> str | None:
    # "p:0/GUID" or "bp:GUID" -> "GUID"
    if not guid:
        return None
    return guid.split("/", 1)[1] if "/" in guid else guid.split(":", 1)[-1]


def read_batch(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


MSG_COLS = ["id", "rowid", "ts", "chat_id", "handle", "is_from_me", "service", "kind", "text", "subject",
            "attachments", "reply_to", "reaction_to", "reaction", "date_read", "date_delivered",
            "date_edited", "is_audio", "raw_file_id"]


class Messages(Source):
    name = "messages"
    timeline_table = "messages_messages"
    schema = SCHEMA
    can_sync = True

    def reset(self, conn) -> None:
        for t in ("messages_messages", "messages_chats", "messages_handles"):
            conn.execute(f"DELETE FROM {t}")

    def import_file(self, conn, path: Path, meta: Mapping, raw_file_id: int) -> int:
        n = 0
        for rec in read_batch(Path(path)):
            k = rec.get("kind")
            if k == "handle":
                conn.execute(
                    "INSERT INTO messages_handles (id, service, name) VALUES (?, ?, ?)"
                    " ON CONFLICT (id) DO UPDATE SET service = excluded.service,"
                    " name = coalesce(excluded.name, name)", (rec["id"], rec["service"], rec["name"]))
            elif k == "chat":
                conn.execute(
                    "INSERT OR REPLACE INTO messages_chats (id, identifier, display_name, service, participants,"
                    " is_group) VALUES (?, ?, ?, ?, ?, ?)",
                    (rec["id"], rec["identifier"], rec["display_name"], rec["service"],
                     json.dumps(sorted(rec["participants"])), int(len(rec["participants"]) > 1)))
            elif k == "message":
                atype = rec.get("associated_message_type") or 0
                if 2000 <= atype < 3000:
                    kind, reaction = "reaction", REACTIONS.get(atype, str(atype))
                elif 3000 <= atype < 4000:
                    kind, reaction = "reaction", "removed_" + REACTIONS.get(atype - 1000, str(atype))
                elif rec.get("item_type"):
                    kind, reaction = "event", None
                else:
                    kind, reaction = "message", None
                row = {
                    "id": rec["guid"], "rowid": rec["rowid"], "ts": apple_time(rec["date"]) or "1970-01-01T00:00:00.000000Z",
                    "chat_id": rec["chat_guid"], "handle": rec["handle"], "is_from_me": rec["is_from_me"],
                    "service": rec["service"], "kind": kind, "text": rec["text"], "subject": rec["subject"],
                    "attachments": json.dumps(rec["attachments"]) if rec["attachments"] else None,
                    "reply_to": rec.get("thread_originator_guid"),
                    "reaction_to": _strip_assoc(rec.get("associated_message_guid")) if kind == "reaction" else None,
                    "reaction": reaction,
                    "date_read": apple_time(rec["date_read"]), "date_delivered": apple_time(rec["date_delivered"]),
                    "date_edited": apple_time(rec.get("date_edited")), "is_audio": rec.get("is_audio_message"),
                    "raw_file_id": raw_file_id,
                }
                conn.execute(
                    f"INSERT INTO messages_messages ({', '.join(MSG_COLS)}) VALUES ({', '.join(':' + c for c in MSG_COLS)})"
                    " ON CONFLICT (id) DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in MSG_COLS if c != "id"),
                    row,
                )
                n += 1
        return n

    def timeline_rows(self, conn, raw_file_id: int | None) -> Iterable[Mapping]:
        sql = """
        SELECT m.id, m.ts, m.text, m.is_from_me, m.handle, m.attachments, m.service, m.subject,
               c.display_name, c.participants, c.is_group, h.name AS handle_name
        FROM messages_messages m
        LEFT JOIN messages_chats c ON c.id = m.chat_id
        LEFT JOIN messages_handles h ON h.id = m.handle
        WHERE m.kind = 'message' AND (:raw IS NULL OR m.raw_file_id = :raw)
        """
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM messages_handles")}
        for r in conn.execute(sql, {"raw": raw_file_id}):
            person = r["handle_name"] or r["handle"]
            if r["is_group"]:
                group = r["display_name"] or ", ".join(
                    names.get(p) or p for p in json.loads(r["participants"] or "[]"))
                who = f"in {group}" + ("" if r["is_from_me"] else f" from {person}")
            elif r["is_from_me"]:
                chat_people = json.loads(r["participants"] or "[]")
                to = names.get(chat_people[0]) or chat_people[0] if chat_people else person
                who = f"to {to or '?'}"
            else:
                who = f"from {person or '?'}"
            body = " ".join(x for x in (r["subject"], r["text"]) if x) or ""
            for a in json.loads(r["attachments"] or "[]"):
                body += f"\n[attachment: {a.get('name') or a.get('type') or 'file'}]"
            target = r["handle"] or ""
            yield {
                "source_key": r["id"],
                "ts": r["ts"],
                "title": f"{r['service'] or 'Message'} {who}",
                "body": body.strip() or None,
                "url": f"sms:{target}" if target else None,
            }

    def sync(self, ctx) -> None:
        db_path = Path(os.path.expanduser(ctx.settings.get("chat_db") or DEFAULT_DB))
        if not db_path.exists():
            raise FileNotFoundError(f"{db_path} not found. Is Messages set up on this Mac?")
        try:
            open(db_path, "rb").close()
        except PermissionError as e:
            raise PermissionError(
                f"Cannot read {db_path}. Give Full Disk Access to the Python that runs wrh "
                "(System Settings > Privacy & Security > Full Disk Access).") from e
        after = int(ctx.get_state("last_rowid") or 0)
        now = dt.datetime.now(dt.timezone.utc)
        # Once a day, export the last few days again to pick up edits, unsends and read receipts.
        last_recheck = ctx.get_state("last_recheck")
        recheck = after > 0 and (not last_recheck or now - dt.datetime.fromisoformat(
            last_recheck.replace("Z", "+00:00")) > dt.timedelta(hours=24))
        recent = now - dt.timedelta(days=RECHECK_DAYS) if recheck else now + dt.timedelta(days=3650)
        contacts = {}
        try:
            contacts = read_contacts(ctx.settings.get("contacts_glob") or CONTACTS_GLOB)
        except Exception as e:
            ctx.run.warn(f"Could not read Contacts for names: {e}")
        if not contacts:
            ctx.run.warn("No contact names found; messages will show phone numbers and emails only")
        records, last = export(db_path, after, recent, int(ctx.settings.get("max_per_run") or MAX_PER_RUN),
                               contacts)
        msgs = [r for r in records if r["kind"] == "message"]
        undecoded = sum(1 for r in msgs if r["undecoded"])
        if undecoded:
            ctx.run.warn(f"{undecoded} message(s) had text in a format that could not be decoded")
        if not msgs:
            ctx.run.message = "no new messages"
            if recheck:
                ctx.set_state("last_recheck", to_iso(now))
            return
        data = gzip.compress(("\n".join(json.dumps(r) for r in records) + "\n").encode(), mtime=0)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        ctx.save(f"messages-{stamp}-{after + 1}-{last}.jsonl.gz", data, {"chat_db": str(db_path)})
        ctx.set_state("last_rowid", last)
        if recheck or after == 0:
            ctx.set_state("last_recheck", to_iso(now))
        ctx.run.message = f"{sum(1 for r in msgs if r['rowid'] > after)} new, {len(msgs)} exported"
