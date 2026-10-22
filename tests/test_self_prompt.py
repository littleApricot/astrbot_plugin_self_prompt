"""Verify section edits, real persona persistence and next-request injection."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image
from astrbot.core.astr_main_agent import _ensure_persona_and_skills
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.persona_mgr import PersonaManager
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
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
        await self.plugin.terminate()
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

    def auto_context(self, result='{"updates": {}}'):
        provider = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=LLMResponse("assistant", completion_text=result)
            )
        )
        self.context.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value=None),
        )
        self.context.get_using_provider_async = AsyncMock(return_value=provider)
        self.context.get_provider_by_id = MagicMock(return_value=provider)
        return provider

    def message_event(self, number, origin="test:GroupMessage:group-1", text=None):
        extras = {}
        return SimpleNamespace(
            unified_msg_origin=origin,
            message_obj=SimpleNamespace(message_id=str(number)),
            get_platform_name=lambda: "qq_official",
            get_sender_id=lambda: "user-1",
            get_self_id=lambda: "bot-1",
            get_sender_name=lambda: "Alice",
            get_message_outline=lambda: text or f"history message {number}",
            get_extra=lambda key, default=None: extras.get(key, default),
            set_extra=lambda key, value: extras.__setitem__(key, value),
        )

    async def wait_pending(self, expected):
        async def wait():
            while self.plugin.memory_queue.pending_count() != expected:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), timeout=3)

    async def test_100_messages_trigger_background_save_and_next_request_load(self):
        provider = self.auto_context(
            '{"updates": {"memory": "Alice likes blue cats."}}'
        )
        self.req.contexts = [{"role": "user", "content": "Keep normal history"}]
        for number in range(99):
            await self.plugin.collect_history(self.message_event(number))
        await asyncio.sleep(0.02)
        provider.text_chat.assert_not_awaited()
        await self.plugin.collect_history(self.message_event(99))
        await self.wait_pending(0)
        provider.text_chat.assert_awaited_once()
        call = provider.text_chat.call_args.kwargs
        history = json.loads(call["prompt"])["history"]
        self.assertEqual(len(history), 100)
        self.assertEqual(history[0]["text"], "history message 0")
        self.assertEqual(history[-1]["text"], "history message 99")
        self.assertIsNone(call["func_tool"])
        self.assertEqual(call["contexts"], [])
        self.assertEqual(self.req.contexts[0]["content"], "Keep normal history")
        fresh = PersonaManager(self.db, self.acm)
        await fresh.initialize()
        self.context.persona_manager = fresh
        request = ProviderRequest(conversation=SimpleNamespace(persona_id=None))
        with patch("astrbot.core.astr_main_agent.SkillManager") as skills:
            skills.return_value.list_skills.return_value = []
            await _ensure_persona_and_skills(request, {}, self.context, self.event)
        self.assertIn("Alice likes blue cats.", request.system_prompt)

    async def test_final_replies_count_once_but_tools_and_chunks_do_not(self):
        provider = self.auto_context()
        for number in range(50):
            event = self.message_event(number)
            await self.plugin.collect_history(event)
            await self.plugin.collect_history(event)
            await self.plugin.collect_reply(
                event,
                LLMResponse(
                    "assistant",
                    completion_text="tool plan",
                    tools_call_name=["search"],
                ),
            )
            chunk = LLMResponse("assistant", completion_text="partial")
            chunk.is_chunk = True
            await self.plugin.collect_reply(event, chunk)
            reply = LLMResponse("assistant", completion_text=f"reply {number}")
            await self.plugin.collect_reply(event, reply)
            await self.plugin.collect_reply(event, reply)
        await self.wait_pending(0)
        provider.text_chat.assert_awaited_once()
        history = json.loads(provider.text_chat.call_args.kwargs["prompt"])["history"]
        self.assertEqual(sum(r["role"] == "user" for r in history), 50)
        self.assertEqual(sum(r["role"] == "assistant" for r in history), 50)
        # Redelivery after completion does not start another batch.
        await self.plugin.collect_history(self.message_event(0))
        self.assertEqual(self.plugin.memory_queue.pending_count(), 0)

    async def test_pending_and_completed_batches_survive_plugin_restart(self):
        provider = self.auto_context()
        for number in range(99):
            await self.plugin.collect_history(self.message_event(number))
        await self.plugin.terminate()
        with patch(
            "astrbot_plugin_self_prompt.main.StarTools.get_data_dir",
            return_value=self.root / "data",
        ):
            self.plugin = SelfPrompt(self.context)
        await self.plugin.initialize()
        await self.plugin.collect_history(self.message_event(99))
        await self.wait_pending(0)
        self.assertEqual(provider.text_chat.await_count, 1)
        await self.plugin.terminate()
        with patch(
            "astrbot_plugin_self_prompt.main.StarTools.get_data_dir",
            return_value=self.root / "data",
        ):
            self.plugin = SelfPrompt(self.context)
        await self.plugin.initialize()
        await asyncio.sleep(0.02)
        self.assertEqual(provider.text_chat.await_count, 1)
        await self.plugin.collect_history(self.message_event(100))
        self.assertEqual(self.plugin.memory_queue.pending_count(), 1)

    async def test_separate_sessions_and_personas_do_not_mix_their_counts(self):
        provider = self.auto_context()
        for number in range(60):
            await self.plugin.collect_history(
                self.message_event(number, origin="group-a")
            )
            await self.plugin.collect_history(
                self.message_event(number, origin="group-b")
            )
        await self.manager.create_persona("other", "Other persona")
        self.override = {"persona_id": "other"}
        for number in range(60, 99):
            await self.plugin.collect_history(
                self.message_event(number, origin="group-a")
            )
        await asyncio.sleep(0.02)
        provider.text_chat.assert_not_awaited()
        self.assertEqual(len(self.plugin.memory_queue.ready(100)), 0)
        self.assertEqual(self.plugin.memory_queue.pending_count(), 159)

    async def test_failure_keeps_batch_and_later_retry_processes_exactly_100(self):
        provider = self.auto_context()
        provider.text_chat.side_effect = RuntimeError("simulated failure")
        with patch.object(self.plugin, "_start_worker"):
            for number in range(101):
                await self.plugin.collect_history(self.message_event(number))
        await self.plugin._process_ready()
        self.assertEqual(self.plugin.memory_queue.pending_count(), 101)
        provider.text_chat.side_effect = None
        self.plugin._retry_after.clear()
        await self.plugin._process_ready()
        self.assertEqual(self.plugin.memory_queue.pending_count(), 1)
        self.assertEqual(provider.text_chat.await_count, 2)

    async def test_manual_edits_during_summary_are_preserved_and_batch_is_not_acked(
        self,
    ):
        provider = self.auto_context()

        async def edit_while_running(**kwargs):
            await self.manager.update_persona(
                "default", system_prompt="Manually updated"
            )
            return LLMResponse(
                "assistant", completion_text='{"updates":{"memory":"stale"}}'
            )

        provider.text_chat.side_effect = edit_while_running
        with patch.object(self.plugin, "_start_worker"):
            for number in range(100):
                await self.plugin.collect_history(self.message_event(number))
        await self.plugin._process_ready()
        self.assertEqual(
            (await self.manager.get_persona("default")).system_prompt,
            "Manually updated",
        )
        self.assertEqual(self.plugin.memory_queue.pending_count(), 100)

    async def test_invalid_result_cannot_erase_prompt_or_advance_progress(self):
        self.auto_context('{"updates":{"unknown":"bad"}}')
        original = (await self.manager.get_persona("default")).system_prompt
        with patch.object(self.plugin, "_start_worker"):
            for number in range(100):
                await self.plugin.collect_history(self.message_event(number))
        await self.plugin._process_ready()
        self.assertEqual(
            (await self.manager.get_persona("default")).system_prompt, original
        )
        self.assertEqual(self.plugin.memory_queue.pending_count(), 100)

    async def test_disabled_auto_memory_keeps_manual_tools_available(self):
        provider = self.auto_context()
        self.plugin.config["auto_memory_enabled"] = False
        await self.plugin.collect_history(self.message_event(0))
        self.assertEqual(self.plugin.memory_queue.pending_count(), 0)
        current = json.loads(await self.plugin.get_self_prompt(self.event))
        result = json.loads(
            await self.plugin.update_self_prompt(
                self.event,
                "memory",
                "Still editable",
                current["revision"],
            )
        )
        self.assertTrue(result["ok"])
        provider.text_chat.assert_not_awaited()

    async def test_media_only_messages_count_without_sending_image_data_to_summary(
        self,
    ):
        provider = self.auto_context()
        for number in range(50):
            event = self.message_event(number, text="[图片]")
            await self.plugin.collect_history(event)
            await self.plugin.collect_reply(
                event,
                LLMResponse(
                    "assistant",
                    result_chain=MessageChain([Image(file="private-image.png")]),
                ),
            )
        await self.wait_pending(0)
        provider.text_chat.assert_awaited_once()
        prompt = provider.text_chat.call_args.kwargs["prompt"]
        self.assertNotIn("private-image.png", prompt)
        self.assertEqual(len(json.loads(prompt)["history"]), 100)

    async def test_oversized_batch_uses_multiple_calls_without_losing_old_or_new_text(
        self,
    ):
        provider = self.auto_context()
        expected = []
        with patch.object(self.plugin, "_start_worker"):
            for number in range(100):
                text = ("long fact " * 8000) if number == 40 else f"message {number}"
                expected.append(text)
                await self.plugin.collect_history(self.message_event(number, text=text))
        with patch("astrbot_plugin_self_prompt.main.INPUT_CHAR_LIMIT", 8000):
            await self.plugin._process_ready()
        self.assertGreater(provider.text_chat.await_count, 1)
        actual = "".join(
            row["text"]
            for call in provider.text_chat.call_args_list
            for row in json.loads(call.kwargs["prompt"])["history"]
        )
        self.assertEqual(actual, "".join(expected))
        self.assertEqual(self.plugin.memory_queue.pending_count(), 0)
