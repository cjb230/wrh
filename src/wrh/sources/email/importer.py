"""Email: Gmail and any other IMAP account, plus mbox files (Google Takeout).

Sync (``wrh sync email``, or the scheduler) logs in over IMAP with an app
password from the Keychain and fetches messages it has not seen, folder by
folder, by IMAP UID. For Gmail it reads "[Gmail]/All Mail" (every message
once) and keeps Gmail's labels and ids. Mail is only read, never changed:
folders are opened read-only.

Each batch of fetched messages is archived as one ``.jsonl.gz`` raw file
(one JSON line per message, the full RFC 822 message base64-encoded), then
imported. ``wrh import email <file>.mbox`` imports a Takeout mbox the same way.

Tables:

* ``email_messages``     - one row per message: headers, plain-text body,
  attachment names, Gmail labels and thread id.
* ``email_participants`` - one row per (message, person, role) with role
  from, to, cc, bcc, reply_to. Join on ``address`` to find everything with
  someone.
* ``email_correspondents`` - everyone you have sent mail to, read from your
  Sent folder before anything else, so their mail is never filed as bulk.
* ``email_attachment_files`` - attachments, stored once each (attachments.py).

Every message gets a ``category`` (personal, list, automated, bulk, spam;
see classify.py). ``[email.keep]`` in config.toml decides what is kept per
category; by default bulk mail and spam are not kept at all.
Bulk and spam stay off the timeline.
"""

from __future__ import annotations

import base64
import datetime as dt
import email
import email.policy
import email.utils
import gzip
import hashlib
import html
import imaplib
import json
import mailbox
import re
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from ... import keys
from ...timeutil import to_iso
from ..base import Source
from . import attachments as att_store
from .classify import DEFAULT_KEEP, TIMELINE_EXCLUDE, classify

SCHEMA = """
CREATE TABLE IF NOT EXISTS email_messages (
    id TEXT PRIMARY KEY,           -- account:gmail id, or account:Message-ID
    account TEXT NOT NULL,         -- name from config.toml, or 'takeout'
    message_id TEXT,               -- the Message-ID header
    ts TEXT NOT NULL,              -- Date header (or server arrival time), UTC
    received_at TEXT,              -- server arrival time (IMAP INTERNALDATE)
    from_name TEXT,
    from_address TEXT,
    to_text TEXT,
    cc_text TEXT,
    subject TEXT,
    body TEXT,                     -- plain text (from text/plain, else HTML stripped)
    snippet TEXT,                  -- first 200 characters of body
    attachments TEXT,              -- JSON list of {name, type, size}
    in_reply_to TEXT,
    refs TEXT,                     -- References header
    folders TEXT,                  -- JSON list of IMAP folders it was found in
    labels TEXT,                   -- JSON list of Gmail labels
    gm_msgid TEXT,                 -- Gmail message id (decimal)
    gm_thrid TEXT,                 -- Gmail thread id (decimal)
    flags TEXT,                    -- IMAP flags, e.g. \\Seen \\Flagged
    is_sent INTEGER,               -- 1 if you sent it
    size INTEGER,                  -- bytes, as received
    category TEXT,                 -- personal, list, automated, bulk, spam
    category_reason TEXT,          -- the rule that decided it
    stored TEXT,                   -- full, or headers (body and attachments not kept)
    raw_file_id INTEGER REFERENCES raw_files(id)
);
CREATE INDEX IF NOT EXISTS email_messages_category ON email_messages(category);
CREATE INDEX IF NOT EXISTS email_messages_ts ON email_messages(ts);
CREATE INDEX IF NOT EXISTS email_messages_from ON email_messages(from_address);
CREATE INDEX IF NOT EXISTS email_messages_thread ON email_messages(gm_thrid);
CREATE INDEX IF NOT EXISTS email_messages_raw ON email_messages(raw_file_id);

CREATE TABLE IF NOT EXISTS email_participants (
    message_id TEXT NOT NULL REFERENCES email_messages(id),
    role TEXT NOT NULL,            -- from, to, cc, bcc, reply_to
    name TEXT,
    address TEXT NOT NULL,         -- lower-cased
    PRIMARY KEY (message_id, role, address)
);
CREATE INDEX IF NOT EXISTS email_participants_address ON email_participants(address);

CREATE TABLE IF NOT EXISTS email_correspondents (
    account TEXT NOT NULL,
    address TEXT NOT NULL,         -- lower-cased
    times INTEGER NOT NULL,        -- messages you sent them
    first_ts TEXT,
    last_ts TEXT,
    PRIMARY KEY (account, address)
);
""" + att_store.SCHEMA

