"""The `wrh` command."""

from __future__ import annotations

import json
from pathlib import Path

import click

from . import keys
from .config import get_paths


def _paths(ctx):
    return get_paths(ctx.obj.get("home"))


def _open(ctx, create=False):
    from .db import DatabaseError
    from .pipeline import open_db

    try:
        return open_db(_paths(ctx), create=create)
    except (keys.KeyError_, FileNotFoundError, DatabaseError) as e:
        raise click.ClickException(str(e))


@click.group()
@click.option("--home", envvar="WRH_HOME", type=click.Path(file_okay=False),
              help="Data directory (default ~/wrh-data, or $WRH_HOME).")
@click.pass_context
def cli(ctx, home):
    """wrh: a personal data warehouse."""
    ctx.ensure_object(dict)
    ctx.obj["home"] = home


@cli.group()
def key():
    """Manage the database encryption key."""


@key.command("init")
@click.option("--force", is_flag=True, help="Replace a key that is already stored. Dangerous.")
def key_init(force):
    """Generate a new key and store it in the OS keychain."""
    if keys.has_stored_key() and not force:
        raise click.ClickException("A key is already stored. Refusing to overwrite it (see --force).")
    k = keys.generate()
    keys.store_key(k)
    click.echo("Stored a new 256-bit key in the OS keychain (service 'wrh', account 'db-key').")
    click.echo("")
    click.echo("Save this copy in your password manager now. Without it, a lost keychain")
    click.echo("means the database cannot be opened:")
    click.echo("")
    click.echo(f"    {k}")


@key.command("import")
def key_import():
    """Store an existing key (for example from your password manager) in the keychain."""
    k = click.prompt("Key (64 hex characters)", hide_input=True)
    keys.store_key(k)
    click.echo("Stored.")


@key.command("check")
@click.pass_context
def key_check(ctx):
    """Check that the key opens the database."""
    conn = _open(ctx)
    n = conn.execute("SELECT count(*) FROM timeline").fetchone()[0]
    click.echo(f"OK: key opens {_paths(ctx).db} ({n} timeline rows).")


@cli.command()
@click.pass_context
def init(ctx):
    """Create the data directory and the encrypted database."""
    p = _paths(ctx)
    existed = p.db.exists()
    conn = _open(ctx, create=True)
    conn.close()
    click.echo(f"{'Opened' if existed else 'Created'} encrypted database at {p.db}")


@cli.command("import")
@click.argument("source")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--meta", "meta_pairs", multiple=True, metavar="KEY=VALUE",
              help="Metadata for these files, e.g. --meta browser=chrome --meta device=laptop")
@click.pass_context
def import_cmd(ctx, source, files, meta_pairs):
    """Archive and import raw FILES for SOURCE (e.g. `wrh import browser History`)."""
    from .pipeline import import_files

    meta = dict(p.split("=", 1) for p in meta_pairs)
    results = import_files(_open(ctx), _paths(ctx), source, files, meta)
    for r in results:
        click.echo(json.dumps(r))
    if any("error" in r for r in results):
        raise click.ClickException("Some files failed; see above, or the import_problems table.")


@cli.command()
@click.pass_context
def ingest(ctx):
    """Import everything collectors have dropped into inbox/<source>/."""
    from .pipeline import ingest_inbox

    conn = _open(ctx)
    results = ingest_inbox(conn, _paths(ctx))
    for r in results:
        click.echo(json.dumps(r))
    click.echo(f"{len(results)} file(s) processed.")


@cli.command()
@click.argument("source")
@click.pass_context
def sync(ctx, source):
    """Fetch new data for SOURCE now (email, calendar, messages, photos)."""
    from .pipeline import sync_source

    try:
        r = sync_source(_open(ctx), _paths(ctx), source)
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo(f"{source}: {r.status}. {r.files} file(s), {r.rows_read} rows read, "
               f"{r.timeline_rows} timeline rows. {r.message or ''}")
    if r.status != "ok":
        click.echo(f"See: wrh problems --run {r.id}")


@cli.command("run")
@click.option("--due", is_flag=True, help="Run only the jobs whose interval has passed (for launchd).")
@click.argument("jobs", nargs=-1)
@click.pass_context
def run_cmd(ctx, due, jobs):
    """Run scheduled jobs: all of them, the named JOBS, or (with --due) those due now."""
    from .scheduler import lock, run_due

    p = _paths(ctx)
    try:
        with lock(p):
            done = run_due(_open(ctx), p, list(jobs) or None, force=not due)
    except BlockingIOError:
        click.echo("Another `wrh run` is still going; skipping.")
        return
    for job, status in done:
        click.echo(f"{job}: {status}")


