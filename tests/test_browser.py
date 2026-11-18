import json

from conftest import make_chrome, make_firefox, make_safari
from wrh import pipeline


def test_import_all_three_formats(conn, paths, tmp_path):
    for maker, name, browser in [(make_chrome, "History", "chrome"),
                                 (make_firefox, "places.sqlite", "firefox"),
                                 (make_safari, "History.db", "safari")]:
        f = maker(tmp_path / f"{browser}-{name}")
        r = pipeline.import_file(conn, paths, "browser", f, {"browser": browser, "device": "mbp"})
        assert r["timeline_rows"] == 1, r

    rows = conn.execute("SELECT browser, ts, domain, transition FROM browser_visits ORDER BY browser").fetchall()
    assert [tuple(r) for r in rows] == [
        ("chrome", "2026-11-01T12:30:00.000000Z", "example.com", "link"),
        ("firefox", "2026-11-01T12:30:00.000000Z", "simonwillison.net", "typed"),
        ("safari", "2026-11-01T12:30:00.000000Z", "en.wikipedia.org", None),
    ]
    hits = pipeline.search(conn, "dogsheep")
    assert [h["url"] for h in hits] == ["https://simonwillison.net/dogsheep"]
    assert pipeline.search(conn, "sourdough")[0]["title"] == "Sourdough starter guide"


def test_reimport_is_idempotent(conn, paths, tmp_path):
    f = make_chrome(tmp_path / "History")
    pipeline.import_file(conn, paths, "browser", f, {"browser": "chrome"})
    again = pipeline.import_file(conn, paths, "browser", f, {"browser": "chrome"})
    assert again["skipped"] == "already archived"
    r = pipeline.reimport(conn, paths, "browser")
    assert r == {"raw_files": 1, "rows_read": 1, "timeline_rows": 1}
    assert conn.execute("SELECT count(*) FROM timeline_fts WHERE timeline_fts MATCH 'sourdough'").fetchone()[0] == 1
    # Raw copy is encrypted on disk and decrypts back to the identical bytes.
    from wrh import raw

    rel = conn.execute("SELECT path FROM raw_files").fetchone()[0]
    stored = (paths.raw / rel).read_bytes()
    assert stored.startswith(raw.MAGIC) and b"SQLite format 3" not in stored
    with raw.plaintext(paths, rel) as plain:
        assert plain.read_bytes() == f.read_bytes()
    assert list((paths.home / "tmp").iterdir()) == []


def test_collect_then_ingest(conn, paths, tmp_path, monkeypatch):
    import sys

    from wrh.sources.browser import collector

    home = tmp_path / "home"
    monkeypatch.setattr(sys, "platform", "darwin")
    prof = home / "Library/Application Support/Google/Chrome/Default"
    prof.mkdir(parents=True)
    make_chrome(prof / "History")
    ff = home / "Library/Application Support/Firefox/Profiles/abc.default"
    ff.mkdir(parents=True)
    make_firefox(ff / "places.sqlite")

    outbox = paths.inbox / "browser"
    written = collector.collect(outbox, device="laptop", home=home)
    assert len(written) == 2
    meta = json.loads(written[0].with_suffix(".json").read_text())
    assert meta["device"] == "laptop"

    results = pipeline.ingest_inbox(conn, paths)
    assert len(results) == 2
    assert list(outbox.iterdir()) == []
    assert conn.execute("SELECT count(*) FROM timeline").fetchone()[0] == 2