GMAIL_ALL_MAIL = "[Gmail]/All Mail"
GMAIL_SENT = "[Gmail]/Sent Mail"
GMAIL_CATEGORIES = ("promotions", "updates", "social", "forums")
MIN_INLINE_IMAGE_KB = 20     # smaller inline images (logos, tracking pixels) are not stored
MAX_ATTACHMENT_MB = 50
BATCH = 200                  # messages per IMAP fetch
DEFAULT_MAX_PER_RUN = 5000   # so the first sync of a big mailbox spreads over several runs
BODY_LIMIT = 100_000         # characters of body kept per message
POLICY = email.policy.default


# --- parsing ------------------------------------------------------------------

def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def _decode_part(part) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def is_attachment(part) -> bool:
    if part.is_multipart() or part.get_content_type().startswith("multipart/"):
        return False
    if part.get_content_type() == "message/rfc822":
        return True
    disp = part.get_content_disposition()
    return disp == "attachment" or (bool(part.get_filename()) and disp != "inline") \
        or not part.get_content_type().startswith("text/")


def body_and_attachments(msg) -> tuple[str, list[dict]]:
    plain, htmls, atts = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if is_attachment(part):
            sha = str(part.get("X-WRH-Attachment-SHA256") or "").strip() or None
            if sha or part.get("X-WRH-Attachment-Skipped"):
                size = int(str(part.get("X-WRH-Attachment-Size") or 0)) or None
            else:
                payload = part.get_payload(decode=True)
                size = len(payload) if payload else None
            atts.append({"name": part.get_filename(), "type": ctype, "size": size, "sha256": sha,
                         "skipped": str(part.get("X-WRH-Attachment-Skipped") or "") or None})
            continue
        if ctype == "text/plain":
            plain.append(_decode_part(part))
        elif ctype == "text/html":
            htmls.append(_decode_part(part))
    text = "\n\n".join(p for p in plain if p.strip()) or _html_to_text("\n".join(htmls))
    return text.strip()[:BODY_LIMIT], atts


def split_attachments(raw: bytes, save, *, min_inline_kb=MIN_INLINE_IMAGE_KB,
                      max_mb=MAX_ATTACHMENT_MB) -> bytes:
    """Take attachments out of a raw message.

    ``save(data, sha256, type, name)`` stores each one. Returns the message
    with every attachment replaced by an empty stub that names it.
    """
    msg = email.message_from_bytes(raw)  # compat32: closest to the original bytes
    changed = False
    for part in msg.walk():
        if not is_attachment(part):
            continue
        data = part.get_payload(decode=True)
        if data is None:  # an attached message/rfc822 is a list of parts
            data = part.as_bytes()
            part.set_payload([])
        if not data:
            continue
        sha = hashlib.sha256(data).hexdigest()
        ctype, name = part.get_content_type(), part.get_filename()
        skipped = None
        if part.get_content_disposition() == "inline" and ctype.startswith("image/") \
                and len(data) < min_inline_kb * 1024:
            skipped = "small inline image"
        elif len(data) > max_mb * 1024 * 1024:
            skipped = f"over {max_mb} MB"
        else:
            save(data, sha, ctype, name)
        if part.get_content_type() != "message/rfc822":
            part.set_payload("")
        del part["Content-Transfer-Encoding"]
        part["Content-Transfer-Encoding"] = "7bit"
        part["X-WRH-Attachment-Size"] = str(len(data))
        if skipped:
            part["X-WRH-Attachment-Skipped"] = skipped
        else:
            part["X-WRH-Attachment-SHA256"] = sha
        changed = True
    return msg.as_bytes() if changed else raw


