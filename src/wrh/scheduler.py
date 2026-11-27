"""Unattended running: `wrh run --due`, `wrh status` and `wrh check`.

One LaunchAgent on the server (collectors/macos/com.wrh.run.plist) runs
``wrh run --due`` every 15 minutes. That runs each job whose interval has
passed since its last run:

* ``ingest``  - collector snapshots waiting in inbox/
* one job per source with a section in config.toml (email, calendar, ...)
* ``check``   - once a day: report failing or stale sources

A lock file stops two runs overlapping. A job that fails is logged in
import_runs / import_problems and the next job still runs.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import smtplib
import socket
import subprocess
from contextlib import contextmanager
from email.message import EmailMessage

from . import keys, pipeline, runlog
from . import settings as settings_mod
from .config import Paths
from .sources import SOURCES
from .timeutil import now_iso


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)


@contextmanager
def lock(paths: Paths):
    """Hold <data>/run.lock, or raise BlockingIOError if another run has it."""
    paths.home.mkdir(parents=True, exist_ok=True)
    f = open(paths.home / "run.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(str(os.getpid()))
        f.flush()
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def configured_jobs(settings: dict) -> list[str]:
    """Jobs in the order they run."""
    jobs = ["ingest"]
    jobs += [n for n, s in SOURCES.items() if s.can_sync and settings_mod.section(settings, n) is not None]
    jobs.append("check")
    return jobs


def _source_of(job: str) -> str:
    return {"ingest": "*", "check": "*"}.get(job, job)


def _job_of(job: str) -> str:
    return job if job in ("ingest", "check") else "sync"


def last_run(conn, job: str, ok_only: bool = False):
    sql = "SELECT * FROM import_runs WHERE source = ? AND job = ?"
    if ok_only:
        sql += " AND status IN ('ok', 'warning')"
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", (_source_of(job), _job_of(job))).fetchone()


def is_due(conn, settings: dict, job: str, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(dt.timezone.utc)
    last = last_run(conn, job)
    if last is None:
        return True
    # A little slack, so a 15-minute job run by a 15-minute timer is never skipped.
    interval = dt.timedelta(minutes=settings_mod.interval_minutes(settings, job)) * 0.9
    return now - _parse(last["started_at"]) >= interval


def run_job(conn, paths: Paths, settings: dict, job: str, trigger: str = "schedule") -> str:
    """Run one job; never raises. Returns its status."""
    if job == "ingest":
        # ingest logs one run per source that had files; record the sweep itself too.
        with runlog.run(conn, "*", "ingest", trigger, swallow=True) as r:
            results = pipeline.ingest_inbox(conn, paths, trigger)
            r.files = sum(1 for x in results if "error" not in x and not x.get("skipped"))
            r.message = f"{len(results)} file(s) in inbox"
        return r.status
    if job == "check":
        with runlog.run(conn, "*", "check", trigger, swallow=True) as r:
            problems = check(conn, settings)
            r.message = f"{len(problems)} problem(s)"
            if problems:
                notify(conn, paths, settings, problems, r)
        return "ok"
    try:
        r = pipeline.sync_source(conn, paths, job, trigger, settings)
        return r.status
    except Exception:
        return "error"  # already recorded in import_runs by sync_source


def run_due(conn, paths: Paths, only: list[str] | None = None, force: bool = False) -> list[tuple[str, str]]:
    settings = settings_mod.load(paths)
    done = []
    for job in configured_jobs(settings):
        if only and job not in only:
            continue
        if not force and not is_due(conn, settings, job):
            continue
        done.append((job, run_job(conn, paths, settings, job, "manual" if force else "schedule")))
    return done


# --- status and checks ---------------------------------------------------------

def status(conn, settings: dict) -> list[dict]:
    rows = []
    now = dt.datetime.now(dt.timezone.utc)
    for job in configured_jobs(settings):
        if job == "check":
            continue
        last, ok = last_run(conn, job), last_run(conn, job, ok_only=True)
        hours = (now - _parse(ok["finished_at"] or ok["started_at"])).total_seconds() / 3600 if ok else None
        stale_after = settings_mod.stale_after_hours(settings, job)
        rows.append({
            "job": job,
            "every_min": settings_mod.interval_minutes(settings, job),
            "last_run": last["started_at"][:16] if last else None,
            "last_status": last["status"] if last else "never run",
            "last_ok": ok["finished_at"][:16] if ok and ok["finished_at"] else None,
            "hours_since_ok": round(hours, 1) if hours is not None else None,
            "stale": hours is None or hours > stale_after,
            "error": (last["error"] or "")[:120] if last and last["status"] in ("error", "interrupted") else "",
        })
    return rows


def check(conn, settings: dict) -> list[str]:
    """Human-readable problems: failing jobs and sources with no recent success."""
    problems = []
    for s in status(conn, settings):
        if s["job"] == "ingest":
            # Ingest only matters if collectors are sending files; its failures
            # show up per source below.
            if s["last_status"] == "error":
                problems.append(f"ingest: last run failed: {s['error']}")
            continue
        if s["last_status"] in ("error", "interrupted"):
            problems.append(f"{s['job']}: last run {s['last_status']} at {s['last_run']} UTC: {s['error']}")
        if s["stale"]:
            since = f"since {s['last_ok']} UTC" if s["last_ok"] else "ever"
            problems.append(f"{s['job']}: no successful run {since}")
    # Collector sources: failed snapshots waiting in inbox/*/failed are listed by ingest runs.
    for r in conn.execute(
        "SELECT source, count(*) n FROM import_runs WHERE job = 'ingest' AND status = 'error'"
        " AND source != '*' AND started_at > ? GROUP BY source", (_ago(24),)
    ):
        problems.append(f"{r['source']}: {r['n']} ingest run(s) with errors in the last 24 hours")
    return problems


def _ago(hours: float) -> str:
    from .timeutil import to_iso

    return to_iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours))


def notify(conn, paths: Paths, settings: dict, problems: list[str], r: runlog.Run) -> None:
    """Tell James: a macOS notification on the server, and an email if [alerts] is set.

    Emails at most once a day, and again sooner only if the problems change.
    """
    text = "\n".join(problems)
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification {json.dumps(text[:200])} with title "wrh: problems"'],
            check=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    alerts = settings.get("alerts") or {}
    if not alerts.get("email_to"):
        return
    last_text = runlog.get_state(conn, "*", "alert_text")
    last_at = runlog.get_state(conn, "*", "alert_at")
    if last_text == text and last_at and _parse(last_at) > dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=23):
        return
    try:
        send_email(settings, f"wrh on {socket.gethostname()}: {len(problems)} problem(s)",
                   text + "\n\nDetails: `wrh status`, or the import_runs and import_problems tables.")
    except Exception as e:
        r.warn(f"Could not send the alert email: {e}")
        return
    with conn:
        runlog.set_state(conn, "*", "alert_text", text)
        runlog.set_state(conn, "*", "alert_at", now_iso())


def send_email(settings: dict, subject: str, body: str) -> None:
    alerts = settings["alerts"]
    accounts = {a["name"]: a for a in (settings.get("email") or {}).get("accounts", [])}
    acct = accounts.get(alerts.get("smtp_account") or "")
    if acct is None:
        raise ValueError("[alerts] smtp_account must name one of the [[email.accounts]]")
    host = alerts.get("smtp_host") or acct.get("smtp_host") or acct["host"].replace("imap.", "smtp.", 1)
    port = int(alerts.get("smtp_port") or 465)
    msg = EmailMessage()
    msg["From"] = acct["user"]
    msg["To"] = alerts["email_to"]
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(acct["user"], keys.get_secret(f"email/{acct['name']}"))
        s.send_message(msg)
