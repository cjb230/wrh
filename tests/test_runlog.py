import datetime as dt

import pytest

from conftest import make_chrome
from wrh import pipeline, runlog, scheduler
from wrh.sources import SOURCES


def runs(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM import_runs ORDER BY id")]


def test_import_is_logged(conn, paths, tmp_path):
    f = make_chrome(tmp_path / "History")
    pipeline.import_files(conn, paths, "browser", [f], {"browser": "chrome"})
    [r] = runs(conn)
    assert (r["source"], r["job"], r["status"], r["files"], r["timeline_rows"]) == ("browser", "import", "ok", 1, 1)
    assert r["finished_at"] and r["host"]


def test_failure_is_logged_and_rolled_back(conn, paths):
    with pytest.raises(ZeroDivisionError):
        with runlog.run(conn, "browser", "sync") as r:
            r.warn("odd row", item="row 7")
            conn.execute("INSERT INTO timeline (ts, source, source_table, source_key) VALUES ('x', 'browser', 't', 'k')")
            1 / 0
    [r] = runs(conn)
    assert r["status"] == "error" and r["error"].startswith("ZeroDivisionError") and r["problems"] == 2
    probs = conn.execute("SELECT level, item, message, detail FROM import_problems ORDER BY id").fetchall()
    assert [(p["level"], p["item"]) for p in probs] == [("warning", "row 7"), ("error", None)]
    assert "Traceback" in probs[1]["detail"]
    assert conn.execute("SELECT count(*) FROM timeline").fetchone()[0] == 0  # rolled back


def test_stale_running_row_is_marked_interrupted(conn):
    conn.execute("INSERT INTO import_runs (source, job, started_at, status, host, pid)"
                 " VALUES ('email', 'sync', '2026-01-01T00:00:00.000000Z', 'running', 'otherhost', 1)")
    with runlog.run(conn, "email", "sync"):
        pass
    assert [r["status"] for r in runs(conn)] == ["interrupted", "ok"]


def test_bad_inbox_file_goes_to_failed(conn, paths):
    d = paths.inbox / "browser"
    d.mkdir(parents=True)
    (d / "snap.sqlite").write_bytes(b"not a database")
    (d / "snap.json").write_text("{}")
    res = pipeline.ingest_inbox(conn, paths)
    assert "error" in res[0]
    assert (d / "failed" / "snap.sqlite").exists() and not (d / "snap.sqlite").exists()
    assert runs(conn)[0]["status"] == "error"


class FakeSync:
    calls = 0


@pytest.fixture
def fake_sync(monkeypatch):
    src = SOURCES["calendar"]

    def sync(ctx):
        FakeSync.calls += 1
        if ctx.settings.get("fail"):
            raise RuntimeError("network down")
        ctx.run.message = "fine"

    monkeypatch.setattr(src, "sync", sync)
    FakeSync.calls = 0
    return src


def test_scheduler_runs_due_jobs_and_checks(conn, paths, fake_sync):
    (paths.home / "config.toml").write_text("[calendar]\ninterval_minutes = 60\n")
    done = scheduler.run_due(conn, paths)
    assert [j for j, _ in done] == ["ingest", "calendar", "check"]
    assert FakeSync.calls == 1
    assert scheduler.run_due(conn, paths) == []  # nothing due again yet
    assert scheduler.check(conn, {"calendar": {}}) == []

    # Pretend the last run was two hours ago: due again.
    conn.execute("UPDATE import_runs SET started_at = '2020-01-01T00:00:00.000000Z'")
    conn.commit()
    assert [j for j, _ in scheduler.run_due(conn, paths, ["calendar"])] == ["calendar"]


def test_check_reports_failures_and_staleness(conn, paths, fake_sync):
    settings = {"calendar": {"fail": True}, "email": {"accounts": []}}
    (paths.home / "config.toml").write_text("[calendar]\nfail = true\n")
    assert scheduler.run_job(conn, paths, settings, "calendar") == "error"
    problems = scheduler.check(conn, settings)
    assert any("calendar: last run error" in p and "network down" in p for p in problems)
    assert any("email: no successful run ever" in p for p in problems)
    assert any("calendar: no successful run ever" in p for p in problems)


def test_lock_blocks_second_run(paths):
    paths.ensure()
    with scheduler.lock(paths):
        with pytest.raises(BlockingIOError):
            with scheduler.lock(paths):
                pass


def test_sync_state(conn):
    assert runlog.get_state(conn, "email", "x") is None
    runlog.set_state(conn, "email", "x", 5)
    runlog.set_state(conn, "email", "x", 6)
    assert runlog.get_state(conn, "email", "x") == "6"
