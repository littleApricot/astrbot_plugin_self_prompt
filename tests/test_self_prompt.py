"""Exercise the new plugin against native persona persistence and request objects."""

import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from astrbot.core.astr_main_agent import _ensure_persona_and_skills
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_self_prompt.core_memory import (
    current_memory,
    owner_edit,
    project_history,
)
from astrbot_plugin_self_prompt.main import SelfPrompt
from astrbot_plugin_self_prompt.prompt_document import (
    read_sections,
    replace_section,
)
from astrbot_plugin_self_prompt.tests.support import Sandbox


class PluginIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = await Sandbox().open()
        self.p = self.s.plugin

    async def asyncTearDown(self):
        await self.s.close()

    async def test_tools_and_request_have_no_owner_bookkeeping(self):
        self.assertFalse(hasattr(self.p, "update_self_prompt"))
        self.assertFalse(hasattr(self.p, "notebook_list"))
        for name in (
            "get_self_prompt",
            "notebook_read",
            "notebook_create",
            "notebook_update",
            "notebook_delete",
        ):
            tool = llm_tools.get_func(name)
            self.assertIsNotNone(tool)
            wire = json.dumps(
                {"parameters": tool.parameters, "description": tool.description}
            )
            self.assertNotRegex(wire, r"revision|version_id|版本")
        memory = json.loads(await self.p.get_self_prompt(self.s.event))
        self.assertEqual(set(memory), {"ok", "fixed_prompt", "sections"})
        self.assertEqual(set(memory["sections"]), {"persona", "output_preferences", "nicknames"})
        self.assertNotIn("self_prompt:", self.s.request.system_prompt)
        self.assertNotIn("版本", self.s.request.system_prompt)
        req = self.s.request
        with patch("astrbot.core.astr_main_agent.SkillManager") as skills:
            skills.return_value.list_skills.return_value = []
            await _ensure_persona_and_skills(req, {}, self.s.context, self.s.event)
        self.assertIn("notebook_create", req.func_tool.names())
        self.assertNotIn("update_self_prompt", req.func_tool.names())
        self.assertNotIn("notebook_list", req.func_tool.names())

    async def test_legacy_other_memory_moves_once_without_loss(self):
        old = replace_section("Nickname Echo", "memory", "原文" * 9000)
        # Supply a legacy prompt as if it had existed before plugin installation.
        await self.p._original_update("default", system_prompt=old)
        await self.p._ensure_layout("default")
        current = (await self.s.manager.get_persona("default")).system_prompt
        self.assertEqual(read_sections(current)["memory"], "")
        pages = self.p.store.catalog("default", self.p.settings())["pages"]
        self.assertEqual(len(pages), 2)
        self.assertEqual(
            "".join(
                self.p.store.page("default", row["page_id"])["content"] for row in pages
            ),
            "原文" * 9000,
        )
        await self.p._ensure_layout("default")
        self.assertEqual(
            len(self.p.store.catalog("default", self.p.settings())["pages"]), 2
        )
        request = await self.s.prepare(self.s.message(2))
        self.assertNotIn("原文原文", request.system_prompt)
        self.assertIn("迁移记忆", request.extra_user_content_parts[0]["text"])

    async def test_notebook_guidance_is_only_in_reply_tool_definitions(self):
        await self.p.notebook_create(
            self.s.event, "流程", "处理故障时读取", "private page body"
        )
        req = await self.s.prepare(self.s.message(2))
        with patch("astrbot.core.astr_main_agent.SkillManager") as skills:
            skills.return_value.list_skills.return_value = []
            await _ensure_persona_and_skills(req, {}, self.s.context, self.s.event)
        schemas = {
            row["function"]["name"]: row["function"]
            for row in req.func_tool.openai_schema()
        }
        message_text = req.system_prompt + json.dumps(
            [req.contexts, await req.assemble_context()], ensure_ascii=False
        )
        for name, guidance in {
            "notebook_read": "根据本轮目录的页面名称和描述主动读取正文",
            "notebook_create": "遇到值得记住的事实",
            "notebook_update": "整体替换正文前分段读完整页",
            "notebook_delete": "释放页数",
        }.items():
            self.assertIn(guidance, schemas[name]["description"])
            self.assertNotIn(guidance, message_text)
        self.assertNotIn("[记忆与记事本]", message_text)
        self.assertIn("流程", message_text)
        self.assertNotIn("private page body", message_text)

    async def test_failed_migration_preserves_entire_original(self):
        self.s.config["notebook_max_pages"] = 1
        old = replace_section("Core", "memory", "x" * 13000)
        await self.p._original_update("default", system_prompt=old)
        with self.assertRaisesRegex(ValueError, "notebook_full"):
            await self.p._ensure_layout("default")
        self.assertEqual(
            (await self.s.manager.get_persona("default")).system_prompt, old
        )
        self.assertEqual(
            self.p.store.catalog("default", self.p.settings())["page_count"], 0
        )

    async def test_only_directory_is_injected_and_reply_history_kept(self):
        saved = json.loads(
            await self.p.notebook_create(
                self.s.event, "流程", "处理故障时读取", "private page body"
            )
        )
        event = self.s.message(2)
        req = await self.s.prepare(event)
        req.contexts = [{"role": "user", "content": "old full reply context"}]
        await self.p.prepare_prompt_tools(event, req)
        self.assertIn("流程", req.extra_user_content_parts[0]["text"])
        self.assertNotIn("private page body", str(req.extra_user_content_parts))
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertEqual(
            req.contexts, [{"role": "user", "content": "old full reply context"}]
        )
        self.assertTrue(req.extra_user_content_parts[0]["_no_save"])
        data = json.loads(await self.p.notebook_read(event, saved["page_id"]))
        self.assertEqual(data["content"], "private page body")

    async def test_tools_read_update_delete_and_concurrent_editor(self):
        created = json.loads(
            await self.p.notebook_create(
                self.s.event, "Facts", "Facts for Alice", "Initial fact"
            )
        )
        page_id = created["page_id"]
        other = self.s.message(2)
        await self.s.prepare(other)
        result = json.loads(
            await self.p.notebook_update(other, page_id, content="No read")
        )
        self.assertEqual(result["error"], "read_page_first")
        await self.p.notebook_read(other, page_id)
        await self.p.notebook_update(self.s.event, page_id, content="New fact")
        result = json.loads(
            await self.p.notebook_update(other, page_id, content="Stale fact")
        )
        self.assertEqual(result["error"], "page_changed")
        await self.p.notebook_read(other, page_id)
        self.assertTrue(json.loads(await self.p.notebook_delete(other, page_id))["ok"])
        self.assertEqual(
            self.p.store.catalog("default", self.p.settings())["page_count"], 0
        )
        self.assertEqual(
            json.loads(await self.p.notebook_read(other, page_id))["error"],
            "page_not_found",
        )

    async def test_pagination_requires_whole_page_before_replacement(self):
        page = json.loads(
            await self.p.notebook_create(
                self.s.event, "Long", "Long procedure", "x" * 8001
            )
        )
        event = self.s.message(2)
        await self.s.prepare(event)
        await self.p.notebook_read(event, page["page_id"])
        self.assertEqual(
            json.loads(
                await self.p.notebook_update(event, page["page_id"], content="new")
            )["error"],
            "read_page_first",
        )
        await self.p.notebook_read(event, page["page_id"], 4000)
        await self.p.notebook_read(event, page["page_id"], 8000)
        self.assertTrue(
            json.loads(
                await self.p.notebook_update(event, page["page_id"], content="complete")
            )["ok"]
        )

    async def collect(self, count, start=0, origin="test:GroupMessage:room"):
        for number in range(start, start + count):
            await self.p.collect_history(self.s.message(number, origin=origin))

    async def test_configurable_batch_updates_and_preserves_sources(self):
        self.s.config["auto_memory_message_interval"] = 3
        self.s.provider.text = '{"updates":{"persona":"Nickname Echo"}}'
        await self.collect(2)
        await self.p._process_ready()
        self.assertEqual(len(self.s.provider.calls), 0)
        await self.collect(1, 2)
        await self.p._process_ready()
        self.assertEqual(len(self.s.provider.calls), 1)
        self.assertEqual(self.p.store.pending_count(), 0)
        rows = self.p.store.records("default")
        automatic = next(row for row in rows if row["kind"] == "automatic")
        record = self.p.store.record("default", automatic["id"])
        self.assertTrue(record["changed"])
        self.assertEqual(
            [row["text"] for row in record["sources"]],
            ["message 0", "message 1", "message 2"],
        )
        self.assertEqual(
            read_sections((await self.s.manager.get_persona("default")).system_prompt)[
                "persona"
            ],
            "Nickname Echo",
        )
        payload = self.s.provider.calls[0]
        self.assertNotIn("revision", payload["prompt"])
        self.assertNotIn("版本", payload["system_prompt"])
        with self.p.store._connect() as db:
            rows = db.execute("SELECT payload FROM messages WHERE done=1").fetchall()
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row[0] != "{}" for row in rows))

    async def test_unchanged_batch_has_its_own_sources_and_log(self):
        self.s.config["auto_memory_message_interval"] = 2
        before = (await self.s.manager.get_persona("default")).system_prompt
        await self.collect(2)
        await self.p._process_ready()
        await self.collect(2, 2)
        await self.p._process_ready()
        records = [
            self.p.store.record("default", row["id"])
            for row in self.p.store.records("default")
            if row["kind"] == "automatic"
        ]
        self.assertEqual(len(records), 2)
        self.assertTrue(
            all(not row["changed"] and row["state"] == "committed" for row in records)
        )
        self.assertEqual(
            [source["text"] for source in records[0]["sources"]],
            ["message 2", "message 3"],
        )
        self.assertEqual(
            (await self.s.manager.get_persona("default")).system_prompt, before
        )

    async def test_unchanged_over_budget_memory_still_records_sources(self):
        current = (await self.s.manager.get_persona("default")).system_prompt
        await self.s.manager.update_persona(
            "default", system_prompt=replace_section(current, "persona", "x" * 8100)
        )
        self.s.config["auto_memory_message_interval"] = 1
        await self.collect(1)
        await self.p._process_ready()
        self.assertEqual(self.p.store.pending_count(), 0)
        row = next(
            row for row in self.p.store.records("default") if row["kind"] == "automatic"
        )
        self.assertFalse(row["changed"])
        self.assertEqual(row["state"], "committed")

    async def test_background_worker_processes_wakeup(self):
        self.s.config["auto_memory_message_interval"] = 1
        await self.collect(1)
        SelfPrompt._start_worker(self.p)

        async def wait():
            while self.p.store.pending_count():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 2)
        self.assertEqual(len(self.s.provider.calls), 1)

    async def test_create_after_delete_and_after_change_is_not_stale_cached_success(
        self,
    ):
        original = json.loads(
            await self.p.notebook_create(self.s.event, "A", "purpose", "one")
        )
        await self.p.notebook_delete(self.s.event, original["page_id"])
        second = json.loads(
            await self.p.notebook_create(self.s.event, "A", "purpose", "one")
        )
        self.assertNotEqual(original["page_id"], second["page_id"])
        await self.p.notebook_update(self.s.event, second["page_id"], content="two")
        third = json.loads(
            await self.p.notebook_create(self.s.event, "A", "purpose", "one")
        )
        self.assertNotEqual(second["page_id"], third["page_id"])

    async def test_projection_archives_original_fields_once(self):
        req = self.s.request
        req.contexts = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c",
                        "function": {
                            "name": "update_self_prompt",
                            "arguments": '{"expected_revision":"old"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c",
                "content": '{"revision":"old","ok":true}',
            },
        ]
        before = copy.deepcopy(req.contexts)
        await self.p.prepare_prompt_tools(self.s.event, req)
        await self.p.prepare_prompt_tools(self.s.event, req)
        self.assertNotIn("revision", json.dumps(req.contexts))
        with self.p.store._connect() as db:
            saved = [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT message FROM original_tool_messages ORDER BY rowid"
                )
            ]
        self.assertEqual(saved, before)

    async def test_native_editor_preserves_other_persona_fields(self):
        await self.s.manager.update_persona(
            "default", tools=["get_self_prompt"], skills=[], begin_dialogs=["hello"]
        )
        prompt = (await self.s.manager.get_persona("default")).system_prompt
        await self.s.manager.update_persona(
            "default", system_prompt=replace_section(prompt, "persona", "Name")
        )
        saved = await self.s.manager.get_persona("default")
        self.assertEqual(saved.tools, ["get_self_prompt"])
        self.assertEqual(saved.skills, [])
        self.assertEqual(saved.begin_dialogs, ["hello"])

    async def test_external_content_change_is_not_reported_as_unchanged(self):
        current = (await self.s.manager.get_persona("default")).system_prompt
        await self.p._original_update(
            "default",
            system_prompt=replace_section(current, "persona", "External edit"),
        )
        await self.p._ensure_layout("default")
        row = self.p.store.records("default")[0]
        self.assertEqual(row["kind"], "external")
        self.assertTrue(row["changed"])

    async def test_silent_messages_and_completed_replies_count_once(self):
        self.s.config["auto_memory_message_interval"] = 4
        for number in range(2):
            event = self.s.message(number)
            await self.p.collect_history(event)
            await self.p.collect_history(event)
            response = LLMResponse("assistant", completion_text="final")
            await self.p.collect_reply(
                event,
                LLMResponse("assistant", completion_text="plan", tools_call_name=["x"]),
            )
            await self.p.collect_reply(event, response)
            await self.p.collect_reply(event, response)
        await self.p._process_ready()
        self.assertEqual(len(self.s.provider.calls), 1)
        history = json.loads(self.s.provider.calls[0]["prompt"])["history"]
        self.assertEqual(
            [row["role"] for row in history], ["user", "assistant", "user", "assistant"]
        )
        await self.p.collect_history(self.s.message(0))
        self.assertEqual(self.p.store.pending_count(), 0)

    async def test_manual_save_during_consolidation_wins(self):
        self.s.config["auto_memory_message_interval"] = 1

        async def intervene(**kwargs):
            original = (await self.s.manager.get_persona("default")).system_prompt
            updated = replace_section(original, "persona", "Owner says this")
            await self.s.manager.update_persona("default", system_prompt=updated)
            return LLMResponse(
                "assistant", completion_text='{"updates":{"persona":"Stale result"}}'
            )

        self.s.provider.text = intervene
        await self.collect(1)
        await self.p._process_ready()
        self.assertEqual(
            read_sections((await self.s.manager.get_persona("default")).system_prompt)[
                "persona"
            ],
            "Owner says this",
        )
        self.assertEqual(self.p.store.pending_count(), 1)
        self.assertTrue(
            any(row["state"] == "failed" for row in self.p.store.records("default"))
        )

    async def test_disallowed_or_oversized_update_cannot_advance_queue(self):
        self.s.config["auto_memory_message_interval"] = 1
        original = (await self.s.manager.get_persona("default")).system_prompt
        await self.collect(1)
        for text in (
            '{"updates":{"memory":"not allowed"}}',
            json.dumps({"updates": {"persona": "x" * 8001}}),
        ):
            self.s.provider.text = text
            self.p._retry_after.clear()
            await self.p._process_ready()
            self.assertEqual(self.p.store.pending_count(), 1)
            self.assertEqual(
                (await self.s.manager.get_persona("default")).system_prompt, original
            )

    async def test_native_editor_saves_each_snapshot_and_keeps_fixed(self):
        original = (await self.s.manager.get_persona("default")).system_prompt
        first = owner_edit(
            original,
            "Exact fixed  \n",
            {"persona": "First", "output_preferences": "Short", "nicknames": "Echo"},
        )
        await self.s.manager.update_persona("default", system_prompt=first)
        second = replace_section(first, "persona", "Second")
        await self.s.manager.update_persona("default", system_prompt=second)
        manual = [
            self.p.store.record("default", row["id"])
            for row in self.p.store.records("default")
            if row["kind"] == "manual"
        ]
        self.assertEqual(
            [
                current_memory(row["content"])["sections"]["persona"]
                for row in manual[:2]
            ],
            ["Second", "First"],
        )
        self.assertEqual(current_memory(second)["fixed_prompt"], "Exact fixed  \n")
        self.assertTrue(all(row["sources"] == [] for row in manual))

    async def test_prepared_write_recovers_and_acknowledges_exact_batch(self):
        self.s.config["auto_memory_message_interval"] = 2
        await self.collect(3)
        original = (await self.s.manager.get_persona("default")).system_prompt
        target = replace_section(original, "persona", "Recovered")
        scope = self.p.store.ready(2)[0]
        records = self.p.store.batch(scope["id"], 2)
        record_id = self.p.store.stage(
            "default",
            original,
            target,
            "automatic",
            records,
            scope["id"],
            records[-1]["id"],
        )
        await self.p._original_update("default", system_prompt=target)
        await self.p._recover("default")
        await self.p._recover("default")
        self.assertEqual(self.p.store.pending_count(), 1)
        self.assertEqual(
            self.p.store.record("default", record_id)["state"], "committed"
        )

    async def test_legacy_history_projection_preserves_original_and_other_tools(self):
        content = json.dumps(
            {
                "ok": True,
                "revision": "private-hash",
                "sections": {"persona": "Keep this"},
            }
        )
        history = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c",
                        "function": {"name": "get_self_prompt", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c",
                "content": [{"type": "text", "text": content}],
            },
            {"role": "tool", "tool_call_id": "other", "content": content},
        ]
        before = copy.deepcopy(history)
        projected = project_history(history)
        self.assertNotIn("private-hash", projected[1]["content"][0]["text"])
        self.assertIn("Keep this", projected[1]["content"][0]["text"])
        self.assertEqual(projected[2], history[2])
        self.assertEqual(history, before)

    async def test_reload_restores_editor_hook_and_preserves_pending(self):
        await self.collect(1)
        await self.p.terminate()
        self.assertIs(self.s.manager.update_persona, self.p._original_update)
        with patch(
            "astrbot_plugin_self_prompt.main.StarTools.get_data_dir",
            return_value=self.s.data,
        ):
            other = SelfPrompt(self.s.context, self.s.config)
        await other._ensure_layout("default")
        self.assertEqual(other.store.pending_count(), 1)
        await other.terminate()

    async def test_consolidation_reads_notebook_and_retains_original_sources(self):
        self.s.config["auto_memory_message_interval"] = 2
        page = self.p.store.create_page(
            "default",
            "称呼线索",
            "理解对话对象",
            "群友把机器人叫作新绰号，lljy 表示柳浪家园。",
            self.p.settings(),
        )
        await self.collect(2)
        before = (await self.s.manager.get_persona("default")).system_prompt
        persona = (
            read_sections(before)["persona"]
            + "\n新绰号是机器人的称呼；lljy＝柳浪家园。"
        )
        self.s.provider.text = json.dumps(
            {"updates": {"persona": persona}}, ensure_ascii=False
        )
        await self.p._process_ready()
        payload = json.loads(self.s.provider.calls[0]["prompt"])
        notes = [r for r in payload["history"] if r.get("source_type") == "notebook"]
        self.assertEqual(len(notes), 1)
        self.assertIn("新绰号", notes[0]["text"])
        self.assertNotIn("page_stamp", notes[0])
        actual = (await self.s.manager.get_persona("default")).system_prompt
        self.assertIn("新绰号", read_sections(actual)["persona"])
        automatic = next(
            r for r in self.p.store.records("default") if r["kind"] == "automatic"
        )
        sources = self.p.store.record("default", automatic["id"])["sources"]
        self.assertEqual(len(sources), 3)
        full = self.p.store.page("default", page["page_id"])
        from astrbot_plugin_self_prompt.store import page_stamp

        self.p.store.change_page(
            "default",
            page["page_id"],
            page_stamp(full),
            {},
            self.p.settings(),
            delete=True,
        )
        self.assertEqual(
            self.p.store.record("default", automatic["id"])["sources"][-1]["text"],
            notes[0]["text"],
        )
        self.assertEqual(self.p.store.notebook_sources("default"), [])
        self.assertEqual(self.p.store.pending_count(), 0)

    async def test_unchanged_notebook_is_not_reread_and_edits_are_retried_after_failure(
        self,
    ):
        from astrbot_plugin_self_prompt.store import page_stamp

        self.s.config["auto_memory_message_interval"] = 1
        page = self.p.store.create_page(
            "default", "术语", "含义", "old term", self.p.settings()
        )
        await self.collect(1)
        await self.p._process_ready()
        self.assertEqual(self.p.store.notebook_sources("default"), [])
        await self.collect(1, 1)
        await self.p._process_ready()
        self.assertFalse(
            any(
                r.get("source_type") == "notebook"
                for r in json.loads(self.s.provider.calls[-1]["prompt"])["history"]
            )
        )
        full = self.p.store.page("default", page["page_id"])
        self.p.store.change_page(
            "default",
            page["page_id"],
            page_stamp(full),
            {"content": "new term"},
            self.p.settings(),
        )
        self.s.provider.text = "invalid result"
        await self.collect(1, 2)
        await self.p._process_ready()
        self.assertEqual(self.p.store.pending_count(), 1)
        self.assertEqual(
            self.p.store.notebook_sources("default")[0]["text"], "new term"
        )
        self.s.provider.text = '{"updates":{}}'
        self.p._retry_after.clear()
        await self.p._process_ready()
        self.assertEqual(self.p.store.notebook_sources("default"), [])
        record = next(
            r for r in self.p.store.records("default") if r["kind"] == "automatic"
        )
        saved = self.p.store.record("default", record["id"])
        self.assertFalse(saved["changed"])
        self.assertEqual(saved["sources"][-1]["text"], "new term")
