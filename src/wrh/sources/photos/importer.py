"""Photos: metadata for every photo and video in the Mac's Photos library.

With iCloud Photos on, the server's Photos library holds the iPhone's
photos, so sync reads it with `osxphotos <https://github.com/RhetTbull/osxphotos>`_
(macOS only). It stores metadata, not the image files: when and where it
was taken, the place name, people Photos recognised, Photos' own labels
("beach", "dog"), keywords, albums, captions and camera details. The
process running it needs Full Disk Access.

Sync exports photos added or changed since the last run as ``.jsonl.gz``
raw files (5,000 photos each), so the first run on a big library takes a
while but later runs are quick. Every 30 days it exports the whole library
again, because Photos does not always mark a photo as changed when it
recognises a face or names a place.

Tables:

* ``photos_photos``  - one row per photo or video.
* ``photos_people``  - one row per (photo, person), for "every photo of X".
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from ...timeutil import to_iso
from ..base import Source

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos_photos (
    id TEXT PRIMARY KEY,           -- Photos' UUID
    ts TEXT NOT NULL,              -- when it was taken, UTC
    local_time TEXT,               -- when it was taken, local time where it was taken
    added_at TEXT,
    modified_at TEXT,
    filename TEXT,                 -- original file name
    kind TEXT,                     -- photo, video
    title TEXT,
    description TEXT,
    latitude REAL,
    longitude REAL,
    place TEXT,                    -- Photos' place name, e.g. "Old Town, Warsaw"
    address TEXT,
    country TEXT,
    people TEXT,                   -- JSON list of names
    labels TEXT,                   -- JSON list of Photos' search labels
    keywords TEXT,                 -- JSON list
    albums TEXT,                   -- JSON list
    favorite INTEGER,
    hidden INTEGER,
    screenshot INTEGER,
    selfie INTEGER,
    live_photo INTEGER,
    width INTEGER,
    height INTEGER,
    duration_s REAL,
    camera TEXT,
    lens TEXT,
    in_cloud_only INTEGER,         -- 1 if the original is not downloaded to this Mac
    path TEXT,                     -- original file on this Mac, if downloaded
    raw_file_id INTEGER REFERENCES raw_files(id)
);
CREATE INDEX IF NOT EXISTS photos_photos_ts ON photos_photos(ts);
CREATE INDEX IF NOT EXISTS photos_photos_raw ON photos_photos(raw_file_id);

CREATE TABLE IF NOT EXISTS photos_people (
    photo_id TEXT NOT NULL REFERENCES photos_photos(id),
    person TEXT NOT NULL,
    PRIMARY KEY (photo_id, person)
);
CREATE INDEX IF NOT EXISTS photos_people_person ON photos_people(person);
"""

CHUNK = 5000
FULL_REFRESH_DAYS = 30


def _iso(t) -> str | None:
    if not isinstance(t, dt.datetime):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return to_iso(t)


def _get(obj, name, default=None):
    try:
        v = getattr(obj, name)
        return v() if callable(v) else v
    except Exception:
        return default


def photo_record(p) -> dict:
    """The fields we keep from an osxphotos PhotoInfo. Every field is optional."""
    place = _get(p, "place")
    exif = _get(p, "exif_info")
    lat, lon = (_get(p, "location") or (None, None))[:2]
    date = _get(p, "date")
    return {
        "uuid": p.uuid,
        "date": _iso(date),
        "local_time": date.replace(tzinfo=None).isoformat() if isinstance(date, dt.datetime) else None,
        "date_added": _iso(_get(p, "date_added")),
        "date_modified": _iso(_get(p, "date_modified")),
        "filename": _get(p, "original_filename"),
        "kind": "video" if _get(p, "ismovie") else "photo",
        "title": _get(p, "title"),
        "description": _get(p, "description"),
        "latitude": lat,
        "longitude": lon,
        "place": _get(place, "name") if place else None,
        "address": _get(place, "address_str") if place else None,
        "country": _get(place, "country_code") if place else None,
        "people": [x for x in (_get(p, "persons") or []) if x and x != "_UNKNOWN_"],
        "labels": list(_get(p, "labels") or []),
        "keywords": list(_get(p, "keywords") or []),
        "albums": list(_get(p, "albums") or []),
        "favorite": bool(_get(p, "favorite")),
        "hidden": bool(_get(p, "hidden")),
        "screenshot": bool(_get(p, "screenshot")),
        "selfie": bool(_get(p, "selfie")),
        "live_photo": bool(_get(p, "live_photo")),
        "width": _get(p, "width"),
        "height": _get(p, "height"),
        "duration": _get(exif, "duration") if exif else None,
        "camera": " ".join(x for x in (_get(exif, "camera_make"), _get(exif, "camera_model")) if x) or None
        if exif else None,
        "lens": _get(exif, "lens_model") if exif else None,
        "in_cloud_only": bool(_get(p, "ismissing")),
        "path": _get(p, "path"),
    }