def headers_only(raw: bytes) -> bytes:
    for sep in (b"\r\n\r\n", b"\n\n"):
        i = raw.find(sep)
        if i >= 0:
            return raw[: i + len(sep)]
    return raw


def _addresses(msg, header: str) -> list[tuple[str, str]]:
    values = msg.get_all(header, [])
    out = []
    for name, addr in email.utils.getaddresses([str(v) for v in values]):
        if addr and "@" in addr:
            out.append((name or None, addr.strip().lower()))
    return out


def _date(msg, fallback: str | None) -> str | None:
    d = msg.get("Date")
    if d:
        try:
            t = email.utils.parsedate_to_datetime(str(d))
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
            return to_iso(t)
        except (TypeError, ValueError, IndexError):
            pass
    return fallback


def parse_message(raw: bytes, *, account: str, received_at: str | None = None,
                  folders=(), labels=(), flags=(), gm_msgid=None, gm_thrid=None,
                  my_addresses=(), category=None, category_reason=None, stored="full",
                  size=None, correspondents=frozenset()) -> dict:
    msg = email.message_from_bytes(raw, policy=POLICY)
    frm = _addresses(msg, "From")
    message_id = (str(msg.get("Message-ID") or "").strip() or None)
    if gm_msgid:
        mid = f"{account}:gm:{gm_msgid}"
    elif message_id:
        mid = f"{account}:{message_id}"
    else:
        mid = f"{account}:sha1:{hashlib.sha1(raw).hexdigest()}"
    labels = list(labels)
    from_addr = frm[0][1] if frm else None
    is_sent = int("\\Sent" in labels or "Sent" in labels or (from_addr in my_addresses if from_addr else False))
    if category is None:
        category, category_reason = classify(msg, from_address=from_addr, labels=labels,
                                             is_sent=bool(is_sent), correspondents=correspondents)
    if stored == "full":
        body, atts = body_and_attachments(msg)
    else:
        body, atts = "", []
    return {
        "id": mid,
        "account": account,
        "message_id": message_id,
        "ts": _date(msg, received_at) or received_at or "1970-01-01T00:00:00.000000Z",
        "received_at": received_at,
        "from_name": frm[0][0] if frm else None,
        "from_address": from_addr,
        "to_text": str(msg.get("To") or "") or None,
        "cc_text": str(msg.get("Cc") or "") or None,
        "subject": str(msg.get("Subject") or "").strip() or None,
        "snippet": re.sub(r"\s+", " ", body)[:200] or None,
        "attachments": json.dumps(atts) if atts else None,
        "in_reply_to": str(msg.get("In-Reply-To") or "").strip() or None,
        "refs": str(msg.get("References") or "").strip() or None,
        "folders": json.dumps(sorted(set(folders))) if folders else None,
        "labels": json.dumps(labels) if labels else None,
        "gm_msgid": str(gm_msgid) if gm_msgid else None,
        "gm_thrid": str(gm_thrid) if gm_thrid else None,
        "flags": " ".join(flags) or None,
        "is_sent": is_sent,
        "size": size or len(raw),
        "category": category,
        "category_reason": category_reason,
        "stored": stored,
        "body": body or None,
        "_participants": [(role, name, addr) for role, hdr in
                          (("from", "From"), ("to", "To"), ("cc", "Cc"), ("bcc", "Bcc"), ("reply_to", "Reply-To"))
                          for name, addr in _addresses(msg, hdr)],
    }


