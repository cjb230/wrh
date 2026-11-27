import io

from wrh import pipeline
from wrh.sources.calendar import importer as cal

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
X-WR-CALNAME:Personal
BEGIN:VEVENT
UID:standup@google.com
DTSTAMP:{stamp}
DTSTART;TZID=Europe/Warsaw:20261102T093000
DTEND;TZID=Europe/Warsaw:20261102T094500
RRULE:FREQ=WEEKLY;COUNT=3
SUMMARY:Sourdough club call
LOCATION:Zoom
ATTENDEE;CN=Anna Nowak;PARTSTAT=ACCEPTED:mailto:anna@example.com
ORGANIZER:mailto:james@gmail.com
END:VEVENT
BEGIN:VEVENT
UID:standup@google.com
RECURRENCE-ID;TZID=Europe/Warsaw:20261109T093000
DTSTAMP:{stamp}
DTSTART;TZID=Europe/Warsaw:20261109T110000
DTEND;TZID=Europe/Warsaw:20261109T111500
SUMMARY:Sourdough club call (moved)
END:VEVENT
{extra}END:VCALENDAR
"""

DENTIST = """BEGIN:VEVENT
UID:dentist@google.com
DTSTAMP:20261101T000000Z
DTSTART;VALUE=DATE:20261120
DTEND;VALUE=DATE:20261121
SUMMARY:Dentist
END:VEVENT
"""


def ics(tmp_path, extra=DENTIST, stamp="20261101T000000Z", name="cal.ics"):
    p = tmp_path / name
    p.write_text(ICS.format(extra=extra, stamp=stamp).replace("\n", "\r\n"))
    return p


def test_import_with_repeats_and_exceptions(conn, paths, tmp_path):
    r = pipeline.import_file(conn, paths, "calendar", ics(tmp_path), {"calendar": "personal"})
    assert r["rows_read"] == 3 and r["timeline_rows"] == 4  # 3 weekly + 1 all-day
    occ = [tuple(x) for x in conn.execute("SELECT event_id, start FROM calendar_occurrences ORDER BY start")]
    assert occ == [
        ("personal:standup@google.com", "2026-11-02T08:30:00.000000Z"),
        ("personal:standup@google.com:2026-11-09T08:30:00.000000Z", "2026-11-09T10:00:00.000000Z"),
        ("personal:standup@google.com", "2026-11-16T08:30:00.000000Z"),
        ("personal:dentist@google.com", "2026-11-20T00:00:00.000000Z"),
    ]
    ev = conn.execute("SELECT * FROM calendar_events WHERE id = 'personal:standup@google.com'").fetchone()
    assert ev["rrule"] == "FREQ=WEEKLY;COUNT=3" and ev["tzid"] == "Europe/Warsaw"
    assert ev["organizer"] == "james@gmail.com" and '"Anna Nowak"' in ev["attendees"]
    hits = pipeline.search(conn, "anna")
    assert {h["title"] for h in hits} == {"Event: Sourdough club call"}
    assert pipeline.search(conn, "moved")[0]["ts"] == "2026-11-09T10:00:00.000000Z"


def test_removed_event_is_marked_and_leaves_timeline(conn, paths, tmp_path):
    pipeline.import_file(conn, paths, "calendar", ics(tmp_path), {"calendar": "personal"})
    pipeline.import_file(conn, paths, "calendar", ics(tmp_path, extra="", name="b.ics"), {"calendar": "personal"})
    gone = conn.execute("SELECT missing_since FROM calendar_events WHERE uid = 'dentist@google.com'").fetchone()[0]
    assert gone is not None
    assert not pipeline.search(conn, "dentist")
    assert conn.execute("SELECT count(*) FROM timeline WHERE source = 'calendar'").fetchone()[0] == 3
    r = pipeline.reimport(conn, paths, "calendar")
    assert r["timeline_rows"] == 3


def test_sync_skips_unchanged_feed(conn, paths, tmp_path, monkeypatch):
    feed = {"body": ics(tmp_path).read_bytes()}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(cal.urllib.request, "urlopen", lambda req, timeout: Resp(feed["body"]))
    monkeypatch.setenv("WRH_SECRET_CALENDAR_PERSONAL", "https://calendar.google.com/calendar/ical/x/private-y/basic.ics")
    (paths.home / "config.toml").write_text('[calendar]\n[[calendar.feeds]]\nname = "personal"\n')

    assert pipeline.sync_source(conn, paths, "calendar").files == 1
    feed["body"] = ics(tmp_path, stamp="20261126T120000Z").read_bytes()  # only DTSTAMP changed
    r = pipeline.sync_source(conn, paths, "calendar")
    assert (r.files, r.message) == (0, "0 of 1 feed(s) changed")
    feed["body"] = b"<html>Not found</html>"
    r = pipeline.sync_source(conn, paths, "calendar")
    assert r.status == "error"
    assert "not an iCal file" in conn.execute("SELECT message FROM import_problems").fetchone()[0]
