"""LLM chat history from the official Claude and ChatGPT data exports.

Both services email you a zip with a ``conversations.json`` inside:

* Claude (claude.ai > Settings > Privacy > Export data): a list of
  conversations, each with ``chat_messages`` in order.
* ChatGPT (chatgpt.com > Settings > Data controls > Export data): a list of
  conversations, each with a ``mapping`` tree of messages. Edits and
  regenerations are branches of that tree; every branch is kept, and
  ``on_current_branch`` marks the one the conversation ended on.

The format is detected from the file, so one source, ``llm``, handles both:
``wrh import llm ~/Downloads/<export>.zip``. A bare conversations.json works too.

Each export holds your whole history, so importing a newer export updates
conversations in place (ids are the services' own) and adds new messages
without duplicating old ones. Conversations deleted from the service stay
here. An older export imported after a newer one does not undo newer data.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Mapping

from ...timeutil import to_iso
from ..base import Source

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_conversations (
    id TEXT PRIMARY KEY,           -- provider:conversation id
    provider TEXT NOT NULL,        -- claude, chatgpt
    conversation_id TEXT NOT NULL, -- the service's own id
    title TEXT,
    summary TEXT,                  -- Claude only, when the export has one
    created_at TEXT,
    updated_at TEXT,
    model TEXT,                    -- default model, when the export says
    url TEXT,                      -- open the conversation on the service
    message_count INTEGER,
    is_archived INTEGER,           -- ChatGPT only
    raw_file_id INTEGER REFERENCES raw_files(id)
);
CREATE INDEX IF NOT EXISTS llm_conversations_updated ON llm_conversations(updated_at);

CREATE TABLE IF NOT EXISTS llm_messages (
    id TEXT PRIMARY KEY,           -- provider:message id
    conversation_id TEXT NOT NULL REFERENCES llm_conversations(id),
    provider TEXT NOT NULL,
    message_id TEXT NOT NULL,      -- the service's own id
    parent_id TEXT,                -- provider:message id of the previous message
    seq INTEGER,                   -- position in the conversation (depth, for ChatGPT)
    ts TEXT NOT NULL,
    role TEXT NOT NULL,            -- user, assistant, system, tool
    author_name TEXT,              -- tool name, for tool messages
    model TEXT,                    -- ChatGPT only: model that wrote this message
    content_type TEXT,
    text TEXT,
    attachments TEXT,              -- JSON list of {name, text} (Claude)
    on_current_branch INTEGER,     -- 1 if on the branch the conversation ended on
    raw_file_id INTEGER REFERENCES raw_files(id)
);
CREATE INDEX IF NOT EXISTS llm_messages_conv ON llm_messages(conversation_id, seq);
CREATE INDEX IF NOT EXISTS llm_messages_ts ON llm_messages(ts);
CREATE INDEX IF NOT EXISTS llm_messages_raw ON llm_messages(raw_file_id);
"""

CONVERSATIONS_JSON = re.compile(r"conversations(-\d+)?\.json")
ASSISTANT = {"claude": "Claude", "chatgpt": "ChatGPT"}
# Only these roles go on the timeline; tool and system messages stay in llm_messages.
TIMELINE_ROLES = {"user", "assistant"}


