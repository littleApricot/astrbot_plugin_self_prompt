"""Check that oversized history remains complete across model requests."""

import json
import unittest

from astrbot_plugin_self_prompt.auto_memory import apply_summary, history_chunks
from astrbot_plugin_self_prompt.prompt_document import read_sections


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

    def test_updates_preserve_other_sections_and_original_for_no_change(self):
        original = "Existing persona"
        self.assertEqual(apply_summary(original, '{"updates":{}}'), original)
        updated = apply_summary(
            original,
            json.dumps(
                {
                    "updates": {
                        "output_preferences": "Short replies",
                        "persona": "New character",
                    }
                }
            ),
        )
        self.assertEqual(
            read_sections(updated),
            {
                "persona": "New character",
                "output_preferences": "Short replies",
                "memory": "",
                "nicknames": "[]",
            },
        )

    def test_bad_output_and_reserved_markers_are_rejected(self):
        for text in [
            "[]",
            "{}",
            '{"updates":[]}',
            '{"updates":{"persona":null}}',
            '{"updates":{"memory":"<!-- self_prompt:v1 -->"}}',
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                apply_summary("original", text)
