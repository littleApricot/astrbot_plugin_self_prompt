"""Verify migration and the immutable boundary around owner instructions."""

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
    read_fixed,
    read_sections,
    replace_section,
)


class PromptOwnershipTests(unittest.TestCase):
    def test_plain_prompt_goes_to_editable_and_fixed_starts_empty(self):
        original = "Nickname: Echo.\nPreserve all original whitespace.  \n\n"
        migrated = ensure_layout(original)
        self.assertEqual(read_fixed(migrated), "")
        self.assertEqual(read_sections(migrated)["persona"], original)
        self.assertEqual(ensure_layout(migrated), migrated)

    def test_v1_sections_and_surrounding_notes_remain_editable(self):
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
        migrated = ensure_layout("\n\n".join(parts))
        self.assertEqual(read_fixed(migrated), "")
        sections = read_sections(migrated)
        self.assertIn("Leading note", sections["persona"])
        self.assertIn(values["persona"], sections["persona"])
        self.assertIn("Trailing note", sections["persona"])
        self.assertEqual(sections["output_preferences"], values["output_preferences"])
        self.assertEqual(sections["memory"], values["memory"])

    def test_fixed_text_and_every_byte_outside_editable_survive_all_updates(self):
        prompt = ensure_layout("Editable persona")
        fixed = "Keep this exact.  \n\nAn owner note.\n"
        prompt = prompt.replace(
            FIXED_START + "\n\n" + FIXED_END,
            FIXED_START + "\n" + fixed + "\n" + FIXED_END,
        )
        prompt = "External protected prefix\n" + prompt + "\nProtected suffix"
        updated = prompt
        for section in SECTIONS:
            updated = replace_section(updated, section, "Updated " + section)
            self.assertEqual(read_fixed(updated), fixed)
            assert_editable_change(prompt, updated)
        self.assertTrue(updated.startswith("External protected prefix\n"))
        self.assertTrue(updated.endswith("\nProtected suffix"))
        with self.assertRaisesRegex(ValueError, "fixed_prompt_modified"):
            assert_editable_change(
                prompt, updated.replace("Keep this exact.", "Changed.")
            )

    def test_fixed_markers_inside_editable_and_damaged_boundaries_are_rejected(self):
        prompt = ensure_layout("Editable")
        inside = prompt.replace(FIXED_START + "\n\n" + FIXED_END, "")
        inside = inside.replace(START, START + "\n" + FIXED_START + "\n\n" + FIXED_END)
        for bad in [
            inside,
            prompt.replace(END, ""),
            prompt + END,
            prompt.replace(FIXED_END, ""),
        ]:
            with self.subTest(prompt=bad), self.assertRaises(ValueError):
                replace_section(bad, "memory", "attempt")

    def test_model_cannot_inject_boundary_markers_in_any_editable_field(self):
        prompt = ensure_layout("Editable")
        for section in SECTIONS:
            for marker in (START, END, FIXED_START, FIXED_END, LEGACY_START):
                with (
                    self.subTest(section=section, marker=marker),
                    self.assertRaises(ValueError),
                ):
                    replace_section(prompt, section, marker)
