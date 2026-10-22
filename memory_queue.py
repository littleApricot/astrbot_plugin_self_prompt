"""Persist pending chat messages without changing AstrBot conversation history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


class MemoryQueue:
    """Keep independent, restart-safe batches for each session and persona."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scopes (
                    id TEXT PRIMARY KEY, origin TEXT NOT NULL,
                    persona_id TEXT NOT NULL, provider_id TEXT NOT NULL DEFAULT '',
                    model TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL, message_key TEXT NOT NULL,
                    payload TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(scope, message_key)
                );
                CREATE INDEX IF NOT EXISTS pending_messages
                    ON messages(scope, done, id);
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def append(
        self,
        origin: str,
        persona_id: str,
        message_key: str,
        payload: dict,
        provider_id: str = "",
        model: str | None = None,
    ) -> str:
        """Append once, retaining recent consumed IDs to reject redelivery."""
        scope = hashlib.sha256(json.dumps([origin, persona_id]).encode()).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO scopes(id, origin, persona_id) VALUES(?,?,?)",
                (scope, origin, persona_id),
            )
            db.execute(
                "UPDATE scopes SET provider_id=?, model=? WHERE id=?",
                (provider_id, model, scope),
            )
            db.execute(
                "INSERT OR IGNORE INTO messages(scope,message_key,payload) VALUES(?,?,?)",
                (scope, message_key, json.dumps(payload, ensure_ascii=False)),
            )
        return scope

    def ready(self, size: int) -> list[dict]:
        """Return scopes with at least one complete batch, oldest first."""
        with closing(self._connect()) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT s.* FROM scopes s JOIN messages m ON m.scope=s.id "
                    "WHERE m.done=0 GROUP BY s.id HAVING COUNT(*)>=? ORDER BY MIN(m.id)",
                    (size,),
                )
            ]

    def batch(self, scope: str, size: int) -> list[dict]:
        """Read the oldest unprocessed messages in chronological order."""
        with closing(self._connect()) as db:
            return [
                {"id": row["id"], **json.loads(row["payload"])}
                for row in db.execute(
                    "SELECT id,payload FROM messages WHERE scope=? AND done=0 "
                    "ORDER BY id LIMIT ?",
                    (scope, size),
                )
            ]

    def complete(self, scope: str, last_id: int) -> None:
        """Advance only after success and remove processed message contents."""
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE messages SET done=1,payload='{}' WHERE scope=? AND id<=?",
                (scope, last_id),
            )
            db.execute(
                "DELETE FROM messages WHERE scope=? AND done=1 AND id NOT IN "
                "(SELECT id FROM messages WHERE scope=? AND done=1 "
                "ORDER BY id DESC LIMIT 2000)",
                (scope, scope),
            )

    def pending_count(self) -> int:
        """Return the number of unprocessed messages across all scopes."""
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM messages WHERE done=0").fetchone()[
                0
            ]
