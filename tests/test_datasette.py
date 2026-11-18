"""Datasette must be able to read and full-text search the encrypted db.

Runs in a subprocess because the keyed SQLite module has to be installed
before Datasette is first imported.
"""

import subprocess
import sys
import textwrap

from conftest import KEY, make_firefox
from wrh import pipeline


def test_datasette_reads_and_searches_encrypted_db(conn, paths, tmp_path):
    pipeline.import_file(conn, paths, "browser", make_firefox(tmp_path / "places.sqlite"), {})
    conn.close()
    script = textwrap.dedent(f"""
        import asyncio, json
        from wrh.serve import install_keyed_sqlite, DATASETTE_DIR
        install_keyed_sqlite({KEY!r})
        import yaml
        from datasette.app import Datasette

        async def main():
            meta = yaml.safe_load((DATASETTE_DIR / "metadata.yml").read_text())
            ds = Datasette([{str(paths.db)!r}], metadata=meta)
            r = await ds.client.get("/wrh/timeline.json?_search=dogsheep&_shape=array")
            assert r.status_code == 200, r.text
            rows = r.json()
            assert rows[0]["url"] == "https://simonwillison.net/dogsheep", rows
            r = await ds.client.get("/wrh/first_seen.json?q=willison&_shape=array")
            assert r.status_code == 200, r.text
            assert len(r.json()) == 1
            print("ok")
        asyncio.run(main())
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout
