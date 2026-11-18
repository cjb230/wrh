import datetime as dt
import sqlite3
from pathlib import Path

import pytest

KEY = "0f" * 32


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("WRH_DB_KEY", KEY)
    return KEY


@pytest.fixture
def paths(tmp_path, key):
    from wrh.config import get_paths

    return get_paths(tmp_path / "data")


@pytest.fixture
def conn(paths):
    from wrh.pipeline import open_db

    c = open_db(paths, create=True)
    yield c
    c.close()


WHEN = dt.datetime(2026, 11, 1, 12, 30, tzinfo=dt.timezone.utc)


def make_chrome(path: Path) -> Path:
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER,
                       typed_count INTEGER, last_visit_time INTEGER, hidden INTEGER);
    CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER,
                         from_visit INTEGER, transition INTEGER, segment_id INTEGER,
                         visit_duration INTEGER);
    """)
    t = int((WHEN - dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)).total_seconds() * 1e6)
    c.execute("INSERT INTO urls VALUES (1, 'https://www.example.com/sourdough', 'Sourdough starter guide', 1, 0, ?, 0)", (t,))
    c.execute("INSERT INTO urls VALUES (2, 'https://ads.example.net/frame', 'ad', 1, 0, ?, 0)", (t,))
    c.execute("INSERT INTO visits VALUES (1, 1, ?, 0, 805306368, 0, 5000000)", (t,))  # LINK + chain flags
    c.execute("INSERT INTO visits VALUES (2, 2, ?, 0, 3, 0, 0)", (t + 1,))  # AUTO_SUBFRAME: skipped
    c.commit()
    c.close()
    return path


def make_firefox(path: Path) -> Path:
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT, rev_host TEXT);
    CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, from_visit INTEGER, place_id INTEGER,
                                    visit_date INTEGER, visit_type INTEGER, session INTEGER);
    """)
    t = int(WHEN.timestamp() * 1e6)
    c.execute("INSERT INTO moz_places VALUES (1, 'https://simonwillison.net/dogsheep', 'Dogsheep by Simon Willison', '')")
    c.execute("INSERT INTO moz_historyvisits VALUES (1, 0, 1, ?, 2, 0)", (t,))
    c.commit()
    c.close()
    return path


def make_safari(path: Path) -> Path:
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE history_items (id INTEGER PRIMARY KEY, url TEXT, domain_expansion TEXT, visit_count INTEGER);
    CREATE TABLE history_visits (id INTEGER PRIMARY KEY, history_item INTEGER, visit_time REAL,
                                 title TEXT, load_successful BOOLEAN, redirect_source INTEGER,
                                 redirect_destination INTEGER);
    """)
    t = (WHEN - dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)).total_seconds()
    c.execute("INSERT INTO history_items VALUES (1, 'https://en.wikipedia.org/wiki/Quantified_self', 'wikipedia', 1)")
    c.execute("INSERT INTO history_visits VALUES (1, 1, ?, 'Quantified self - Wikipedia', 1, NULL, NULL)", (t,))
    c.commit()
    c.close()
    return path
