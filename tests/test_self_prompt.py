"""Verify section edits, real persona persistence and next-request injection."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from astrbot.core.astr_main_agent import _ensure_persona_and_skills
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.persona_mgr import PersonaManager
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_self_prompt.main import SelfPrompt
from astrbot_plugin_self_prompt.prompt_document import (
    END,
    START,
    read_sections,
    replace_section,
    revision,
)


class DocumentTests(unittest.TestCase):
    def test_legacy_prompt_is_preserved_when_adding_preferences(self):
        original = "Original persona\n\nNickname: from context\n"
        prompt = replace_section(original, "output_preferences", "Use short replies.")
        self.assertEqual(read_sections(prompt)["persona"], original)
        self.assertEqual(
            read_sections(prompt)["output_preferences"], "Use short replies."
        )
        self.assertEqual(read_sections(prompt)["memory"], "")

    def test_edit_preserves_other_sections_and_external_text(self):
        prompt = replace_section("Original persona", "memory", "Remember blue cats.")
        prompt = "Additional instructions\n" + prompt + "\nFooter"
        updated = replace_section(
            prompt, "output_preferences", "## Heading\nKeep it brief."
        )
        self.assertEqual(read_sections(updated)["persona"], "Original persona")
        self.assertEqual(read_sections(updated)["memory"], "Remember blue cats.")
        self.assertTrue(updated.startswith("Additional instructions\n"))
        self.assertTrue(updated.endswith("\nFooter"))
        self.assertNotEqual(revision(prompt), revision(updated))
        cleared = replace_section(updated, "memory", "")
        self.assertEqual(read_sections(cleared)["memory"], "")

    def test_invalid_document_and_content_fail_without_rebuilding(self):
        for prompt in [START, END, START + END, START + START + END]:
            with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                replace_section(prompt, "memory", "new")
        with self.assertRaises(ValueError):
            replace_section("old", "other", "new")
        with self.assertRaises(ValueError):
            replace_section("old", "memory", START)


class PersonaToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="self-prompt-test-")
        self.root = Path(self.temp.name)
        self.db = SQLiteDatabase(str(self.root / "personas.db"))
        self.conf = {
            "agent_runner": {
                "runner_type": "local",
                "config": {"persona": {"persona_id": "default"}},
            },
            "provider_settings": {},
        }
        self.acm = SimpleNamespace(
            default_conf=self.conf, get_conf=lambda *args, **kwargs: self.conf
        )
        self.manager = PersonaManager(self.db, self.acm)
        await self.manager.initialize()
        self.context = SimpleNamespace(
            persona_manager=self.manager,
            get_config=lambda *args, **kwargs: self.conf,
            get_llm_tool_manager=lambda: llm_tools,
            subagent_orchestrator=None,
        )
        self.override = {}
        self.sp_patch = patch(
            "astrbot.core.persona_mgr.sp.get_async",
            AsyncMock(side_effect=lambda **kwargs: self.override),
        )
        self.sp_patch.start()
        with patch(
            "astrbot_plugin_self_prompt.main.StarTools.get_data_dir",
            return_value=self.root / "data",
        ):
            self.plugin = SelfPrompt(self.context)
        self.extra = {}
        self.event = SimpleNamespace(
            unified_msg_origin="test:GroupMessage:group-1",
            get_platform_name=lambda: "qq_official",
            get_extra=lambda key, default=None: self.extra.get(key, default),
            set_extra=lambda key, value: self.extra.__setitem__(key, value),
            trace=MagicMock(),
        )
        self.req = ProviderRequest(
            prompt="hello", conversation=SimpleNamespace(persona_id=None)
        )
        await self.plugin.prepare_prompt_tools(self.event, self.req)

    async def asyncTearDown(self):
        self.sp_patch.stop()
        await self.db.engine.dispose()
        self.temp.cleanup()

    async def test_registered_tools_are_exposed_with_the_three_required_arguments(self):
        tool = llm_tools.get_func("update_self_prompt")
        self.assertIsNotNone(tool)
        self.assertEqual(
            set(tool.parameters["properties"]),
            {"section", "content", "expected_revision"},
        )
        self.assertEqual(
            set(tool.parameters["required"]), set(tool.parameters["properties"])
        )
        request = ProviderRequest(conversation=SimpleNamespace(persona_id=None))
        with patch("astrbot.core.astr_main_agent.SkillManager") as skills:
            skills.return_value.list_skills.return_value = []
            await _ensure_persona_and_skills(request, {}, self.context, self.event)
        self.assertIn("get_self_prompt", request.func_tool.names())
        self.assertIn("update_self_prompt", request.func_tool.names())

    async def test_read_does_not_modify_the_original_persona(self):
        before = (await self.manager.get_persona("default")).system_prompt
        result = json.loads(await self.plugin.get_self_prompt(self.event))
        self.assertTrue(result["ok"])
        self.assertEqual(result["sections"]["persona"], before)
        self.assertEqual(
            (await self.manager.get_persona("default")).system_prompt, before
        )
        self.assertIn("自维护提示词", self.req.system_prompt)

    async def test_three_sections_persist_and_are_loaded_into_the_next_request(self):
        expected = {
            "persona": "My nickname is Echo, and I am calm.",
            "output_preferences": "Prefer short Chinese replies.",
            "memory": "The user's cat is named Blue.",
        }
        for section, content in expected.items():
            current = json.loads(await self.plugin.get_self_prompt(self.event))
            saved = json.loads(
                await self.plugin.update_self_prompt(
                    self.event, section, content, current["revision"]
                )
            )
            self.assertTrue(saved["ok"])
        fresh = PersonaManager(self.db, self.acm)
        await fresh.initialize()
        self.assertEqual(
            read_sections((await fresh.get_persona("default")).system_prompt), expected
        )
        self.context.persona_manager = fresh
        next_request = ProviderRequest(conversation=SimpleNamespace(persona_id=None))
        with patch("astrbot.core.astr_main_agent.SkillManager") as skills:
            skills.return_value.list_skills.return_value = []
            await _ensure_persona_and_skills(next_request, {}, self.context, self.event)
        for content in expected.values():
            self.assertIn(content, next_request.system_prompt)
        backups = list((self.root / "data/backups").rglob("*.txt"))
        self.assertEqual(len(backups), 3)

    async def test_stale_and_concurrent_edits_do_not_erase_new_information(self):
        current = json.loads(await self.plugin.get_self_prompt(self.event))
        results = await asyncio.gather(
            self.plugin.update_self_prompt(
                self.event, "memory", "Keep this fact.", current["revision"]
            ),
            self.plugin.update_self_prompt(
                self.event, "output_preferences", "Be brief.", current["revision"]
            ),
        )
        results = [json.loads(result) for result in results]
        self.assertEqual(sum(bool(result["ok"]) for result in results), 1)
        self.assertIn("revision_conflict", [result.get("error") for result in results])
        latest = json.loads(await self.plugin.get_self_prompt(self.event))
        self.assertEqual(latest["sections"]["memory"], "Keep this fact.")

    async def test_selected_persona_and_existing_tool_settings_are_preserved(self):
        await self.manager.create_persona("other", "Different persona")
        await self.manager.update_persona("other", tools=["get_self_prompt"], skills=[])
        self.override = {"persona_id": "other"}
        await self.plugin.prepare_prompt_tools(self.event, self.req)
        current = json.loads(await self.plugin.get_self_prompt(self.event))
        self.assertEqual(current["persona_id"], "other")
        saved = json.loads(
            await self.plugin.update_self_prompt(
                self.event, "memory", "Other memory", current["revision"]
            )
        )
        self.assertTrue(saved["ok"])
        other = await self.manager.get_persona("other")
        self.assertEqual(other.tools, ["get_self_prompt"])
        self.assertEqual(other.skills, [])
        self.assertNotIn(
            "Other memory", (await self.manager.get_persona("default")).system_prompt
        )

    async def test_disabled_persona_and_invalid_document_do_not_write(self):
        self.extra.clear()
        self.req.conversation.persona_id = "[%None]"
        await self.plugin.prepare_prompt_tools(self.event, self.req)
        self.assertFalse(
            json.loads(await self.plugin.get_self_prompt(self.event))["ok"]
        )
        self.assertFalse(
            json.loads(
                await self.plugin.update_self_prompt(
                    self.event, "memory", "new", "anything"
                )
            )["ok"]
        )
        self.extra["self_prompt_persona_id"] = "default"
        await self.manager.update_persona("default", system_prompt=START)
        result = json.loads(
            await self.plugin.update_self_prompt(
                self.event, "memory", "new", revision(START)
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(
            (await self.manager.get_persona("default")).system_prompt, START
        )
