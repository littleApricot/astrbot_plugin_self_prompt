"""Specify complete batching and other-only summary output."""

import json
import unittest

from astrbot_plugin_self_prompt.auto_memory import apply_summary, history_chunks
from astrbot_plugin_self_prompt.prompt_document import read_sections, replace_section


class SummaryFormatTests(unittest.TestCase):
    def test_long_messages_split_without_dropping_any_text(self):
        text = "early fact\n" + '长消息"\\\n' * 2000 + "\nlate correction"
        records = [{"id": 1, "role": "user", "sender_name": "Alice", "text": text}]
        chunks = history_chunks(records, 4000)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(
            "".join(row["text"] for chunk in chunks for row in chunk), text
        )
        for chunk in chunks:
            self.assertLessEqual(len(json.dumps(chunk, ensure_ascii=False)), 4000)

    def test_summary_updates_only_other_and_preserves_owner_and_nicknames(self):
        original = replace_section("Owner persona", "nicknames", '["Echo"]')
        self.assertEqual(apply_summary(original, '{"updates":{}}'), original)
        updated = apply_summary(
            original, '{"updates":{"other":"Remembered agreement"}}'
        )
        self.assertEqual(read_sections(updated)["other"], "Remembered agreement")
        for key in ("persona", "output_preferences", "nicknames"):
            self.assertEqual(read_sections(updated)[key], read_sections(original)[key])
        self.assertEqual(
            read_sections(apply_summary(updated, '{"updates":{"other":""}}'))["other"],
            "",
        )

    def test_bad_output_and_protected_updates_are_rejected(self):
        for text in (
            "[]",
            "{}",
            '{"updates":[]}',
            '{"updates":{"other":null}}',
            '{"updates":{"persona":"changed"}}',
            '{"updates":{"output_preferences":"changed"}}',
            '{"updates":{"nicknames":["changed"]}}',
            '{"updates":{"other":"<!-- self_prompt:v1 -->"}}',
            '{"updates":{},"notebook_transfers":[]}',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                apply_summary("original", text)
