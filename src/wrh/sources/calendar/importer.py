"""Calendars: Google Calendar (or any calendar) through its iCal (.ics) feed.

Sync downloads each feed in config.toml. For Google Calendar the feed is the
calendar's "Secret address in iCal format" (Google Calendar > Settings >
the calendar > Integrate calendar), stored with
``wrh secret set calendar/<name>``. It needs no Google Cloud project and
no sign-in that can expire. ``wrh import calendar file.ics`` imports a
downloaded .ics too (for example from Google Takeout).

A feed is archived only when its events changed (Google stamps every
download with the current time, so the comparison ignores DTSTAMP lines).

Tables:

* ``calendar_events``      - one row per event as written in the feed,
  including its repeat rule. ``missing_since`` is set when an event
  disappears from the latest copy of its feed (deleted or moved).
* ``calendar_occurrences`` - one row per occurrence: a repeating event
  becomes one row per date, up to a year ahead. These feed the timeline.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Iterable, Mapping

from ... import keys
from ...timeutil import now_iso, to_iso
from ..base import Source

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_events (
    id TEXT PRIMARY KEY,           -- calendar:UID, plus :RECURRENCE-ID for a changed occurrence
    calendar TEXT NOT NULL,
    uid TEXT NOT NULL,
    recurrence_id TEXT,
    summary TEXT,
    description TEXT,
    location TEXT,
    start TEXT,                    -- UTC; all-day events: midnight UTC of the date
    end TEXT,
    all_day INTEGER,
    tzid TEXT,                     -- the event's own time zone
    rrule TEXT,                    -- repeat rule, if any
    status TEXT,                   -- CONFIRMED, TENTATIVE, CANCELLED
    organizer TEXT,
    attendees TEXT,                -- JSON list of {name, email, status}
    url TEXT,
    created TEXT,
    last_modified TEXT,
    sequence INTEGER,
    missing_since TEXT,            -- set when gone from the latest feed
    raw_file_id INTEGER REFERENCES raw_files(id)
);
CREATE INDEX IF NOT EXISTS calendar_events_start ON calendar_events(start);

CREATE TABLE IF NOT EXISTS calendar_occurrences (
    id TEXT PRIMARY KEY,           -- event id + occurrence start
    event_id TEXT NOT NULL REFERENCES calendar_events(id),
    calendar TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT,
    all_day INTEGER,
    raw_file_id INTEGER REFERENCES raw_files(id)
);
CREATE INDEX IF NOT EXISTS calendar_occurrences_start ON calendar_occurrences(start);
CREATE INDEX IF NOT EXISTS calendar_occurrences_event ON calendar_occurrences(event_id);
CREATE INDEX IF NOT EXISTS calendar_occurrences_raw ON calendar_occurrences(raw_file_id);
"""

EXPAND_FROM = dt.datetime(1990, 1, 1, tzinfo=dt.timezone.utc)
EXPAND_AHEAD = dt.timedelta(days=366)


def _utc(v) -> tuple[str | None, bool]:
    """(UTC ISO string, all_day) for an icalendar date or datetime."""
    if v is None:
        return None, False
    if isinstance(v, dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=dt.timezone.utc)  # "floating" time: treated as UTC
        return to_iso(v), False
    if isinstance(v, dt.date):
        return to_iso(dt.datetime(v.year, v.month, v.day, tzinfo=dt.timezone.utc)), True
    return None, False


def _prop(ev, name):
    v = ev.get(name)
    return getattr(v, "dt", v)


def _text(ev, name) -> str | None:
    v = ev.get(name)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _attendees(ev) -> list[dict]:
    raw = ev.get("ATTENDEE")
    if raw is None:
        return []
    out = []
    for a in raw if isinstance(raw, list) else [raw]:
        params = getattr(a, "params", {})
        mail = str(a)
        if mail.lower().startswith("mailto:"):
            mail = mail[7:]
        out.append({"name": params.get("CN"), "email": mail.lower() or None, "status": params.get("PARTSTAT")})
    return out


def normalised_hash(data: bytes) -> str:
    """SHA-256 of the feed without the lines that change on every download."""
    lines = [ln for ln in data.splitlines() if not ln.upper().startswith(b"DTSTAMP")]
    return hashlib.sha256(b"\n".join(lines)).hexdigest()


