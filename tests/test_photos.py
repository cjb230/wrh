import datetime as dt
import sys
import types

from wrh import pipeline

WAW = dt.timezone(dt.timedelta(hours=1))


class Place:
    name = "Old Town, Warsaw"
    address_str = "Rynek Starego Miasta, Warsaw, Poland"
    country_code = "PL"


class Exif:
    camera_make, camera_model, lens_model, duration = "Apple", "iPhone 16", "back camera", None


class Photo:
    def __init__(self, uuid, added, **kw):
        self.uuid = uuid
        self.date = dt.datetime(2026, 11, 1, 13, 30, tzinfo=WAW)
        self.date_added = added
        self.date_modified = None
        self.original_filename = f"IMG_{uuid}.HEIC"
        self.ismovie = False
        self.title = None
        self.description = None
        self.location = (52.2497, 21.0122)
        self.place = Place()
        self.persons = ["Anna Nowak", "_UNKNOWN_"]
        self.labels = ["Bread", "Food"]
        self.keywords = []
        self.albums = ["Baking"]
        self.favorite = True
        self.hidden = self.screenshot = self.selfie = self.live_photo = False
        self.width, self.height = 4032, 3024
        self.exif_info = Exif()
        self.ismissing = True
        self.path = None
        self.__dict__.update(kw)


class Broken(Photo):
    @property
    def place(self):
        raise RuntimeError("bad place")

    @place.setter
    def place(self, v):
        pass


def fake_osxphotos(monkeypatch, photos):
    mod = types.ModuleType("osxphotos")

    class PhotosDB:
        def __init__(self, dbfile=None):
            pass

        def photos(self, intrash=False):
            return list(photos)

    mod.PhotosDB = PhotosDB
    monkeypatch.setitem(sys.modules, "osxphotos", mod)


def test_sync_and_timeline(conn, paths, monkeypatch):
    t1 = dt.datetime(2026, 11, 1, 14, 0, tzinfo=dt.timezone.utc)
    photos = [Photo("A1", t1), Broken("B2", t1, persons=[])]
    fake_osxphotos(monkeypatch, photos)
    (paths.home / "config.toml").write_text("[photos]\n")

    r = pipeline.sync_source(conn, paths, "photos")
    assert (r.status, r.rows_read, r.timeline_rows) == ("ok", 2, 2)
    p = conn.execute("SELECT * FROM photos_photos WHERE id = 'A1'").fetchone()
    assert p["ts"] == "2026-11-01T12:30:00.000000Z" and p["local_time"] == "2026-11-01T13:30:00"
    assert p["place"] == "Old Town, Warsaw" and p["camera"] == "Apple iPhone 16" and p["in_cloud_only"] == 1
    assert conn.execute("SELECT person FROM photos_people").fetchall()[0][0] == "Anna Nowak"
    assert conn.execute("SELECT place FROM photos_photos WHERE id = 'B2'").fetchone()[0] is None

    hit = pipeline.search(conn, "anna")[0]
    assert hit["title"] == "Photo at Old Town, Warsaw with Anna Nowak"

    # Only new or changed photos next time.
    photos.append(Photo("C3", t1 + dt.timedelta(days=1), persons=[]))
    r = pipeline.sync_source(conn, paths, "photos")
    assert (r.rows_read, r.message) == (1, "1 new or changed photo(s)")
    assert pipeline.sync_source(conn, paths, "photos").files == 0

    # A full export again after 30 days.
    conn.execute("UPDATE sync_state SET value = '2020-01-01T00:00:00.000000Z' WHERE key = 'last_full'")
    conn.commit()
    assert pipeline.sync_source(conn, paths, "photos").rows_read == 3


def test_without_osxphotos(conn, paths, monkeypatch):
    monkeypatch.setitem(sys.modules, "osxphotos", None)
    (paths.home / "config.toml").write_text("[photos]\n")
    try:
        pipeline.sync_source(conn, paths, "photos")
    except RuntimeError:
        pass
    assert "osxphotos is not installed" in conn.execute("SELECT error FROM import_runs").fetchone()[0]
