import base64
import gzip
import json
import mailbox
from email.message import EmailMessage

import pytest

from wrh import pipeline, raw
from wrh.sources.email import importer as em


def make_msg(subject="Sourdough club", frm="Anna Nowak <anna@example.com>", to="james@gmail.com",
             body="Bring your starter on Saturday.", msgid="<abc@example.com>", html=False, attach=False):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = frm, to, subject, msgid
    m["Date"] = "Sun, 01 Nov 2026 12:30:00 +0100"
    m["Cc"] = "Bob <bob@example.org>"
    if html:
        m.set_content("<html><body><p>Hello <b>there</b></p><style>x{}</style></body></html>", subtype="html")
    else:
        m.set_content(body)
    if attach:
        m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="recipe.pdf")
    return m.as_bytes()


def batch(tmp_path, records, name="b.jsonl.gz"):
    p = tmp_path / name
    p.write_bytes(gzip.compress("\n".join(json.dumps(r) for r in records).encode()))
    return p


def rec(raw, **kw):
    return {"account": "gmail", "folder": em.GMAIL_ALL_MAIL, "uid": 1, "uidvalidity": "1",
            "internaldate": "2026-11-01T11:30:05.000000Z", "flags": ["\\Seen"],
            "raw_b64": base64.b64encode(raw).decode(), **kw}


def test_batch_import(conn, paths, tmp_path):
    f = batch(tmp_path, [
        rec(make_msg(attach=True), gm_msgid="1789000000000000001", gm_thrid="1789", labels=["\\Inbox", "Baking"]),
        rec(make_msg("Re: club", frm="James <james@gmail.com>", to="anna@example.com", msgid="<def@x>",
                     body="See you then."),
            gm_msgid="1789000000000000002", gm_thrid="1789", labels=["\\Sent"]),
        rec(make_msg("Win a prize", msgid="<spam@x>"), gm_msgid="3", labels=["\\Spam"]),
    ])
    r = pipeline.import_file(conn, paths, "email", f, {"account": "gmail", "my_addresses": ["james@gmail.com"]})
    assert (r["rows_read"], r["timeline_rows"]) == (2, 2)  # spam is skipped

    m = conn.execute("SELECT * FROM email_messages WHERE gm_msgid = '1789000000000000001'").fetchone()
    assert m["ts"] == "2026-11-01T11:30:00.000000Z" and m["from_address"] == "anna@example.com"
    assert m["body"] == "Bring your starter on Saturday." and json.loads(m["labels"]) == ["\\Inbox", "Baking"]
    assert json.loads(m["attachments"])[0]["name"] == "recipe.pdf"
    parts = conn.execute("SELECT role, address FROM email_participants WHERE message_id = ? ORDER BY role",
                         (m["id"],)).fetchall()
    assert [tuple(p) for p in parts] == [("cc", "bob@example.org"), ("from", "anna@example.com"),
                                         ("to", "james@gmail.com")]

    hit = pipeline.search(conn, "starter")[0]
    assert hit["title"] == "Email from Anna Nowak: Sourdough club"
    assert hit["url"] == "https://mail.google.com/mail/u/0/#all/" + format(1789000000000000001, "x")
    sent = pipeline.search(conn, '"Re club"')[0]
    assert sent["title"] == "Email to anna@example.com: Re: club"
    assert pipeline.search(conn, "recipe")  # attachment names are searchable


def test_html_only_body():
    m = em.parse_message(make_msg(html=True), account="x")
    assert m["body"] == "Hello there"


def test_same_message_in_two_folders_merges(conn, paths, tmp_path):
    raw = make_msg()
    a = batch(tmp_path, [rec(raw, account="work", folder="INBOX")], "a.jsonl.gz")
    b = batch(tmp_path, [rec(raw, account="work", folder="Archive", uid=9)], "b.jsonl.gz")
    pipeline.import_file(conn, paths, "email", a, {})
    pipeline.import_file(conn, paths, "email", b, {})
    rows = conn.execute("SELECT id, folders FROM email_messages").fetchall()
    assert len(rows) == 1 and rows[0]["id"] == "work:<abc@example.com>"
    assert sorted(json.loads(rows[0]["folders"])) == ["Archive", "INBOX"]
    assert pipeline.search(conn, "starter")[0]["url"] == "message://%3Cabc@example.com%3E"


