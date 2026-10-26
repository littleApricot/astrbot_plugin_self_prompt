"""Specify owner and model write boundaries for the current section layout."""

import unittest

from astrbot_plugin_self_prompt.prompt_document import (
    END,
    FIXED_END,
    FIXED_START,
    LEGACY_END,
    LEGACY_START,
    SECTIONS,
    START,
    assert_editable_change,
    ensure_layout,
    read_sections,
    replace_section,
)
from astrbot_plugin_self_prompt.tests.support import legacy_prompt


class PromptOwnershipTests(unittest.TestCase):
    def test_plain_prompt_preserves_original_in_owner_persona(self):
        original = "Nickname: Echo.\nPreserve all original whitespace.  \n\n"
        migrated = ensure_layout(original)
        self.assertEqual(read_sections(migrated)["persona"], original)
        self.assertEqual(read_sections(migrated)["other"], "")
        self.assertEqual(ensure_layout(migrated), migrated)

    def test_v1_sections_and_surrounding_notes_survive_upgrade(self):
        values = {
            "persona": "Original persona",
            "output_preferences": "Short replies",
            "memory": "Remembered fact",
        }
        parts = ["Leading note", LEGACY_START]
        for section, value in values.items():
            parts.append(
                f"<!-- self_prompt:{section} -->\n{value}\n<!-- /self_prompt:{section} -->"
            )
        parts.extend([LEGACY_END, "Trailing note"])
        sections = read_sections("\n\n".join(parts))
        self.assertIn("Leading note", sections["persona"])
        self.assertIn("Original persona", sections["persona"])
        self.assertIn("Trailing note", sections["persona"])
        self.assertEqual(sections["output_preferences"], "Short replies")
        self.assertEqual(sections["other"], "Remembered fact")

    def test_v2_fixed_text_merges_into_owner_persona_once(self):
        old = legacy_prompt(
            fixed="Keep exact.  \n", persona="Original persona", memory="Old memory"
        )
        upgraded = ensure_layout(old)
        self.assertEqual(
            read_sections(upgraded)["persona"], "Keep exact.  \n\n\nOriginal persona"
        )
        self.assertEqual(read_sections(upgraded)["other"], "Old memory")
        self.assertNotIn(FIXED_START, upgraded)
        self.assertEqual(ensure_layout(upgraded), upgraded)

    def test_self_memory_and_nickname_writers_have_distinct_exact_boundaries(self):
        original = (
            "External prefix\n"
            + ensure_layout("Owner persona  \n")
            + "\nExternal suffix"
        )
        for allowed in ("other", "nicknames"):
            for key in SECTIONS:
                updated = replace_section(original, key, "New value")
                if key == allowed:
                    assert_editable_change(original, updated, (allowed,))
                else:
                    with self.assertRaisesRegex(
                        ValueError, "protected_sections_modified"
                    ):
                        assert_editable_change(original, updated, (allowed,))
            with self.assertRaisesRegex(ValueError, "protected_sections_modified"):
                assert_editable_change(
                    original, original.replace("External prefix", "Changed"), (allowed,)
                )

    def test_damaged_boundaries_are_rejected(self):
        prompt = ensure_layout("Owner")
        for bad in (
            prompt.replace(END, ""),
            prompt + END,
            prompt + FIXED_START + FIXED_END,
        ):
            with self.subTest(prompt=bad), self.assertRaises(ValueError):
                replace_section(bad, "other", "attempt")

    def test_model_cannot_inject_boundary_markers_in_any_field(self):
        prompt = ensure_layout("Owner")
        for section in SECTIONS:
            for marker in (START, END, FIXED_START, FIXED_END, LEGACY_START):
                with (
                    self.subTest(section=section, marker=marker),
                    self.assertRaises(ValueError),
                ):
                    replace_section(prompt, section, marker)