class Calendar(Source):
    name = "calendar"
    timeline_table = "calendar_occurrences"
    schema = SCHEMA
    can_sync = True

    def reset(self, conn) -> None:
        conn.execute("DELETE FROM calendar_occurrences")
        conn.execute("DELETE FROM calendar_events")

    def import_file(self, conn, path: Path, meta: Mapping, raw_file_id: int) -> int:
        import icalendar
        import recurring_ical_events

        cal = icalendar.Calendar.from_ical(Path(path).read_bytes())
        name = meta.get("calendar") or str(cal.get("X-WR-CALNAME") or Path(path).stem)
        seen, n = set(), 0
        for ev in cal.walk("VEVENT"):
            uid = _text(ev, "UID") or hashlib.sha1(ev.to_ical()).hexdigest()
            rid, _ = _utc(_prop(ev, "RECURRENCE-ID"))
            eid = f"{name}:{uid}" + (f":{rid}" if rid else "")
            start_v, end_v = _prop(ev, "DTSTART"), _prop(ev, "DTEND")
            start, all_day = _utc(start_v)
            end, _ = _utc(end_v)
            if end is None and start_v is not None and ev.get("DURATION") is not None:
                end, _ = _utc(start_v + ev["DURATION"].dt)
            org = ev.get("ORGANIZER")
            row = {
                "id": eid, "calendar": name, "uid": uid, "recurrence_id": rid,
                "summary": _text(ev, "SUMMARY"), "description": _text(ev, "DESCRIPTION"),
                "location": _text(ev, "LOCATION"), "start": start, "end": end, "all_day": int(all_day),
                "tzid": str(start_v.tzinfo) if isinstance(start_v, dt.datetime) and start_v.tzinfo else None,
                "rrule": ev["RRULE"].to_ical().decode() if ev.get("RRULE") else None,
                "status": _text(ev, "STATUS"),
                "organizer": (str(org)[7:] if str(org).lower().startswith("mailto:") else str(org)) if org else None,
                "attendees": json.dumps(_attendees(ev)) or None,
                "url": _text(ev, "URL"),
                "created": _utc(_prop(ev, "CREATED"))[0],
                "last_modified": _utc(_prop(ev, "LAST-MODIFIED"))[0],
                "sequence": int(ev.get("SEQUENCE", 0) or 0),
                "raw_file_id": raw_file_id,
            }
            cols = list(row)
            conn.execute(
                f"INSERT INTO calendar_events ({', '.join(cols)}, missing_since)"
                f" VALUES ({', '.join(':' + c for c in cols)}, NULL)"
                " ON CONFLICT (id) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id") + ", missing_since = NULL",
                row,
            )
            seen.add(eid)
            n += 1

        # Occurrences, with repeats, exceptions and changed instances applied.
        horizon = dt.datetime.now(dt.timezone.utc) + EXPAND_AHEAD
        # Rebuilt from scratch for this calendar, timeline rows included.
        conn.execute(
            "DELETE FROM timeline WHERE source_table = 'calendar_occurrences' AND source_key IN"
            " (SELECT id FROM calendar_occurrences WHERE calendar = ?)", (name,))
        conn.execute("DELETE FROM calendar_occurrences WHERE calendar = ?", (name,))
        for occ in recurring_ical_events.of(cal).between(EXPAND_FROM, horizon):
            uid = _text(occ, "UID") or hashlib.sha1(occ.to_ical()).hexdigest()
            rid, _ = _utc(_prop(occ, "RECURRENCE-ID"))
            changed = f"{name}:{uid}:{rid}" if rid else None
            eid = changed if changed in seen else f"{name}:{uid}"
            start, all_day = _utc(_prop(occ, "DTSTART"))
            end, _ = _utc(_prop(occ, "DTEND"))
            if start is None or eid not in seen:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO calendar_occurrences (id, event_id, calendar, start, end, all_day, raw_file_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"{eid}@{start}", eid, name, start, end, int(all_day), raw_file_id),
            )

        # Events no longer in this feed.
        if seen:
            conn.execute(
                f"UPDATE calendar_events SET missing_since = ? WHERE calendar = ? AND missing_since IS NULL"
                f" AND id NOT IN ({', '.join('?' * len(seen))})",
                (now_iso(), name, *seen),
            )
        return n

    def timeline_rows(self, conn, raw_file_id: int | None) -> Iterable[Mapping]:
        sql = """
        SELECT o.id, o.start, o.calendar, e.summary, e.location, e.description, e.attendees,
               e.status, e.url, e.all_day
        FROM calendar_occurrences o JOIN calendar_events e ON e.id = o.event_id
        WHERE (:raw IS NULL OR o.raw_file_id = :raw)
        """
        for r in conn.execute(sql, {"raw": raw_file_id}):
            if (r["status"] or "").upper() == "CANCELLED":
                continue
            people = [a.get("name") or a.get("email") for a in json.loads(r["attendees"] or "[]")]
            body = "\n".join(x for x in [
                r["location"] and f"Where: {r['location']}",
                people and "With: " + ", ".join(p for p in people if p),
                r["description"],
            ] if x)
            yield {
                "source_key": r["id"],
                "ts": r["start"],
                "title": f"Event: {r['summary'] or '(no title)'}",
                "body": body or None,
                "url": r["url"],
            }

    def sync(self, ctx) -> None:
        feeds = ctx.settings.get("feeds") or []
        if not feeds:
            ctx.run.warn("No [[calendar.feeds]] in config.toml")
        changed = 0
        for feed in feeds:
            name = feed["name"]
            try:
                url = feed.get("url") or keys.get_secret(f"calendar/{name}")
                req = urllib.request.Request(url, headers={"User-Agent": "wrh-calendar-sync"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                if b"BEGIN:VCALENDAR" not in data[:2000]:
                    raise ValueError("the download is not an iCal file (wrong or revoked secret address?)")
                digest = normalised_hash(data)
                if ctx.get_state(f"{name}/hash") == digest:
                    continue
                stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
                ctx.save(f"{name}-{stamp}.ics", data, {"calendar": name})
                ctx.set_state(f"{name}/hash", digest)
                changed += 1
            except Exception as e:
                ctx.run.exception(f"Feed {name}: {e}", item=name)
        ctx.run.message = f"{changed} of {len(feeds)} feed(s) changed"
