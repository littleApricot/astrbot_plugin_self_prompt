"""Verify transactional capacity, source retention and current-only page storage."""

import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_self_prompt.memory_queue import MemoryQueue
from astrbot_plugin_self_prompt.settings import Settings
from astrbot_plugin_self_prompt.store import Store, catalog_text, page_stamp
from astrbot_plugin_self_prompt.upgrade_backup import backup_legacy


class StoreTests(unittest.TestCase):
    def test_upgrade_backup_precedes_schema_changes_and_survives_restarts(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            queue = MemoryQueue(data / "auto_memory.sqlite3")
            queue.append("room", "p", "1", {"text": "original source"})
            (data / "backups").mkdir()
            (data / "backups/old.txt").write_text("original prompt")
            backup_legacy(data, {"auto_memory_enabled": False})
            Store(queue.path)
            queue.append("room", "p", "2", {"text": "new source"})
            backup_legacy(data, {"auto_memory_enabled": True})
            snapshot = data / "upgrade_v0_4_backup"
            self.assertEqual(MemoryQueue(snapshot / queue.path.name).pending_count(), 1)
            self.assertEqual(
                (snapshot / "backups/old.txt").read_text(), "original prompt"
            )
            self.assertFalse(
                json.loads((snapshot / "config.json").read_text())[
                    "auto_memory_enabled"
                ]
            )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "memory.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_concurrent_last_slot_and_lowering_cap(self):
        limits = Settings(max_pages=1)

        def create(number):
            try:
                self.store.create_page("p", str(number), "purpose", "body", limits)
                return True
            except ValueError as exc:
                self.assertEqual(str(exc), "notebook_full")
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(create, range(4))), 1)
        page = self.store.catalog("p", limits)["pages"][0]
        full = self.store.page("p", page["page_id"])
        self.store.change_page(
            "p", page["page_id"], page_stamp(full), {"content": "updated"}, limits
        )
        self.assertEqual(self.store.page("p", page["page_id"])["content"], "updated")

    def test_current_data_is_removed_and_capacity_freed(self):
        limits = Settings(max_pages=1)
        result = self.store.create_page(
            "p", "Facts", "Purpose", "discard this body", limits
        )
        page = self.store.page("p", result["page_id"])
        self.store.change_page(
            "p", page["page_id"], page_stamp(page), {}, limits, delete=True
        )
        with self.assertRaisesRegex(ValueError, "page_not_found"):
            self.store.page("p", page["page_id"])
        self.assertEqual(self.store.catalog("p", limits)["remaining_pages"], 1)
        with self.store._connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM notebook_pages").fetchone()[0], 0
            )

    def test_index_budget_checks_entire_catalog_and_persona_isolation(self):
        limits = Settings(catalog_bytes=180)
        self.store.create_page("p", "one", "short", "body", limits)
        with self.assertRaisesRegex(ValueError, "catalog_budget_exceeded"):
            self.store.create_page("p", "二" * 24, "文" * 60, "body", limits)
        self.assertEqual(self.store.catalog("q", limits)["page_count"], 0)
        pages = self.store.catalog("p", limits)["pages"]
        self.assertEqual(
            self.store.catalog("p", limits)["catalog_bytes"],
            len(catalog_text(pages).encode()),
        )

    def test_source_queue_old_schema_survives_and_new_records_keep_text(self):
        scope = self.store.append("room", "p", "m1", {"role": "user", "text": "source"})
        record_id = self.store.stage(
            "p", "old", "new", "automatic", self.store.batch(scope, 1), scope, 1
        )
        self.store.finish(record_id)
        reopened = Store(self.store.path)
        self.assertEqual(reopened.pending_count(), 0)
        self.assertEqual(
            reopened.record("p", record_id)["sources"][0]["text"], "source"
        )
        with reopened._connect() as db:
            self.assertEqual(
                json.loads(db.execute("SELECT payload FROM messages").fetchone()[0])[
                    "text"
                ],
                "source",
            )
        reopened.append("room", "p", "m1", {"text": "redelivery"})
        self.assertEqual(reopened.pending_count(), 0)

    def test_invalid_settings_do_not_become_unlimited(self):
        for key in (
            "auto_memory_message_interval",
            "notebook_max_pages",
            "long_term_memory_max_bytes",
            "notebook_catalog_max_bytes",
        ):
            for value in (0, -1, True, "100", None):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    Settings.read({key: value})

    def test_notebook_sources_acknowledge_only_successful_snapshots(self):
        page = self.store.create_page(
            "p", "Terms", "Names and slang", "old nickname", Settings()
        )
        sources = self.store.notebook_sources("p")
        self.assertEqual(len(sources), 1)
        failed = self.store.stage("p", "before", "before", "automatic", sources)
        self.store.fail(failed, "test failure")
        self.assertEqual(self.store.notebook_sources("p")[0]["text"], "old nickname")
        full = self.store.page("p", page["page_id"])
        self.store.change_page(
            "p",
            page["page_id"],
            page_stamp(full),
            {"content": "new nickname"},
            Settings(),
        )
        committed = self.store.stage("p", "same", "same", "automatic", sources)
        self.store.finish(committed)
        self.assertEqual(self.store.notebook_sources("p")[0]["text"], "new nickname")
        latest = self.store.notebook_sources("p")
        final = self.store.stage("p", "same", "same", "automatic", latest)
        self.store.finish(final)
        self.assertEqual(Store(self.store.path).notebook_sources("p"), [])
        current = self.store.page("p", page["page_id"])
        self.store.change_page(
            "p", page["page_id"], page_stamp(current), {}, Settings(), delete=True
        )
        self.assertEqual(
            self.store.record("p", final)["sources"][0]["text"], "new nickname"
        )
        self.assertEqual(self.store.notebook_sources("p"), [])