# --- reading raw files ----------------------------------------------------------

def read_batch(path: Path) -> Iterator[dict]:
    """Our own sync batches: gzip'd JSON lines."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def read_mbox(path: Path, account: str) -> Iterator[dict]:
    """An mbox file, e.g. Google Takeout's 'All mail Including Spam and Trash.mbox'."""
    box = mailbox.mbox(str(path), create=False)
    try:
        for key in box.iterkeys():
            raw = box.get_bytes(key)
            msg = email.message_from_bytes(raw, policy=POLICY)
            labels = [x.strip() for x in str(msg.get("X-Gmail-Labels") or "").split(",") if x.strip()]
            # Takeout puts Gmail's thread id in X-GM-THRID.
            yield {"account": account, "raw": raw, "labels": labels,
                   "gm_thrid": str(msg.get("X-GM-THRID") or "").strip() or None}
    finally:
        box.close()


def known_correspondents(conn, account: str | None = None) -> set[str]:
    """Everyone you have sent mail to: from the Sent folder scan and from imported sent mail."""
    rows = conn.execute(
        "SELECT address FROM email_correspondents WHERE (:a IS NULL OR account = :a)"
        " UNION SELECT p.address FROM email_participants p JOIN email_messages m ON m.id = p.message_id"
        " WHERE m.is_sent = 1 AND p.role IN ('to', 'cc', 'bcc')", {"a": account})
    return {r[0] for r in rows}


def mbox_correspondents(path: Path, my_addresses=()) -> set[str]:
    """Recipients of the sent mail in an mbox (a first pass, before sorting the rest)."""
    out = set()
    for it in read_mbox(path, "x"):
        msg = email.message_from_bytes(headers_only(it["raw"]), policy=POLICY)
        frm = _addresses(msg, "From")
        if "Sent" in it["labels"] or (frm and frm[0][1] in my_addresses):
            for hdr in ("To", "Cc", "Bcc"):
                out |= {a for _, a in _addresses(msg, hdr)}
    return out