@cli.command()
@click.pass_context
def status(ctx):
    """Show each job's last run, last success, and whether it is stale."""
    from . import settings as settings_mod
    from .scheduler import status as _status

    rows = _status(_open(ctx), settings_mod.load(_paths(ctx)))
    click.echo(f"{'job':<10} {'every':>6} {'last run (UTC)':<17} {'status':<12} {'last ok (UTC)':<17} stale")
    for r in rows:
        click.echo(f"{r['job']:<10} {str(r['every_min']) + 'm':>6} {r['last_run'] or '-':<17} "
                   f"{r['last_status']:<12} {r['last_ok'] or '-':<17} {'STALE' if r['stale'] else ''}")
        if r["error"]:
            click.echo(f"{'':11}{r['error']}")


@cli.command()
@click.option("--notify", is_flag=True, help="Also send the alert (notification, and email if configured).")
@click.pass_context
def check(ctx, notify):
    """Report failing or stale sources. Exits 1 if there are any."""
    from . import runlog
    from . import settings as settings_mod
    from .scheduler import check as _check, notify as _notify

    conn, p = _open(ctx), _paths(ctx)
    s = settings_mod.load(p)
    problems = _check(conn, s)
    for line in problems:
        click.echo(line)
    if problems and notify:
        with runlog.run(conn, "*", "check") as r:
            r.message = f"{len(problems)} problem(s)"
            _notify(conn, p, s, problems, r)
    if not problems:
        click.echo("All good.")
    ctx.exit(1 if problems else 0)


@cli.command()
@click.option("--run", "run_id", type=int, default=None, help="Only this run.")
@click.option("--limit", default=30)
@click.pass_context
def problems(ctx, run_id, limit):
    """List recent problems from the run log."""
    rows = _open(ctx).execute(
        "SELECT p.ts, p.source, p.level, p.item, p.message, p.run_id FROM import_problems p"
        " WHERE (:run IS NULL OR p.run_id = :run) ORDER BY p.id DESC LIMIT :limit",
        {"run": run_id, "limit": limit},
    ).fetchall()
    for r in rows:
        click.echo(f"{r['ts'][:19]} {r['source']:<9} {r['level']:<7} run {r['run_id']}: {r['message']}"
                   + (f" [{r['item']}]" if r["item"] else ""))


@cli.command()
@click.argument("sha256")
@click.option("--out", "out", type=click.Path(dir_okay=False), default=None,
              help="Where to write it (default: its original name, in the current folder).")
@click.pass_context
def attachment(ctx, sha256, out):
    """Decrypt a stored email attachment (the sha256 from email_messages.attachments)."""
    import shutil

    from .sources.email import attachments as att

    conn = _open(ctx)
    try:
        with att.plaintext(conn, _paths(ctx), sha256) as (p, name):
            dest = Path(out or Path(name or sha256).name)
            if dest.exists():
                raise click.ClickException(f"{dest} already exists")
            shutil.copyfile(p, dest)
    except KeyError as e:
        raise click.ClickException(str(e))
    click.echo(f"Wrote {dest}")


@cli.group()
def secret():
    """Passwords and secret URLs used by sync jobs, kept in the Keychain."""


@secret.command("set")
@click.argument("name")
def secret_set(name):
    """Store secret NAME, e.g. `email/gmail` (an app password) or `calendar/personal` (a URL)."""
    value = click.prompt(f"Value for {name}", hide_input=True)
    keys.set_secret(name, value)
    click.echo("Stored in the Keychain.")


@cli.command()
@click.argument("source")
@click.pass_context
def reimport(ctx, source):
    """Rebuild SOURCE's tables and timeline from the raw archive."""
    from .pipeline import reimport as _reimport

    click.echo(json.dumps(_reimport(_open(ctx), _paths(ctx), source)))


@cli.command()
@click.argument("query")
@click.option("--limit", default=20)
@click.option("--source", default=None)
@click.pass_context
def search(ctx, query, limit, source):
    """Full-text search across the timeline (SQLite FTS5 syntax)."""
    from .pipeline import search as _search

    for r in _search(_open(ctx), query, limit, source):
        click.echo(f"{r['ts'][:19]}  {r['source']:<8} {r['title'] or ''}\n{'':29}{r['url'] or ''}")


@cli.group()
def collect():
    """Collectors. Run these on your everyday machines."""


@collect.command("browser")
@click.option("--out", "outbox", required=True, type=click.Path(file_okay=False),
              help="Folder to write snapshots to, later copied to the server's inbox/browser/.")
@click.option("--device", default=None, help="Device name (default: this machine's hostname).")
def collect_browser(outbox, device):
    """Snapshot local browser history (Chrome, Edge, Brave, Arc, Firefox, Safari)."""
    from .sources.browser.collector import collect as _collect

    written = _collect(Path(outbox), device)
    for w in written:
        click.echo(str(w))
    click.echo(f"{len(written)} snapshot(s) written.")


@cli.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8001, show_default=True)
@click.pass_context
def serve(ctx, host, port):
    """Browse and search the database with Datasette. Extra args go to datasette."""
    from .serve import run

    p = _paths(ctx)
    _open(ctx).close()  # fail early with a clear message if the key is wrong
    run(p.db, keys.get_key(), host, port, list(ctx.args))
