"""Store current notebook pages and owner-only memory records in SQLite."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import closing
from datetime import datetime, timezone

from .memory_queue import MemoryQueue


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def catalog_text(pages):
    return "[记事本目录]\n" + encode(pages) + "\n[/记事本目录]"


def page_stamp(page):
    return digest(
        encode([page[key] for key in ("page_id", "title", "description", "content")])
    )


class Store(MemoryQueue):
    """Keep legacy queues while adding transactional pages and source records."""

    def __init__(self, path):
        super().__init__(path)
        self.path.chmod(0o600)
        self.path.parent.chmod(0o700)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS notebook_pages (
                    page_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL,
                    title TEXT NOT NULL, description TEXT NOT NULL, content TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS notebook_persona ON notebook_pages(persona_id);
                CREATE TABLE IF NOT EXISTS memory_records (
                    id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, kind TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL,
                    original TEXT NOT NULL, content TEXT NOT NULL,
                    sources TEXT NOT NULL, scope TEXT, last_message_id INTEGER,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS records_persona ON memory_records(persona_id, created_at);
                CREATE TABLE IF NOT EXISTS imported_backups (
                    source_key TEXT PRIMARY KEY, record_id TEXT
                );
                CREATE TABLE IF NOT EXISTS original_tool_messages (
                    source_key TEXT PRIMARY KEY, persona_id TEXT NOT NULL,
                    origin TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notebook_memory_reads (
                    persona_id TEXT NOT NULL, page_id TEXT NOT NULL, stamp TEXT NOT NULL,
                    PRIMARY KEY(persona_id,page_id)
                );
            """)

    @staticmethod
    def _pages(db, persona_id):
        return [
            dict(row)
            for row in db.execute(
                "SELECT page_id,title,description FROM notebook_pages WHERE persona_id=? ORDER BY rowid",
                (persona_id,),
            )
        ]

    @staticmethod
    def _validate_page(page):
        for key, maximum in (("title", 24), ("description", 60), ("content", 12000)):
            value = page[key]
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError("invalid_page_content")

    @staticmethod
    def _validate_catalog(pages, limits):
        if len(pages) > limits.max_pages:
            raise ValueError("notebook_full")
        if len(catalog_text(pages).encode()) > limits.catalog_bytes:
            raise ValueError("catalog_budget_exceeded")

    def catalog(self, persona_id, limits):
        with closing(self._connect()) as db:
            pages = self._pages(db, persona_id)
        return {
            "pages": pages,
            "page_count": len(pages),
            "max_pages": limits.max_pages,
            "remaining_pages": max(0, limits.max_pages - len(pages)),
            "catalog_bytes": len(catalog_text(pages).encode()),
        }

    def page(self, persona_id, page_id):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM notebook_pages WHERE persona_id=? AND page_id=?",
                (persona_id, page_id),
            ).fetchone()
        if row is None:
            raise ValueError("page_not_found")
        return dict(row)

    def notebook_sources(self, persona_id):
        """Snapshot new/changed pages; acknowledge only with a committed memory record."""
        with closing(self._connect()) as db:
            pages = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM notebook_pages WHERE persona_id=? ORDER BY rowid",
                    (persona_id,),
                )
            ]
            seen = {
                row["page_id"]: row["stamp"]
                for row in db.execute(
                    "SELECT page_id,stamp FROM notebook_memory_reads WHERE persona_id=?",
                    (persona_id,),
                )
            }
        return [
            {
                "source_type": "notebook",
                "role": "notebook",
                "page_id": page["page_id"],
                "page_stamp": page_stamp(page),
                "title": page["title"],
                "description": page["description"],
                "text": page["content"],
                "sender_name": "记事本《" + page["title"] + "》",
                "sender_id": "",
                "origin": "notebook:" + persona_id,
                "time": now(),
            }
            for page in pages
            if seen.get(page["page_id"]) != page_stamp(page)
        ]

    def create_page(self, persona_id, title, description, content, limits):
        page = {
            "page_id": uuid.uuid4().hex[:16],
            "title": title,
            "description": description,
            "content": content,
        }
        self._validate_page(page)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            pages = self._pages(db, persona_id)
            public = {key: page[key] for key in ("page_id", "title", "description")}
            self._validate_catalog([*pages, public], limits)
            db.execute(
                "INSERT INTO notebook_pages VALUES(?,?,?,?,?)",
                (page["page_id"], persona_id, title, description, content),
            )
        return {**public, "remaining_pages": max(0, limits.max_pages - len(pages) - 1)}

    def change_page(
        self, persona_id, page_id, expected, fields, limits, *, delete=False
    ):
        """Use a private content digest; expose only current data to callers."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM notebook_pages WHERE persona_id=? AND page_id=?",
                (persona_id, page_id),
            ).fetchone()
            if row is None:
                raise ValueError("page_not_found")
            page = dict(row)
            if not expected or page_stamp(page) != expected:
                raise ValueError("page_changed")
            if delete:
                db.execute(
                    "DELETE FROM notebook_pages WHERE persona_id=? AND page_id=?",
                    (persona_id, page_id),
                )
            else:
                if not fields or not set(fields).issubset(
                    {"title", "description", "content"}
                ):
                    raise ValueError("invalid_page_content")
                page.update(fields)
                self._validate_page(page)
                pages = self._pages(db, persona_id)
                candidate = [
                    {key: page[key] for key in ("page_id", "title", "description")}
                    if item["page_id"] == page_id
                    else item
                    for item in pages
                ]
                # Lowering the page cap must not disable edits of existing pages.
                if len(catalog_text(candidate).encode()) > limits.catalog_bytes:
                    raise ValueError("catalog_budget_exceeded")
                db.execute(
                    "UPDATE notebook_pages SET title=?,description=?,content=? WHERE page_id=? AND persona_id=?",
                    (
                        page["title"],
                        page["description"],
                        page["content"],
                        page_id,
                        persona_id,
                    ),
                )
            count = db.execute(
                "SELECT COUNT(*) FROM notebook_pages WHERE persona_id=?", (persona_id,)
            ).fetchone()[0]
        return {
            "page_id": page_id,
            "page_count": count,
            "remaining_pages": max(0, limits.max_pages - count),
            **(
                {}
                if delete
                else {"title": page["title"], "description": page["description"]}
            ),
        }

    def stage(
        self,
        persona_id,
        original,
        content,
        kind,
        sources=None,
        scope=None,
        last_id=None,
    ):
        record_id = uuid.uuid4().hex
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO memory_records VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    persona_id,
                    kind,
                    "prepared",
                    now(),
                    original,
                    content,
                    encode(sources or []),
                    scope,
                    last_id,
                    "",
                ),
            )
        return record_id

    def stage_migration(
        self, persona_id, original, content, notes, limits, *, meta=None
    ):
        """Stage all imported pages and the source snapshot in one transaction."""
        meta = meta or {"kind": "migration"}
        kind = meta["kind"]
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT id FROM memory_records WHERE persona_id=? AND kind=? AND state='prepared' AND original=? AND content=?",
                (persona_id, kind, original, content),
            ).fetchone()
            if previous:
                return previous["id"]
            pages = self._pages(db, persona_id)
            additions = []
            title = "群聊黑话" if kind == "automatic" else "迁移记忆"
            description = (
                "群聊黑话的含义、适用对象与用法"
                if kind == "automatic"
                else "升级前保存的其他记忆，可按主题继续整理"
            )
            for offset in range(0, len(notes), 12000):
                number = offset // 12000 + 1
                page = {
                    "page_id": uuid.uuid4().hex[:16],
                    "title": title if len(notes) <= 12000 else f"{title} {number}",
                    "description": description,
                    "content": notes[offset : offset + 12000],
                }
                additions.append(page)
            public = [
                {key: page[key] for key in ("page_id", "title", "description")}
                for page in additions
            ]
            self._validate_catalog([*pages, *public], limits)
            for page in additions:
                db.execute(
                    "INSERT INTO notebook_pages VALUES(?,?,?,?,?)",
                    (
                        page["page_id"],
                        persona_id,
                        page["title"],
                        page["description"],
                        page["content"],
                    ),
                )
            record_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO memory_records VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    persona_id,
                    kind,
                    "prepared",
                    now(),
                    original,
                    content,
                    encode(meta.get("sources") or []),
                    meta.get("scope"),
                    meta.get("last_id"),
                    "",
                ),
            )
        return record_id

    def finish(self, record_id):
        """Commit a memory result and acknowledge its batch atomically."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM memory_records WHERE id=?", (record_id,)
            ).fetchone()
            if row is None or row["state"] != "prepared":
                return
            db.execute(
                "UPDATE memory_records SET state='committed' WHERE id=?", (record_id,)
            )
            if row["scope"] is not None and row["last_message_id"] is not None:
                db.execute(
                    "UPDATE messages SET done=1 WHERE scope=? AND id<=? AND done=0",
                    (row["scope"], row["last_message_id"]),
                )
            if row["kind"] == "automatic":
                for source in json.loads(row["sources"]):
                    if source.get("source_type") == "notebook":
                        db.execute(
                            "INSERT INTO notebook_memory_reads VALUES(?,?,?) ON CONFLICT(persona_id,page_id) DO UPDATE SET stamp=excluded.stamp",
                            (
                                row["persona_id"],
                                source["page_id"],
                                source["page_stamp"],
                            ),
                        )

    def fail(self, record_id, error):
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE memory_records SET state='failed',error=? WHERE id=? AND state='prepared'",
                (error, record_id),
            )

    def record(self, persona_id, record_id):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM memory_records WHERE persona_id=? AND id=?",
                (persona_id, record_id),
            ).fetchone()
        if row is None:
            raise ValueError("record_not_found")
        value = dict(row)
        value["sources"] = json.loads(value["sources"])
        value["changed"] = value["original"] != value["content"]
        return value

    def records(self, persona_id, offset=0, limit=30):
        with closing(self._connect()) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,kind,state,created_at,error,original!=content AS changed FROM memory_records WHERE persona_id=? ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?",
                    (persona_id, limit, offset),
                )
            ]

    def prepared(self, persona_id):
        with closing(self._connect()) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM memory_records WHERE persona_id=? AND state='prepared' ORDER BY rowid",
                    (persona_id,),
                )
            ]

    def latest_content(self, persona_id):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT content FROM memory_records WHERE persona_id=? AND state='committed' AND kind!='legacy_backup' ORDER BY rowid DESC LIMIT 1",
                (persona_id,),
            ).fetchone()
        return row[0] if row else None

    def import_backup(self, persona_id, text, source_name):
        """Import a legacy snapshot once, without inventing chat evidence or edit time."""
        key = digest(encode([persona_id, source_name, text]))
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM imported_backups WHERE source_key=?", (key,)
            ).fetchone():
                return
            known = db.execute(
                "SELECT 1 FROM memory_records WHERE persona_id=? AND content=?",
                (persona_id, text),
            ).fetchone()
            record_id = None
            if not known:
                record_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO memory_records VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record_id,
                        persona_id,
                        "legacy_backup",
                        "committed",
                        now(),
                        text,
                        text,
                        "[]",
                        None,
                        None,
                        "",
                    ),
                )
            db.execute("INSERT INTO imported_backups VALUES(?,?)", (key, record_id))

    def progress(self, persona_id):
        with closing(self._connect()) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT s.origin,COUNT(m.id) AS pending FROM scopes s LEFT JOIN messages m ON m.scope=s.id AND m.done=0 WHERE s.persona_id=? GROUP BY s.id",
                    (persona_id,),
                )
            ]

    def archive_tool_projection(self, persona_id, origin, originals, projected):
        """Retain affected original messages once if the agent persists its projection."""
        with closing(self._connect()) as db, db:
            for original, view in zip(originals, projected):
                if original == view:
                    continue
                raw = encode(original)
                key = digest(encode([persona_id, origin, original]))
                db.execute(
                    "INSERT OR IGNORE INTO original_tool_messages VALUES(?,?,?,?)",
                    (key, persona_id, origin, raw),
                )