def _is_batch(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


COLS = ["id", "account", "message_id", "ts", "received_at", "from_name", "from_address", "to_text",
        "cc_text", "subject", "body", "snippet", "attachments", "in_reply_to", "refs", "folders", "labels",
        "gm_msgid", "gm_thrid", "flags", "is_sent", "size", "category", "category_reason", "stored",
        "raw_file_id"]
UPSERT = (
    f"INSERT INTO email_messages ({', '.join(COLS)}) VALUES ({', '.join(':' + c for c in COLS)})"
    " ON CONFLICT (id) DO UPDATE SET labels = coalesce(excluded.labels, labels),"
    " flags = coalesce(excluded.flags, flags), category = excluded.category,"
    " category_reason = excluded.category_reason,"
    " folders = (SELECT json_group_array(value) FROM (SELECT DISTINCT value FROM json_each("
    "   coalesce(email_messages.folders, '[]')) UNION SELECT value FROM json_each(coalesce(excluded.folders, '[]'))))"
)


class Email(Source):
    name = "email"
    timeline_table = "email_messages"
    schema = SCHEMA
    can_sync = True

    def reset(self, conn) -> None:
        # email_correspondents and email_attachment_files are kept: they are
        # not rebuilt from the raw messages.
        conn.execute("DELETE FROM email_participants")
        conn.execute("DELETE FROM email_messages")

    # -- import --

    def import_file(self, conn, path: Path, meta: Mapping, raw_file_id: int) -> int:
        """A sync batch (already sorted and trimmed by sync), or an mbox file
        (sorted here, with [email.keep]'s defaults: bulk and spam skipped)."""
        path = Path(path)
        my = {a.lower() for a in meta.get("my_addresses", [])}
        keep = {**DEFAULT_KEEP, **(meta.get("keep") or {})}
        if _is_batch(path):
            items, correspondents = read_batch(path), frozenset()
        else:
            account = meta.get("account") or "takeout"
            correspondents = known_correspondents(conn, account) | mbox_correspondents(path, my)
            items = read_mbox(path, account)
        n = 0
        for it in items:
            raw = it["raw"] if isinstance(it.get("raw"), bytes) else base64.b64decode(it["raw_b64"])
            labels = it.get("labels") or ()
            if set(labels) & {"Trash", "\\Trash"}:
                continue
            m = parse_message(
                raw, account=it["account"], received_at=it.get("internaldate"),
                folders=[it["folder"]] if it.get("folder") else (), labels=labels,
                flags=it.get("flags") or (), gm_msgid=it.get("gm_msgid"), gm_thrid=it.get("gm_thrid"),
                my_addresses=my, category=it.get("category"), category_reason=it.get("category_reason"),
                stored=it.get("stored") or "full", size=it.get("size"), correspondents=correspondents,
            )
            if "category" not in it:  # mbox: apply the keep rules now
                rule = keep.get(m["category"], "full")
                if rule == "none":
                    continue
                if rule == "headers":
                    m.update(stored="headers", body=None, snippet=None, attachments=None)
            parts = m.pop("_participants")
            conn.execute(UPSERT, {**m, "raw_file_id": raw_file_id})
            conn.executemany(
                "INSERT OR IGNORE INTO email_participants (message_id, role, name, address) VALUES (?, ?, ?, ?)",
                [(m["id"], role, name, addr) for role, name, addr in parts],
            )
            n += 1
        return n

    def timeline_rows(self, conn, raw_file_id: int | None) -> Iterable[Mapping]:
        sql = """
        SELECT id, ts, from_name, from_address, to_text, subject, body, attachments, is_sent,
               gm_msgid, message_id, category
        FROM email_messages WHERE (:raw IS NULL OR raw_file_id = :raw)
        """
        for r in conn.execute(sql, {"raw": raw_file_id}):
            if r["category"] in TIMELINE_EXCLUDE:
                continue
            if r["is_sent"]:
                who = "to " + (r["to_text"] or "?")
            else:
                who = "from " + (r["from_name"] or r["from_address"] or "?")
            body = r["body"] or ""
            for a in json.loads(r["attachments"] or "[]"):
                if a.get("name"):
                    body += f"\n[attachment: {a['name']}]"
            if r["gm_msgid"]:
                url = f"https://mail.google.com/mail/u/0/#all/{int(r['gm_msgid']):x}"
            elif r["message_id"]:
                url = "message://" + r["message_id"].replace("<", "%3C").replace(">", "%3E")
            else:
                url = None
            yield {
                "source_key": r["id"],
                "ts": r["ts"],
                "title": f"Email {who}: {r['subject'] or '(no subject)'}"[:300],
                "body": body,
                "url": url,
            }

    # -- sync over IMAP --

    def sync(self, ctx) -> None:
        accounts = ctx.settings.get("accounts") or []
        if not accounts:
            ctx.run.warn("No [[email.accounts]] in config.toml")
        max_per_run = int(ctx.settings.get("max_messages_per_run") or DEFAULT_MAX_PER_RUN)
        for acct in accounts:
            try:
                n = sync_account(ctx, acct, max_per_run)
            except Exception as e:
                ctx.run.exception(f"Account {acct.get('name')}: {e}", item=acct.get("name"))
                continue
            max_per_run = max(0, max_per_run - n)
        skipped = getattr(ctx, "skipped", {})
        ctx.run.message = f"{ctx.run.rows_read} message(s) kept" + (
            ", skipped " + ", ".join(f"{v} {k}" for k, v in sorted(skipped.items())) if skipped else "")


def _imap_quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


FETCH_RE = re.compile(rb"UID (\d+)")
GM_RE = {k: re.compile(rb"X-GM-%s (\d+)" % k.encode()) for k in ("MSGID", "THRID")}
LABELS_RE = re.compile(rb"X-GM-LABELS \((.*?)\)(?: |$|\))")
FLAGS_RE = re.compile(rb"FLAGS \((.*?)\)")
INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')


def _parse_labels(s: bytes) -> list[str]:
    out = []
    for m in re.finditer(rb'"((?:[^"\\]|\\.)*)"|(\S+)', s):
        v = m.group(1) if m.group(1) is not None else m.group(2)
        out.append(v.decode("utf-8", "replace").replace('\\"', '"').replace("\\\\", "\\"))
    return out


def parse_fetch(data) -> list[dict]:
    """Turn imaplib's fetch response into dicts.

    imaplib returns, per message, a (head, literal) tuple, then a bytes tail
    holding any items the server sent after the message body (often ")",
    sometimes "UID 12 FLAGS (...))"). Head and tail are searched together.
    """
    pending = []  # [head, raw]
    for item in data:
        if isinstance(item, tuple):
            pending.append([item[0], item[1]])
        elif isinstance(item, bytes) and pending:
            pending[-1][0] += b" " + item
    out = []
    for head, raw in pending:
        uid = FETCH_RE.search(head)
        rec = {"uid": int(uid.group(1)) if uid else None, "raw": raw}
        idate = INTERNALDATE_RE.search(head)
        if idate:
            t = email.utils.parsedate_to_datetime(idate.group(1).decode().replace("-", " ", 2))
            rec["internaldate"] = to_iso(t)
        for k, rx in GM_RE.items():
            m = rx.search(head)
            if m:
                rec["gm_" + k.lower()] = m.group(1).decode()
        m = LABELS_RE.search(head)
        if m:
            rec["labels"] = _parse_labels(m.group(1))
        m = FLAGS_RE.search(head)
        if m:
            rec["flags"] = m.group(1).decode().split()
        out.append(rec)
    return out


def _list_folders(imap) -> dict[str, str]:
    """{special-use flag: folder name}, e.g. {"\\All": "[Gmail]/All Mail", "\\Sent": ...}.

    Gmail's folder names are translated ("[Gmail]/Wysłane"), so we find them by flag.
    """
    out = {}
    typ, data = imap.list()
    for line in data or []:
        if not isinstance(line, bytes):
            continue
        m = re.match(rb'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
        if not m:
            continue
        name = m.group("name").strip()
        if name.startswith(b'"'):
            name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
        name = _decode_mutf7(name)
        for flag in m.group("flags").decode().split():
            if flag in ("\\All", "\\Sent", "\\Junk", "\\Trash", "\\Drafts"):
                out.setdefault(flag, name)
    return out


def _decode_mutf7(b: bytes) -> str:
    """IMAP's modified UTF-7 folder names ("Wys&AUI-ane" -> "Wysłane")."""
    out, i = [], 0
    s = b.decode("ascii", "replace")
    while i < len(s):
        if s[i] == "&":
            j = s.index("-", i)
            if j == i + 1:
                out.append("&")
            else:
                chunk = s[i + 1:j].replace(",", "/")
                chunk += "=" * (-len(chunk) % 4)
                out.append(base64.b64decode(chunk).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _encode_mutf7(s: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append("&" + b + "-")
            buf.clear()

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def _select(imap, folder: str):
    return imap.select(_imap_quote(_encode_mutf7(folder)), readonly=True)


def _folder_state(ctx, imap, key: str) -> tuple[dict, str]:
    uidvalidity = (imap.response("UIDVALIDITY")[1] or [b"0"])[0]
    uidvalidity = uidvalidity.decode() if isinstance(uidvalidity, bytes) else str(uidvalidity)
    state = json.loads(ctx.get_state(key) or "{}")
    if state.get("uidvalidity") != uidvalidity:
        state = {"uidvalidity": uidvalidity, "last_uid": 0}
    return state, uidvalidity


def _new_uids(imap, last_uid: int) -> list[int]:
    typ, data = imap.uid("SEARCH", None, f"UID {last_uid + 1}:*")
    return [int(u) for u in (data[0] or b"").split() if int(u) > last_uid]


def update_correspondents(ctx, imap, name: str, sent_folder: str) -> int:
    """Read To/Cc/Bcc of every message in the Sent folder not read before."""
    typ, _ = _select(imap, sent_folder)
    if typ != "OK":
        ctx.run.warn(f"Could not open the Sent folder {sent_folder!r}; "
                     "mail from people you write to may be misfiled", item=name)
        return 0
    key = f"{name}/sent-scan"
    state, _ = _folder_state(ctx, imap, key)
    uids = _new_uids(imap, state["last_uid"])
    counts: dict[str, list] = {}
    for i in range(0, len(uids), 500):
        chunk = uids[i:i + 500]
        typ, data = imap.uid("FETCH", ",".join(map(str, chunk)), "(UID BODY.PEEK[HEADER.FIELDS (TO CC BCC DATE)])")
        for rec in parse_fetch(data):
            msg = email.message_from_bytes(rec["raw"], policy=POLICY)
            ts = _date(msg, None)
            for hdr in ("To", "Cc", "Bcc"):
                for _, addr in _addresses(msg, hdr):
                    c = counts.setdefault(addr, [0, ts, ts])
                    c[0] += 1
                    if ts and (not c[1] or ts < c[1]):
                        c[1] = ts
                    if ts and (not c[2] or ts > c[2]):
                        c[2] = ts
    with ctx.conn:
        for addr, (n, first, last) in counts.items():
            ctx.conn.execute(
                "INSERT INTO email_correspondents (account, address, times, first_ts, last_ts)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (account, address) DO UPDATE SET"
                " times = times + excluded.times, first_ts = min(coalesce(first_ts, excluded.first_ts),"
                " coalesce(excluded.first_ts, first_ts)), last_ts = max(coalesce(last_ts, ''),"
                " coalesce(excluded.last_ts, ''))", (name, addr, n, first, last))
    if uids:
        state["last_uid"] = max(uids)
        ctx.set_state(key, json.dumps(state))
    return len(counts)


def _gmail_categories(imap, uids: list[int]) -> dict[int, str]:
    """{uid: "CATEGORY_PROMOTIONS", ...} for these UIDs, from Gmail's own tabs."""
    out = {}
    lo, hi = min(uids), max(uids)
    for cat in GMAIL_CATEGORIES:
        typ, data = imap.uid("SEARCH", "X-GM-RAW", f'"category:{cat}"', "UID", f"{lo}:{hi}")
        if typ == "OK" and data and data[0]:
            for u in data[0].split():
                out.setdefault(int(u), f"CATEGORY_{cat.upper()}")
    return out


def sync_account(ctx, acct: dict, max_messages: int) -> int:
    name, host, user = acct["name"], acct["host"], acct["user"]
    gmail = host.endswith("gmail.com") or host.endswith("googlemail.com")
    my = [user.lower()] + [a.lower() for a in acct.get("other_addresses") or []]
    keep = {**DEFAULT_KEEP, **(ctx.settings.get("keep") or {})}
    min_inline = float(ctx.settings.get("min_inline_image_kb") or MIN_INLINE_IMAGE_KB)
    max_mb = float(ctx.settings.get("max_attachment_mb") or MAX_ATTACHMENT_MB)
    password = keys.get_secret(f"email/{name}")
    imap = imaplib.IMAP4_SSL(host, int(acct.get("port") or 993), timeout=120)
    total, skipped = 0, {}
    try:
        imap.login(user, password)
        has_gm = "X-GM-EXT-1" in imap.capabilities
        special = _list_folders(imap)
        all_mail = special.get("\\All") or (GMAIL_ALL_MAIL if gmail else None)
        sent = acct.get("sent_folder") or special.get("\\Sent") or (GMAIL_SENT if gmail else "Sent")
        folders = acct.get("folders") or ([all_mail] if all_mail else ["INBOX", sent])

        update_correspondents(ctx, imap, name, sent)
        correspondents = known_correspondents(ctx.conn, name)

        def save_attachment(data, sha, ctype, fname):
            att_store.store(ctx.conn, ctx.paths, data, sha, ctype, fname)

        for folder in folders:
            if total >= max_messages:
                break
            typ, _ = _select(imap, folder)
            if typ != "OK":
                ctx.run.warn(f"Could not open folder {folder!r}", item=f"{name}/{folder}")
                continue
            key = f"{name}/{folder}"
            state, uidvalidity = _folder_state(ctx, imap, key)
            uids = _new_uids(imap, state["last_uid"])[: max_messages - total]
            items = "(UID INTERNALDATE FLAGS BODY.PEEK[]" + (" X-GM-MSGID X-GM-THRID X-GM-LABELS)" if has_gm else ")")
            for i in range(0, len(uids), BATCH):
                chunk = uids[i:i + BATCH]
                typ, data = imap.uid("FETCH", ",".join(map(str, chunk)), items)
                if typ != "OK":
                    raise RuntimeError(f"FETCH failed in {folder}: {data!r}")
                recs = parse_fetch(data)
                cats = _gmail_categories(imap, chunk) if has_gm and chunk else {}
                lines = []
                for rec in recs:
                    labels = list(rec.get("labels") or [])
                    if rec["uid"] in cats:
                        labels.append(cats[rec["uid"]])
                    raw = rec["raw"]
                    head = email.message_from_bytes(headers_only(raw), policy=POLICY)
                    frm = _addresses(head, "From")
                    from_addr = frm[0][1] if frm else None
                    is_sent = "\\Sent" in labels or (from_addr in my if from_addr else False)
                    category, reason = classify(head, from_address=from_addr, labels=labels,
                                                is_sent=is_sent, correspondents=correspondents)
                    rule = keep.get(category, "full")
                    if rule == "none":
                        skipped[category] = skipped.get(category, 0) + 1
                        continue
                    size = len(raw)
                    if rule == "headers":
                        raw = headers_only(raw)
                    else:
                        try:
                            raw = split_attachments(raw, save_attachment, min_inline_kb=min_inline, max_mb=max_mb)
                        except Exception as e:
                            ctx.run.warn(f"Could not separate attachments, keeping the message whole: {e}",
                                         item=f"{key} uid {rec['uid']}")
                    lines.append(json.dumps({
                        "account": name, "folder": folder, "uidvalidity": uidvalidity, "uid": rec["uid"],
                        "internaldate": rec.get("internaldate"), "flags": rec.get("flags"),
                        "labels": labels, "gm_msgid": rec.get("gm_msgid"), "gm_thrid": rec.get("gm_thrid"),
                        "category": category, "category_reason": reason, "stored": rule, "size": size,
                        "raw_b64": base64.b64encode(raw).decode(),
                    }))
                if lines:
                    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
                    safe = re.sub(r"[^A-Za-z0-9]+", "-", f"{name}-{folder}").strip("-")
                    data_gz = gzip.compress(("\n".join(lines) + "\n").encode(), mtime=0)
                    ctx.save(f"{safe}-{stamp}-{chunk[0]}.jsonl.gz", data_gz,
                             {"account": name, "folder": folder, "my_addresses": my})
                if len(recs) < len(chunk):
                    ctx.run.warn(f"Asked for {len(chunk)} messages, got {len(recs)}", item=key)
                state["last_uid"] = max(chunk)
                ctx.set_state(key, json.dumps(state))
                total += len(recs)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    if skipped:
        ctx.skipped = {k: getattr(ctx, "skipped", {}).get(k, 0) + v for k, v in skipped.items()}
    return total
