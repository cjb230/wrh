import json
import zipfile

from wrh import pipeline, raw


def claude_conv(title="Sourdough troubleshooting", extra=False):
    msgs = [
        {"uuid": "m1", "sender": "human", "text": "Why is my sourdough starter not rising?",
         "content": [{"type": "text", "text": "Why is my sourdough starter not rising?"}],
         "created_at": "2026-10-01T09:00:00.123456Z",
         "attachments": [{"file_name": "notes.txt", "extracted_content": "fed it rye flour on Tuesday"}],
         "files": []},
        {"uuid": "m2", "sender": "assistant", "text": "",
         "content": [{"type": "thinking", "thinking": "hmm"},
                     {"type": "text", "text": "It may be too cold. Try a warmer spot."}],
         "created_at": "2026-10-01T09:00:05Z", "attachments": [], "files": []},
    ]
    if extra:
        msgs.append({"uuid": "m3", "sender": "human", "text": "Thanks, the kitchen was cold.",
                     "content": [], "created_at": "2026-10-02T08:00:00Z",
                     "attachments": [], "files": []})
    return {"uuid": "c-123", "name": title, "created_at": "2026-10-01T09:00:00Z",
            "updated_at": "2026-10-02T08:00:00Z" if extra else "2026-10-01T09:00:05Z",
            "account": {"uuid": "acct"}, "chat_messages": msgs}


def chatgpt_conv():
    def node(nid, parent, children, role=None, text=None, t=None, ctype="text", **md):
        msg = None
        if role:
            msg = {"id": nid, "author": {"role": role, "name": None}, "create_time": t,
                   "content": {"content_type": ctype, "parts": [text]}, "metadata": md}
        return {"id": nid, "message": msg, "parent": parent, "children": children}

    t0 = 1790000000.0
    mapping = {
        "root": node("root", None, ["sys"]),
        "sys": node("sys", "root", ["u1"], "system", "", None, is_visually_hidden_from_conversation=True),
        "u1": node("u1", "sys", ["a1", "a2"], "user", "Explain the Dogsheep project", t0),
        "a1": node("a1", "u1", [], "assistant", "First draft answer about Dogsheep", t0 + 5, model_slug="gpt-4o"),
        "a2": node("a2", "u1", [], "assistant", "Dogsheep is a set of tools by Simon Willison", t0 + 9,
                   model_slug="gpt-5"),
        "ctx": node("ctx", "root", [], "user", "custom instructions", t0, ctype="user_editable_context"),
    }
    return {"title": "Dogsheep question", "create_time": t0, "update_time": t0 + 9, "mapping": mapping,
            "current_node": "a2", "conversation_id": "g-456", "id": "g-456",
            "default_model_slug": "gpt-5", "is_archived": False}


def make_zip(path, convs, folder=""):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(folder + "conversations.json", json.dumps(convs))
        z.writestr(folder + "users.json", "[]")
    return path


def test_claude_and_chatgpt_exports(conn, paths, tmp_path):
    r1 = pipeline.import_file(conn, paths, "llm", make_zip(tmp_path / "claude.zip", [claude_conv()]), {})
    r2 = pipeline.import_file(conn, paths, "llm", make_zip(tmp_path / "gpt.zip", [chatgpt_conv()], "x/"), {})
    assert (r1["rows_read"], r1["timeline_rows"]) == (2, 2)
    assert (r2["rows_read"], r2["timeline_rows"]) == (3, 3)  # hidden system + custom instructions skipped

    msgs = {r["id"]: r for r in conn.execute("SELECT * FROM llm_messages")}
    assert msgs["claude:m1"]["ts"] == "2026-10-01T09:00:00.123456Z"
    assert msgs["claude:m2"]["text"] == "It may be too cold. Try a warmer spot."
    assert msgs["claude:m2"]["role"] == "assistant"
    assert msgs["chatgpt:a2"]["model"] == "gpt-5"
    assert msgs["chatgpt:a2"]["on_current_branch"] == 1
    assert msgs["chatgpt:a1"]["on_current_branch"] == 0
    assert msgs["chatgpt:a2"]["parent_id"] == "chatgpt:u1"

    hit = pipeline.search(conn, "rye")[0]  # attachment text is searchable
    assert hit["url"] == "https://claude.ai/chat/c-123"
    assert hit["title"] == "You: Sourdough troubleshooting"
    hits = pipeline.search(conn, "willison", source="llm")
    assert [(h["title"], h["url"]) for h in hits] == [("ChatGPT: Dogsheep question", "https://chatgpt.com/c/g-456")]


def test_newer_export_updates_without_duplicates(conn, paths, tmp_path):
    old = make_zip(tmp_path / "old.zip", [claude_conv()])
    new = make_zip(tmp_path / "new.zip", [claude_conv("Sourdough: cold kitchen", extra=True)])
    pipeline.import_file(conn, paths, "llm", old, {})
    pipeline.import_file(conn, paths, "llm", new, {})
    assert conn.execute("SELECT count(*) FROM llm_messages").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM timeline WHERE source = 'llm'").fetchone()[0] == 3
    title = lambda: conn.execute("SELECT title FROM llm_conversations").fetchone()[0]
    assert title() == "Sourdough: cold kitchen"
    assert pipeline.search(conn, "kitchen")

    # Re-importing the old export (different file, older data) changes nothing.
    older_copy = make_zip(tmp_path / "old2.zip", [claude_conv()] + [])
    with zipfile.ZipFile(older_copy, "a") as z:
        z.writestr("projects.json", "[]")
    pipeline.import_file(conn, paths, "llm", older_copy, {})
    assert title() == "Sourdough: cold kitchen"
    assert conn.execute("SELECT count(*) FROM timeline WHERE source = 'llm'").fetchone()[0] == 3

    r = pipeline.reimport(conn, paths, "llm")
    assert r["timeline_rows"] == 3 and r["raw_files"] == 3
    assert title() == "Sourdough: cold kitchen"
    assert conn.execute("SELECT count(*) FROM timeline WHERE source = 'llm'").fetchone()[0] == 3


def test_export_is_encrypted_in_archive(conn, paths, tmp_path):
    f = make_zip(tmp_path / "claude.zip", [claude_conv()])
    pipeline.import_file(conn, paths, "llm", f, {})
    rel = conn.execute("SELECT path FROM raw_files").fetchone()[0]
    stored = (paths.raw / rel).read_bytes()
    assert stored.startswith(raw.MAGIC) and b"sourdough" not in stored and b"PK" not in stored[:16]
    with raw.plaintext(paths, rel) as plain:
        assert plain.read_bytes() == f.read_bytes()
