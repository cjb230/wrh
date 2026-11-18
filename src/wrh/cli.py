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
    from .pipeline import import_file

    meta = dict(p.split("=", 1) for p in meta_pairs)
    conn = _open(ctx)
    for f in files:
        click.echo(json.dumps(import_file(conn, _paths(ctx), source, Path(f), meta)))


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