def load_conversations(path: Path) -> list[dict]:
    """Read conversations.json from an export zip, or a bare JSON file."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = sorted(n for n in z.namelist()
                           if CONVERSATIONS_JSON.fullmatch(PurePosixPath(n).name))
            if not names:
                raise ValueError(f"{path.name}: no conversations.json in this zip")
            convs: list = []
            for n in names:
                convs.extend(json.loads(z.read(n)))
            return convs
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a list of conversations")
    return data


def detect_provider(convs: list[dict]) -> str | None:
    for c in convs:
        if "chat_messages" in c:
            return "claude"
        if "mapping" in c:
            return "chatgpt"
    return None


def _iso_str(s) -> str | None:
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return to_iso(t)


def _iso_epoch(x) -> str | None:
    if x is None:
        return None
    return to_iso(dt.datetime.fromtimestamp(float(x), dt.timezone.utc))


# --- Claude -----------------------------------------------------------------

def _claude_text(m: dict) -> str:
    blocks = m.get("content") or []
    texts = [b.get("text") or "" for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n\n".join(t for t in texts if t)
    return text or (m.get("text") or "")


def _claude(convs: list[dict]) -> Iterator[tuple[dict, list[dict]]]:
    for c in convs:
        cid = c["uuid"]
        created = _iso_str(c.get("created_at"))
        msgs = []
        for i, m in enumerate(c.get("chat_messages") or []):
            atts = [{"name": a.get("file_name"), "text": a.get("extracted_content")}
                    for a in (m.get("attachments") or [])]
            atts += [{"name": f.get("file_name"), "text": None} for f in (m.get("files") or [])]
            sender = m.get("sender") or "unknown"
            parent = m.get("parent_message_uuid")
            msgs.append({
                "message_id": m["uuid"],
                "parent_id": f"claude:{parent}" if parent else None,
                "seq": i,
                "ts": _iso_str(m.get("created_at")) or created,
                "role": "user" if sender == "human" else sender,
                "author_name": None,
                "model": None,
                "content_type": "text",
                "text": _claude_text(m),
                "attachments": atts,
                "on_current_branch": 1,
            })
        yield {
            "conversation_id": cid,
            "title": c.get("name") or None,
            "summary": c.get("summary") or None,
            "created_at": created,
            "updated_at": _iso_str(c.get("updated_at")) or created,
            "model": c.get("model"),
            "url": f"https://claude.ai/chat/{cid}",
            "is_archived": None,
        }, msgs


# --- ChatGPT ----------------------------------------------------------------

# Content that is not part of the visible conversation, or repeats in every chat.
CHATGPT_SKIP_TYPES = {"user_editable_context", "thoughts", "reasoning_recap", "model_editable_context"}


def _chatgpt_text(content: dict) -> str:
    out = []
    for p in content.get("parts") or []:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            if isinstance(p.get("text"), str):
                out.append(p["text"])
            elif "image" in str(p.get("content_type", "")):
                out.append("[image]")
    for k in ("text", "result"):
        if isinstance(content.get(k), str):
            out.append(content[k])
    return "\n\n".join(x for x in out if x and x.strip())


def _chatgpt(convs: list[dict]) -> Iterator[tuple[dict, list[dict]]]:
    for c in convs:
        cid = c.get("conversation_id") or c.get("id")
        mapping = c.get("mapping") or {}
        created = _iso_epoch(c.get("create_time"))

        current, node = set(), c.get("current_node")
        while node and node in mapping and node not in current:
            current.add(node)
            node = mapping[node].get("parent")

        depth: dict[str, int] = {}

        def depth_of(nid: str) -> int:
            chain = []
            while nid in mapping and nid not in depth and len(chain) <= len(mapping):
                chain.append(nid)
                nid = mapping[nid].get("parent")
            d = depth.get(nid, -1)
            for n in reversed(chain):
                d += 1
                depth[n] = d
            return d if chain else depth.get(nid, 0)

        msgs = []
        for nid, n in mapping.items():
            m = n.get("message")
            if not m:
                continue
            content = m.get("content") or {}
            md = m.get("metadata") or {}
            if content.get("content_type") in CHATGPT_SKIP_TYPES:
                continue
            if md.get("is_visually_hidden_from_conversation"):
                continue
            text = _chatgpt_text(content)
            if not text:
                continue
            author = m.get("author") or {}
            parent = n.get("parent")
            msgs.append({
                "message_id": nid,
                "parent_id": f"chatgpt:{parent}" if parent else None,
                "seq": depth_of(nid),
                "ts": _iso_epoch(m.get("create_time")) or created,
                "role": author.get("role") or "unknown",
                "author_name": author.get("name"),
                "model": md.get("model_slug"),
                "content_type": content.get("content_type"),
                "text": text,
                "attachments": [],
                "on_current_branch": 1 if nid in current else 0,
            })
        msgs.sort(key=lambda x: (x["seq"], x["ts"] or ""))
        yield {
            "conversation_id": cid,
            "title": c.get("title") or None,
            "summary": None,
            "created_at": created,
            "updated_at": _iso_epoch(c.get("update_time")) or created,
            "model": c.get("default_model_slug"),
            "url": f"https://chatgpt.com/c/{cid}",
            "is_archived": int(bool(c.get("is_archived"))) if "is_archived" in c else None,
        }, msgs


PARSERS = {"claude": _claude, "chatgpt": _chatgpt}

CONV_COLS = ["id", "provider", "conversation_id", "title", "summary", "created_at", "updated_at",
             "model", "url", "message_count", "is_archived", "raw_file_id"]
MSG_COLS = ["id", "conversation_id", "provider", "message_id", "parent_id", "seq", "ts", "role",
            "author_name", "model", "content_type", "text", "attachments", "on_current_branch",
            "raw_file_id"]


def _insert(table: str, cols: list[str], update: bool) -> str:
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"
    if update:
        sets = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
        return sql + f" ON CONFLICT (id) DO UPDATE SET {sets}"
    return sql + " ON CONFLICT (id) DO NOTHING"


class LLMChats(Source):
    name = "llm"
    timeline_table = "llm_messages"
    schema = SCHEMA

    def reset(self, conn) -> None:
        conn.execute("DELETE FROM llm_messages")
        conn.execute("DELETE FROM llm_conversations")

    def import_file(self, conn, path: Path, meta: Mapping, raw_file_id: int) -> int:
        convs = load_conversations(path)
        provider = meta.get("provider") or detect_provider(convs)
        if provider not in PARSERS:
            if not convs:
                return 0
            raise ValueError(f"{Path(path).name}: not a Claude or ChatGPT export")
        n = 0
        for conv, msgs in PARSERS[provider](convs):
            cid = f"{provider}:{conv['conversation_id']}"
            old = conn.execute("SELECT updated_at FROM llm_conversations WHERE id = ?", (cid,)).fetchone()
            # Newest export wins. An older one only adds messages we have never seen.
            newer = old is None or (conv["updated_at"] or "") >= (old[0] or "")
            conn.execute(_insert("llm_conversations", CONV_COLS, newer), {
                **conv, "id": cid, "provider": provider, "message_count": len(msgs),
                "raw_file_id": raw_file_id,
            })
            msg_sql = _insert("llm_messages", MSG_COLS, newer)
            for m in msgs:
                conn.execute(msg_sql, {
                    **m,
                    "id": f"{provider}:{m['message_id']}",
                    "conversation_id": cid,
                    "provider": provider,
                    "attachments": json.dumps(m["attachments"]) if m["attachments"] else None,
                    "raw_file_id": raw_file_id,
                })
                n += 1
        return n

    def timeline_rows(self, conn, raw_file_id: int | None) -> Iterable[Mapping]:
        sql = """
        SELECT m.id, m.ts, m.role, m.text, m.attachments, m.provider, c.title, c.url
        FROM llm_messages m JOIN llm_conversations c ON c.id = m.conversation_id
        WHERE (:raw IS NULL OR m.raw_file_id = :raw)
        """
        for r in conn.execute(sql, {"raw": raw_file_id}):
            if r["role"] not in TIMELINE_ROLES:
                continue
            who = "You" if r["role"] == "user" else ASSISTANT.get(r["provider"], r["provider"])
            body = r["text"] or ""
            for a in json.loads(r["attachments"] or "[]"):
                body += f"\n\n[attachment: {a.get('name') or 'untitled'}]"
                if a.get("text"):
                    body += "\n" + a["text"]
            yield {
                "source_key": r["id"],
                "ts": r["ts"],
                "title": f"{who}: {r['title'] or 'Untitled chat'}",
                "body": body,
                "url": r["url"],
            }