def test_mbox_import(conn, paths, tmp_path):
    p = tmp_path / "All mail.mbox"
    box = mailbox.mbox(str(p))
    raw = make_msg().replace(b"Subject:", b"X-Gmail-Labels: Inbox,Baking\r\nX-GM-THRID: 42\r\nSubject:", 1)
    box.add(raw)
    box.add(make_msg("Second", msgid="<2@x>"))
    box.flush()
    box.close()
    r = pipeline.import_file(conn, paths, "email", p, {})
    assert r["rows_read"] == 2
    m = conn.execute("SELECT * FROM email_messages WHERE message_id = '<abc@example.com>'").fetchone()
    assert m["account"] == "takeout" and json.loads(m["labels"]) == ["Inbox", "Baking"] and m["gm_thrid"] == "42"


def test_parse_fetch():
    raw = make_msg()
    data = [
        (b'1 (X-GM-THRID 77 X-GM-MSGID 88 X-GM-LABELS ("\\\\Inbox" "My label") UID 5 '
         b'INTERNALDATE "01-Nov-2026 12:30:05 +0100" FLAGS (\\Seen) BODY[] {%d}' % len(raw), raw),
        b")",
        (b"2 (UID 6 BODY[] {3}", b"abc"),
        b' FLAGS (\\Flagged))',
    ]
    a, b = em.parse_fetch(data)
    assert a["uid"] == 5 and a["gm_msgid"] == "88" and a["gm_thrid"] == "77"
    assert a["labels"] == ["\\Inbox", "My label"] and a["flags"] == ["\\Seen"]
    assert a["internaldate"] == "2026-11-01T11:30:05.000000Z"
    assert b["uid"] == 6 and b["flags"] == ["\\Flagged"] and b["raw"] == b"abc"


def bulk_msg(subject="50% off sourdough kits", msgid="<promo@shop>", frm="Shop <news@shop.example>"):
    raw = make_msg(subject, frm=frm, msgid=msgid, body="Huge sale!")
    return raw.replace(b"Subject:", b"List-Unsubscribe: <mailto:u@shop.example>\r\nX-Mailchimp-Campaign: 1\r\nSubject:", 1)


class FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL for sync_account, Gmail flavour."""
    capabilities = ("IMAP4REV1", "X-GM-EXT-1")
    folders = {}      # name -> {uid: raw}
    promotions = set()

    def __init__(self, host, port, timeout=None):
        self.current = None

    def login(self, user, password):
        assert password == "app-password"

    def list(self):
        return "OK", [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
                      b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Wys&AUI-ane"',
                      b'(\\HasNoChildren) "/" "INBOX"']

    def select(self, folder, readonly=False):
        assert readonly
        name = em._decode_mutf7(folder.strip('"').encode())
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.current = self.folders[name]
        return "OK", [str(len(self.current)).encode()]

    def response(self, name):
        return name, [b"555"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            if args[0] == "X-GM-RAW":
                lo, hi = (int(x) for x in args[3].split(":"))
                hits = [u for u in self.current if lo <= u <= hi and "promotions" in args[1] and u in self.promotions]
                return "OK", [" ".join(map(str, hits)).encode()]
            lo = int(args[1].split()[1].split(":")[0])
            return "OK", [" ".join(str(u) for u in sorted(self.current) if u >= lo).encode()]
        uids = [int(u) for u in args[0].split(",")]
        headers_only = "HEADER.FIELDS" in args[1]
        out = []
        for u in uids:
            raw = self.current[u]
            if headers_only:
                raw = raw.split(b"\n\n")[0] + b"\n\n"
            out.append((b"%d (X-GM-MSGID %d X-GM-THRID 1 X-GM-LABELS (\\Inbox) UID %d FLAGS () "
                        b'INTERNALDATE "01-Nov-2026 12:30:05 +0000" BODY[] {%d}' % (u, 1000 + u, u, len(raw)), raw))
            out.append(b")")
        return "OK", out

    def logout(self):
        pass


@pytest.fixture
def imap(paths, monkeypatch):
    monkeypatch.setenv("WRH_SECRET_EMAIL_GMAIL", "app-password")
    monkeypatch.setattr(em.imaplib, "IMAP4_SSL", FakeIMAP)
    (paths.home / "config.toml").write_text(
        '[email]\n[[email.accounts]]\nname = "gmail"\nhost = "imap.gmail.com"\nuser = "james@gmail.com"\n')
    FakeIMAP.promotions = set()
    FakeIMAP.folders = {"[Gmail]/All Mail": {}, "[Gmail]/Wysłane": {}}
    return FakeIMAP


def test_sync_is_incremental(conn, paths, imap):
    imap.folders["[Gmail]/All Mail"] = {1: make_msg(msgid="<1@x>"), 2: make_msg("Two", msgid="<2@x>")}
    r = pipeline.sync_source(conn, paths, "email")
    assert (r.status, r.rows_read) == ("ok", 2)
    imap.folders["[Gmail]/All Mail"][3] = make_msg("Three", msgid="<3@x>")
    r = pipeline.sync_source(conn, paths, "email")
    assert r.rows_read == 1  # only the new one
    assert conn.execute("SELECT count(*) FROM email_messages").fetchone()[0] == 3
    state = conn.execute("SELECT value FROM sync_state WHERE key = 'gmail/[Gmail]/All Mail'").fetchone()[0]
    assert json.loads(state) == {"uidvalidity": "555", "last_uid": 3}


def test_bulk_is_dropped_but_correspondents_are_kept(conn, paths, imap):
    # You once wrote to news@club.example, so its newsletter counts as personal.
    sent = make_msg("Hi", frm="James <james@gmail.com>", to="Club <news@club.example>", msgid="<s@x>")
    imap.folders["[Gmail]/Wysłane"] = {1: sent}
    imap.folders["[Gmail]/All Mail"] = {
        1: make_msg(msgid="<friend@x>"),                                    # a stranger writing: personal
        2: bulk_msg(),                                                      # marketing: dropped
        3: bulk_msg("Club news", "<club@x>", "Club <news@club.example>"),   # bulk-looking, but a correspondent
        4: make_msg("Weekly deals", msgid="<p@x>", frm="Deals <deals@x.example>"),  # Gmail Promotions tab
        5: make_msg("Your receipt", msgid="<r@x>", frm="no-reply@shop.example"),     # automated: kept
    }
    imap.promotions = {4}
    r = pipeline.sync_source(conn, paths, "email")
    assert r.message == "3 message(s) kept, skipped 2 bulk"
    cats = dict(conn.execute("SELECT message_id, category FROM email_messages").fetchall())
    assert cats == {"<friend@x>": "personal", "<club@x>": "personal", "<r@x>": "automated"}
    reason = conn.execute("SELECT category_reason FROM email_messages WHERE message_id = '<club@x>'").fetchone()[0]
    assert reason == "you have written to this sender"
    assert conn.execute("SELECT times FROM email_correspondents WHERE address = 'news@club.example'").fetchone()[0] == 1


def test_attachments_stored_once_outside_messages(conn, paths, imap):
    a = make_msg(attach=True, msgid="<a@x>")
    b = make_msg("Fwd: recipe", attach=True, msgid="<b@x>")
    imap.folders["[Gmail]/All Mail"] = {1: a, 2: b}
    pipeline.sync_source(conn, paths, "email")
    files = conn.execute("SELECT * FROM email_attachment_files").fetchall()
    assert len(files) == 1 and files[0]["name"] == "recipe.pdf" and files[0]["size"] == len(b"%PDF-1.4 fake")
    stored = (paths.raw / files[0]["path"]).read_bytes()
    assert b"PDF" not in stored  # encrypted
    for (rel,) in conn.execute("SELECT path FROM raw_files WHERE source = 'email'"):
        with raw.plaintext(paths, rel) as p:
            assert base64.b64encode(b"%PDF-1.4 fake") not in gzip.decompress(p.read_bytes())
    att = json.loads(conn.execute("SELECT attachments FROM email_messages WHERE message_id = '<a@x>'").fetchone()[0])
    assert att == [{"name": "recipe.pdf", "type": "application/pdf", "size": 13, "sha256": files[0]["sha256"],
                    "skipped": None}]
    with em.att_store.plaintext(conn, paths, files[0]["sha256"]) as (p, name):
        assert p.read_bytes() == b"%PDF-1.4 fake" and name == "recipe.pdf"
    # Reimport keeps the link to the stored file.
    pipeline.reimport(conn, paths, "email")
    att2 = json.loads(conn.execute("SELECT attachments FROM email_messages WHERE message_id = '<b@x>'").fetchone()[0])
    assert att2[0]["sha256"] == files[0]["sha256"]
    assert pipeline.search(conn, "recipe")


def test_small_inline_images_are_skipped():
    m = EmailMessage()
    m["From"], m["Subject"] = "a@x", "logo"
    m.set_content("hi")
    m.add_related(b"\x89PNG" + b"0" * 100, maintype="image", subtype="png", cid="<logo>", disposition="inline")
    saved = []
    out = em.split_attachments(m.as_bytes(), lambda *a: saved.append(a))
    assert saved == [] and b"X-WRH-Attachment-Skipped: small inline image" in out


def test_classify_rules():
    from email import message_from_bytes
    from wrh.sources.email.classify import classify

    def c(extra=b"", frm="a@b.example", **kw):
        msg = message_from_bytes(extra + b"From: " + frm.encode() + b"\r\nSubject: x\r\n\r\n")
        return classify(msg, from_address=frm, **kw)[0]

    assert c() == "personal"
    assert c(b"Precedence: bulk\r\n") == "bulk"
    assert c(b"List-Unsubscribe: <x>\r\n") == "bulk"
    assert c(b"List-Unsubscribe: <x>\r\nList-Id: <cooks.lists.example>\r\n") == "list"
    assert c(b"Auto-Submitted: auto-generated\r\n") == "automated"
    assert c(frm="notifications@github.com") == "automated"
    assert c(b"X-Spam-Flag: YES\r\n") == "spam"
    assert c(labels=["CATEGORY_PROMOTIONS"]) == "bulk"
    assert c(b"Precedence: bulk\r\n", correspondents={"a@b.example"}) == "personal"
    assert c(b"Precedence: bulk\r\n", is_sent=True) == "personal"


def test_mbox_skips_bulk_and_uses_sent_mail(conn, paths, tmp_path):
    p = tmp_path / "All mail.mbox"
    box = mailbox.mbox(str(p))
    box.add(make_msg("Hi", frm="James <james@gmail.com>", to="news@club.example", msgid="<s@x>")
            .replace(b"Subject:", b"X-Gmail-Labels: Sent\r\nSubject:", 1))
    box.add(bulk_msg("Club news", "<club@x>", "Club <news@club.example>"))
    box.add(bulk_msg())
    box.add(make_msg("Junk", msgid="<j@x>").replace(b"Subject:", b"X-Gmail-Labels: Spam\r\nSubject:", 1))
    box.flush()
    box.close()
    r = pipeline.import_file(conn, paths, "email", p, {"my_addresses": ["james@gmail.com"]})
    assert r["rows_read"] == 2
    assert {x[0] for x in conn.execute("SELECT message_id FROM email_messages")} == {"<s@x>", "<club@x>"}


def test_sync_missing_password_is_logged(conn, paths, monkeypatch):
    monkeypatch.setattr(em.keys, "_keyring", lambda: (_ for _ in ()).throw(RuntimeError("no keyring")))
    (paths.home / "config.toml").write_text(
        '[email]\n[[email.accounts]]\nname = "work"\nhost = "imap.example.com"\nuser = "j@example.com"\n')
    r = pipeline.sync_source(conn, paths, "email")
    assert r.status == "error"
    p = conn.execute("SELECT item, message FROM import_problems").fetchone()
    assert p["item"] == "work" and "Could not read secret" in p["message"]