def read_batch(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


COLS = ["id", "ts", "local_time", "added_at", "modified_at", "filename", "kind", "title", "description",
        "latitude", "longitude", "place", "address", "country", "people", "labels", "keywords", "albums",
        "favorite", "hidden", "screenshot", "selfie", "live_photo", "width", "height", "duration_s",
        "camera", "lens", "in_cloud_only", "path", "raw_file_id"]


def _list(v) -> str | None:
    return json.dumps(v) if v else None


class Photos(Source):
    name = "photos"
    timeline_table = "photos_photos"
    schema = SCHEMA
    can_sync = True

    def reset(self, conn) -> None:
        conn.execute("DELETE FROM photos_people")
        conn.execute("DELETE FROM photos_photos")

    def import_file(self, conn, path: Path, meta: Mapping, raw_file_id: int) -> int:
        n = 0
        for r in read_batch(Path(path)):
            row = {
                "id": r["uuid"], "ts": r["date"] or r["date_added"] or "1970-01-01T00:00:00.000000Z",
                "local_time": r.get("local_time"), "added_at": r.get("date_added"),
                "modified_at": r.get("date_modified"), "filename": r.get("filename"), "kind": r.get("kind"),
                "title": r.get("title"), "description": r.get("description"),
                "latitude": r.get("latitude"), "longitude": r.get("longitude"), "place": r.get("place"),
                "address": r.get("address"), "country": r.get("country"),
                "people": _list(r.get("people")), "labels": _list(r.get("labels")),
                "keywords": _list(r.get("keywords")), "albums": _list(r.get("albums")),
                "favorite": int(bool(r.get("favorite"))), "hidden": int(bool(r.get("hidden"))),
                "screenshot": int(bool(r.get("screenshot"))), "selfie": int(bool(r.get("selfie"))),
                "live_photo": int(bool(r.get("live_photo"))), "width": r.get("width"),
                "height": r.get("height"), "duration_s": r.get("duration"), "camera": r.get("camera"),
                "lens": r.get("lens"), "in_cloud_only": int(bool(r.get("in_cloud_only"))),
                "path": r.get("path"), "raw_file_id": raw_file_id,
            }
            conn.execute(
                f"INSERT INTO photos_photos ({', '.join(COLS)}) VALUES ({', '.join(':' + c for c in COLS)})"
                " ON CONFLICT (id) DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in COLS if c != "id"),
                row,
            )
            conn.execute("DELETE FROM photos_people WHERE photo_id = ?", (row["id"],))
            conn.executemany("INSERT OR IGNORE INTO photos_people (photo_id, person) VALUES (?, ?)",
                             [(row["id"], person) for person in r.get("people") or []])
            n += 1
        return n

    def timeline_rows(self, conn, raw_file_id: int | None) -> Iterable[Mapping]:
        sql = """
        SELECT id, ts, kind, title, description, place, address, people, labels, keywords, albums,
               filename, path, screenshot
        FROM photos_photos WHERE (:raw IS NULL OR raw_file_id = :raw)
        """
        for r in conn.execute(sql, {"raw": raw_file_id}):
            what = "Screenshot" if r["screenshot"] else ("Video" if r["kind"] == "video" else "Photo")
            people = json.loads(r["people"] or "[]")
            title = f"{what}: {r['title']}" if r["title"] else (
                f"{what} at {r['place']}" if r["place"] else f"{what} {r['filename'] or ''}".strip())
            if people:
                title += " with " + ", ".join(people[:5])
            body = "\n".join(x for x in [
                r["description"],
                r["address"],
                "People: " + ", ".join(people) if people else None,
                "Labels: " + ", ".join(json.loads(r["labels"] or "[]")) if r["labels"] else None,
                "Keywords: " + ", ".join(json.loads(r["keywords"] or "[]")) if r["keywords"] else None,
                "Albums: " + ", ".join(json.loads(r["albums"] or "[]")) if r["albums"] else None,
                f"{r['filename']} ({r['id']})",
            ] if x)
            yield {
                "source_key": r["id"],
                "ts": r["ts"],
                "title": title,
                "body": body,
                "url": f"file://{r['path']}" if r["path"] else None,
            }

    def sync(self, ctx) -> None:
        try:
            import osxphotos
        except ImportError as e:
            raise RuntimeError("osxphotos is not installed (it only installs on macOS)") from e
        library = ctx.settings.get("library")
        since = ctx.get_state("last_changed")
        # Photos does not always bump date_modified when it learns a face or a
        # place, so every 30 days export everything again.
        last_full = ctx.get_state("last_full")
        full = not last_full or dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(
            last_full.replace("Z", "+00:00")) > dt.timedelta(days=FULL_REFRESH_DAYS)
        if full:
            since = None
        db = osxphotos.PhotosDB(dbfile=library) if library else osxphotos.PhotosDB()
        changed, newest, failed = [], since, 0
        for p in db.photos(intrash=False):
            try:
                rec = photo_record(p)
            except Exception as e:
                failed += 1
                if failed <= 20:
                    ctx.run.warn(f"Could not read photo metadata: {e}", item=getattr(p, "uuid", None))
                continue
            stamp = max(x for x in (rec["date_added"], rec["date_modified"], "") if x is not None)
            if since and stamp and stamp <= since:
                continue
            changed.append(rec)
            if stamp and (newest is None or stamp > newest):
                newest = stamp
        if failed > 20:
            ctx.run.warn(f"{failed} photos in total could not be read")
        changed.sort(key=lambda r: r["date_added"] or "")
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        for i in range(0, len(changed), CHUNK):
            part = changed[i:i + CHUNK]
            data = gzip.compress(("\n".join(json.dumps(r) for r in part) + "\n").encode(), mtime=0)
            ctx.save(f"photos-{now}-{i // CHUNK:04d}.jsonl.gz", data, {"library": library or "default"})
        if newest:
            ctx.set_state("last_changed", newest)
        if full:
            ctx.set_state("last_full", to_iso(dt.datetime.now(dt.timezone.utc)))
        ctx.run.message = f"{len(changed)} new or changed photo(s)"
